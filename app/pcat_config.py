# -*- coding: utf-8 -*-
#@Author    : qichunren

import configparser, os
import json
from io import StringIO
import threading
import os
import tempfile
import stat
import glob
import logging
import time

config = configparser.ConfigParser()
config_path = "/etc/pcat-manager-web.ini"
user_config_path = "/etc/pcat-manager-web-user.json"
HOOK_PATH = "/etc/pcat_hook_cmd.sh"
user_config=None
_save_lock = threading.Lock()
CPU_GOV_PATH = "/sys/devices/system/cpu/"

default_user_config = {
    "sms_hook_enabled": False,
    'roaming': 1,
    'lte_bands_allowed': "all",
    'nr5g_bands_allowed': "all",
    'roaming_pref': "all",
    'show_debug_logs': False,
    'network_mode': 'auto',
    'fan_mode': 'smart_quiet'
    }



version = "2.0.0"
skip_auth = False # Config by --skip-auth, skip linux user password auth. This flag can be changed by other.
skip_pin = False # Config by --skip-pin, skip PIN protection for protected routes during development.
web_port = 80 # Config by --port 81
luci_port = 8080

net_stats_db = "/etc/net_stats.db"
modem_db = "/etc/pc_modem.db"
modem_dev = "/dev/ttyUSB3"
log_file = "/tmp/pcat-manager-web.log"

import oem_config as _oem

def get_package_urls():
    """
    Get package URLs from OEM config (boot1) or fallback to empty.
    """
    firmware_base = _oem.get_firmware_base_url()
    if firmware_base:
        # Ensure trailing slash
        if not firmware_base.endswith("/"):
            firmware_base += "/"
        return {
            "package_version_url": firmware_base + "packages-volatile.txt",
            "volatile_packages_url": firmware_base + "volatile/"
        }
    # No firmware URL configured
    return {
        "package_version_url": "",
        "volatile_packages_url": ""
    }

# Get URLs based on OEM config
_package_urls = get_package_urls()
kernel_v = "r2304"
package_version_url = _package_urls["package_version_url"]
volatile_packages_url = _package_urls["volatile_packages_url"]


#if size of modem db is zero, remove and go ahead
if os.path.exists(modem_db):
    if os.path.getsize(modem_db) == 0:
        os.remove(modem_db)

#####################################################################################

# Delegate OEM functions to oem_config module (reads from boot1 partition)
def get_oem_name():
    return _oem.get_oem_name()

def get_logo_uri():
    return _oem.get_logo_uri()

def get_text_logo_uri():
    return _oem.get_text_logo_uri()

def get_vendor_site_domain():
    return _oem.get_vendor_site_domain()

def get_vendor_wiki_url():
    return _oem.get_vendor_wiki_url()


def get_user_config_key(key, fallback=None):
    """
    Safely retrieve `key` from global_user_config.
    If missing, log a warning and return `fallback`.
    """
    return global_user_config.get(key, fallback)

def set_user_config_key(key, value):
    """
    Safely set `key` in global_user_config.
    If missing, log a warning and return `fallback`.
    """
    global_user_config[key] = value
    save_user_configs(global_user_config)


def get_user_configs(fallback=default_user_config):
    """
    Load the user JSON config from disk.
    If missing or invalid, reset to fallback and return that.
    """
    global default_user_config
    fallback = default_user_config

    try:
        if os.path.exists(user_config_path):
            with open(user_config_path, 'r') as f:
                return json.load(f)
        else:
            # File doesn't exist: create with fallback
            with open(user_config_path, 'w') as f:
                json.dump(fallback, f, indent=4)
            return fallback

    except (IOError, json.JSONDecodeError) as e:
        logging.error(f"Error loading user config ({e!r}); resetting to default.")
        try:
            with open(user_config_path, 'w') as f:
                json.dump(fallback, f, indent=4)
        except IOError as write_err:
            logging.error(f"Failed to write fallback config: {write_err!r}")
        return fallback

def get_cpin():
    cpin = get_user_config_key("cpin", "")
    if len(cpin) >= 3:
        return cpin
    else:
        return ""

def set_cpin(cpin):
    if len(cpin) == 0:
        set_user_config_key("cpin", "")
    else:
        set_user_config_key("cpin", cpin)
    

def save_user_configs(config=None):
    """
    Thread-safe save of the provided config (or global_user_config) to disk.
    """
    print("save_user_configs to file...")
    cfg = config or global_user_config

    # only one writer at a time
    with _save_lock:
        try:
            # ensure directory exists
            os.makedirs(os.path.dirname(user_config_path), exist_ok=True)
            with open(user_config_path, 'w') as f:
                json.dump(cfg, f, indent=4)
        except IOError as e:
            logging.error(f"Error saving user config ({e!r})")

def save_config():
    sections = config.sections()
    if 'modem' not in sections:
        config.add_section('modem')
    if 'device' not in sections:
        config.add_section('device')    
    with open(config_path, 'w') as config_file:
        config.write(config_file, space_around_delimiters = False)

def load_sms_hook_script():
    """
    Return the contents of the hook script, or an empty string if it doesn't exist.
    """
    try:
        with open(HOOK_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except OSError as e:
        # Log e if you have a logger, then fall back to empty script
        return ""

def save_sms_hook_script(script: str) -> bool:
    """
    Atomically save `script` to HOOK_PATH, make it executable, and return True on success.
    """
    dirpath = os.path.dirname(HOOK_PATH)
    try:
        # Ensure the directory exists
        os.makedirs(dirpath, exist_ok=True)

        # Write to a temp file in the same directory
        fd, temp_path = tempfile.mkstemp(dir=dirpath, prefix=".hook_tmp_")
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(script)

        # Move into place (atomic on most Unixes)
        os.replace(temp_path, HOOK_PATH)

        # Make executable by owner
        os.chmod(HOOK_PATH, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        return True
    except OSError:
        # Cleanup temp if it still exists
        try:
            os.remove(temp_path)
        except Exception:
            pass
        return False

def enable_sms_hook(isEnabled: bool):
    global_user_config["sms_hook_enabled"] = isEnabled
    save_user_configs()

def get_sms_hook_enabled():
    return get_user_config_key("sms_hook_enabled", False)

def _get_big_core_ids():
    """Detect big cores by finding CPUs with highest max frequency (RK3576: A72 cores)."""
    freqs = {}
    for cpu_dir in glob.glob(os.path.join(CPU_GOV_PATH, "cpu[0-9]*")):
        cpu_id = os.path.basename(cpu_dir)
        freq_path = os.path.join(cpu_dir, "cpufreq", "cpuinfo_max_freq")
        online_path = os.path.join(cpu_dir, "online")
        was_offline = False
        try:
            with open(freq_path, "r") as f:
                freqs[cpu_id] = int(f.read().strip())
        except OSError:
            # Core may be offline - temporarily bring it online to read freq
            try:
                with open(online_path, "r") as f:
                    if f.read().strip() == "0":
                        was_offline = True
                        with open(online_path, "w") as fw:
                            fw.write("1")
                        time.sleep(0.1)
                        with open(freq_path, "r") as f:
                            freqs[cpu_id] = int(f.read().strip())
            except (OSError, ValueError):
                pass
            finally:
                if was_offline:
                    try:
                        with open(online_path, "w") as fw:
                            fw.write("0")
                    except OSError:
                        pass
        except ValueError:
            continue
    if not freqs:
        return []
    max_freq = max(freqs.values())
    min_freq = min(freqs.values())
    if max_freq == min_freq:
        return []  # All cores same speed, no big/LITTLE
    return [cpu_id for cpu_id, freq in freqs.items() if freq == max_freq]


def get_cpu_scheduling_enabled() -> bool:
    """Check if powersave mode is active (big cores offlined)."""
    big_cores = _get_big_core_ids()
    if not big_cores:
        # Fallback: check governor for non big.LITTLE chips
        path = os.path.join(CPU_GOV_PATH, "cpu0", "cpufreq", "scaling_governor")
        try:
            with open(path, "r") as f:
                return f.read().strip() == "powersave"
        except (FileNotFoundError, OSError):
            return False
    # Check if any big core is offline
    for cpu_id in big_cores:
        online_path = os.path.join(CPU_GOV_PATH, cpu_id, "online")
        try:
            with open(online_path, "r") as f:
                if f.read().strip() == "1":
                    return False  # At least one big core is online
        except (FileNotFoundError, OSError):
            continue
    return True


def set_cpu_scheduling_enabled(isEnabled: bool):
    """Enable powersave by offlining big cores (A72), disable by bringing them back online."""
    big_cores = _get_big_core_ids()
    if big_cores:
        # big.LITTLE: offline/online the big cores
        for cpu_id in big_cores:
            online_path = os.path.join(CPU_GOV_PATH, cpu_id, "online")
            try:
                with open(online_path, "w") as f:
                    f.write("0" if isEnabled else "1")
            except (PermissionError, FileNotFoundError, OSError):
                continue
    else:
        # Fallback for non big.LITTLE: use governor
        target = "powersave" if isEnabled else "schedutil"
        pattern = os.path.join(CPU_GOV_PATH, "cpu[0-9]*", "cpufreq", "scaling_governor")
        for gov_file in glob.glob(pattern):
            try:
                with open(gov_file, "w") as f:
                    f.write(target)
            except (PermissionError, FileNotFoundError, OSError):
                continue

    # persist user setting
    global_user_config["cpu_scheduling_enabled"] = isEnabled
    save_user_configs()

def get_fan_speed() -> int:
    """
    Get current fan speed level (0-9 scale, where 9=100%, 0=0%)
    Returns the fan speed from user config, defaulting to 5 (50%)
    """
    return get_user_config_key("fan_speed", 5)

def get_actual_fan_speed() -> int:
    """
    Get actual fan speed from hwmon sensors in RPM
    Scans /sys/class/hwmon/hwmon*/fan1_input and returns the first valid reading
    Returns 0 if no fan speed can be read
    """
    import glob
    import os
    
    # Search for hwmon fan speed files
    fan_paths = glob.glob("/sys/class/hwmon/hwmon*/fan1_input")
    
    for fan_path in fan_paths:
        try:
            if os.path.exists(fan_path) and os.access(fan_path, os.R_OK):
                with open(fan_path, "r") as f:
                    fan_rpm = int(f.read().strip())
                    if fan_rpm > 0:  # Valid fan speed reading
                        return fan_rpm
        except (FileNotFoundError, PermissionError, ValueError) as e:
            # Continue to next hwmon device
            continue
    
    return 0  # No valid fan speed found

def set_fan_speed(speed: int):
    """
    Set fan speed level (0-9 scale, where 9=100%, 0=0%)
    Controls thermal cooling devices via /sys/class/thermal/cooling_device*/cur_state
    """
    # Clamp speed to valid range
    speed = max(0, min(9, int(speed)))
    
    # Find pcat-pm-fan cooling devices
    thermal_path = "/sys/class/thermal"
    for device_num in range(10):  # check cooling_device0 through cooling_device9
        device_path = f"{thermal_path}/cooling_device{device_num}"
        type_path = f"{device_path}/type"
        state_path = f"{device_path}/cur_state"
        
        try:
            # Check if this cooling device is a pcat-pm-fan
            with open(type_path, "r") as f:
                device_type = f.read().strip()
            
            if device_type == "pcat-pm-fan":
                # Set the fan speed
                with open(state_path, "w") as f:
                    f.write(str(speed))
                print(f"Set fan speed to {speed} for cooling_device{device_num}")
                
        except (FileNotFoundError, PermissionError) as e:
            # Device doesn't exist or permission denied, continue to next
            continue
    
    # Persist user setting
    global_user_config["fan_speed"] = speed
    save_user_configs()

def set_fan_speed_smart(temperature: float | None, user_max_speed: int | None, mode: str = "smart_quiet"):
    # Base temperature thresholds for smart_quiet mode
    CHARGING_START_FAN_TEMP = 45
    DISCHARGING_START_FAN_TEMP = 52
    CRITICAL_TEMP = 60  # Critical temperature for machine protection
    CRITICAL_FAN_SPEED = 8  # Override fan speed for machine protection

    # Apply custom temperature settings for smart_cool mode
    if mode == "smart_cool":
        CHARGING_START_FAN_TEMP = 43
        DISCHARGING_START_FAN_TEMP = 48

    # Handle None values gracefully
    if user_max_speed is None:
        user_max_speed = 5  # Default to medium speed

    user_max_speed = max(0, min(9, int(user_max_speed)))  # Clamp to 0-9
    calculated_speed = 0

    # Handle None temperature gracefully - set speed to 0
    if temperature is None:
        final_speed = 0  # Turn off fan when temperature unavailable
        set_fan_speed_direct(final_speed)
        return final_speed

    # Critical temperature override - protect machine regardless of user settings
    if temperature > CRITICAL_TEMP:  # type: ignore[operator]
        final_speed = CRITICAL_FAN_SPEED
        set_fan_speed_direct(final_speed)
        return final_speed

    # Check charging status directly
    is_charging = is_device_charging()

    if is_charging:
        if temperature >= CHARGING_START_FAN_TEMP:  # type: ignore[operator]
            calculated_speed = int(temperature - CHARGING_START_FAN_TEMP + 1)  # type: ignore[operator]
    else:
        if temperature >= DISCHARGING_START_FAN_TEMP:  # type: ignore[operator]
            calculated_speed = int(temperature - DISCHARGING_START_FAN_TEMP + 1)  # type: ignore[operator]

    # Cap at user maximum and system maximum (9)
    final_speed = min(calculated_speed, user_max_speed, 9)
    final_speed = max(0, final_speed)  # Ensure non-negative

    # Apply the calculated speed to the hardware
    set_fan_speed_direct(final_speed)

    return final_speed

def is_device_charging() -> bool:
    """
    Detect if device is charging based on sysfs voltage, current, and battery level

    Returns True if:
    - Input voltage > 3V AND current > -0.1A AND battery level < 95%

    Returns:
        bool: True if charging, False otherwise
    """
    try:
        # Read voltage from sysfs (in microvolts)
        with open("/sys/class/power_supply/battery/voltage_now", "r") as f:
            voltage_uv = int(f.read().strip())
        voltage_v = voltage_uv / 1000000  # Convert to volts

        # Read current from sysfs (in microamps)
        with open("/sys/class/power_supply/battery/current_now", "r") as f:
            current_ua = int(f.read().strip())
        current_a = current_ua / 1000000  # Convert to amps

        # Read battery capacity (State of Charge) from sysfs (percentage)
        with open("/sys/class/power_supply/battery/capacity", "r") as f:
            soc = int(f.read().strip())

        # Charging detection logic: voltage > 3V AND current > -0.1A AND SOC < 95%
        is_charging = voltage_v > 3.0 and current_a > -0.1 and soc < 95

        return is_charging

    except (FileNotFoundError, PermissionError, ValueError) as e:
        # If we can't read battery info, assume not charging
        print(f"Error reading battery status for charging detection: {e}")
        return False

def set_fan_speed_direct(speed: int):
    """
    Directly set fan speed without updating user config (used by smart algorithm)

    Args:
        speed: Fan speed level (0-9)
    """
    # Clamp speed to valid range
    speed = max(0, min(9, int(speed)))

    # Find pcat-pm-fan cooling devices
    thermal_path = "/sys/class/thermal"
    for device_num in range(10):  # check cooling_device0 through cooling_device9
        device_path = f"{thermal_path}/cooling_device{device_num}"
        type_path = f"{device_path}/type"
        state_path = f"{device_path}/cur_state"

        try:
            # Check if this cooling device is a pcat-pm-fan
            with open(type_path, "r") as f:
                device_type = f.read().strip()

            if device_type == "pcat-pm-fan":
                # Set the fan speed
                with open(state_path, "w") as f:
                    f.write(str(speed))
                print(f"Smart fan control: Set fan speed to {speed} for cooling_device{device_num}")

        except (FileNotFoundError, PermissionError) as e:
            # Device doesn't exist or permission denied, continue to next
            continue

def get_sms_limit_for_screen() -> int:
    """
    Get SMS limit for screen display
    Returns the maximum number of SMS messages to show on screen, default 5
    """
    return get_user_config_key("sms_limit_for_screen", 5)

def set_sms_limit_for_screen(limit: int):
    """
    Set SMS limit for screen display
    """
    # Clamp limit to reasonable range (1-100)
    limit = max(1, min(100, int(limit)))
    
    global_user_config["sms_limit_for_screen"] = limit
    save_user_configs()

def get_show_debug_logs() -> bool:
    """
    Get debug logging visibility setting for firmware upgrades
    Returns True if debug logs should be shown, False if hidden (default)
    """
    return get_user_config_key("show_debug_logs", False)

def set_show_debug_logs(show: bool):
    """
    Set debug logging visibility setting for firmware upgrades
    """
    global_user_config["show_debug_logs"] = bool(show)
    save_user_configs()

def get_network_mode() -> str:
    """
    Get saved network mode setting
    Returns the network mode: 'auto', 'eth_only', 'cell_only', or 'all_off'
    Defaults to 'auto' if not set
    """
    return get_user_config_key("network_mode", "auto")

def set_network_mode(mode: str):
    """
    Set network mode setting
    Valid modes: 'auto', 'eth_only', 'cell_only', 'all_off'
    """
    valid_modes = ['auto', 'eth_only', 'cell_only', 'all_off']
    if mode not in valid_modes:
        logging.warning(f"Invalid network mode '{mode}', defaulting to 'auto'")
        mode = 'auto'

    global_user_config["network_mode"] = mode
    save_user_configs()

def get_power_mode() -> str:
    """
    Get saved power mode setting.
    Returns: 'manual', 'car', 'timer', or 'direct_power'
    Defaults to 'manual' if not set.
    """
    return get_user_config_key("power_mode", "manual")

def set_power_mode(mode: str):
    """
    Set power mode setting.
    Valid modes: 'manual', 'car', 'timer', 'direct_power'
    """
    valid_modes = ['manual', 'car', 'timer', 'direct_power']
    if mode not in valid_modes:
        logging.warning(f"Invalid power mode '{mode}', defaulting to 'manual'")
        mode = 'manual'
    global_user_config["power_mode"] = mode
    save_user_configs()

def get_fan_mode() -> str:
    """
    Get saved fan mode setting
    Returns the fan mode: 'manual', 'smart_quiet', or 'smart_cool'
    Defaults to 'smart_quiet' if not set
    """
    return get_user_config_key("fan_mode", "smart_quiet")

def set_fan_mode(mode: str):
    """
    Set fan mode setting
    Valid modes: 'manual', 'smart_quiet', 'smart_cool'
    """
    valid_modes = ['manual', 'smart_quiet', 'smart_cool']
    if mode not in valid_modes:
        logging.warning(f"Invalid fan mode '{mode}', defaulting to 'smart_quiet'")
        mode = 'smart_quiet'

    global_user_config["fan_mode"] = mode
    save_user_configs()


#####################################################################################

global_user_config = get_user_configs()