# -*- coding: utf-8 -*-
#@Author    : qichunren

import threading
# import pyprctl # patch:set real thread name
import time
import os
import json
import datetime
import subprocess
import shutil
import pathlib
import re
import requests # on product device: 2.25.1

import network_stats
import pcat_config
import oem_config
import power_event_log
import battery_history
from cmd_tool import CmdTool
from pcat_util import pcat_logger
from pcat_util import hardware_util
from pcat_util import command_parser


socket_client = None
modem_client = None
flask_app = None
main_app = None # Potin to App instance
timer_manager = None

def cmd_to_log(cmd_args, log_file):
  with open(log_file,"wb") as out, open(log_file,"wb") as err:
    subprocess.run(cmd_args, stdout=out,stderr=err)
  threading.Timer(20.0, lambda: shutil.rmtree("/cmd-shell")).start()    

# 获取文件修改时间
def modification_date(filename):
    t = os.path.getmtime(filename)
    t = datetime.datetime.fromtimestamp(t)
    t = t.strftime("%Y-%m-%d %H:%M:%S")
    return t

# 检测 SD 卡的状态
def sd_card_status(dev = "/dev/mmcblk1"):
  dev_exists = os.path.exists(dev)
  if dev_exists:
    return 1
  else:
    return 0
  
def get_uptime(locales=None):
  uptime_seconds = 0
  with open("/proc/uptime", "r") as f:
    uptime_seconds = float(f.readline().split()[0])
  
  uptime = datetime.timedelta(seconds=uptime_seconds)
  # 分别获取天数、小时数和分钟数
  days = uptime.days
  hours, remainder = divmod(uptime.seconds, 3600)
  minutes, seconds = divmod(remainder, 60)
  
  # Use translations if provided, otherwise fall back to English
  if locales:
    up_text = locales.get("uptime_up", "up")
    day_text = locales.get("uptime_day", "day") if days == 1 else locales.get("uptime_days", "days")
    hour_text = locales.get("uptime_hour", "hour") if hours == 1 else locales.get("uptime_hours", "hours")
    minute_text = locales.get("uptime_minute", "minute") if minutes == 1 else locales.get("uptime_minutes", "minutes")
    second_text = locales.get("uptime_second", "second") if seconds == 1 else locales.get("uptime_seconds", "seconds")
  else:
    up_text = "up"
    day_text = "day" if days == 1 else "days"
    hour_text = "hour" if hours == 1 else "hours"
    minute_text = "minute" if minutes == 1 else "minutes"
    second_text = "second" if seconds == 1 else "seconds"
  
  # 格式化输出字符串
  if days == 0:
    if hours == 0:
      uptime_string = f"{up_text} {minutes} {minute_text}, {seconds} {second_text}"
    else:
      uptime_string = f"{up_text} {hours} {hour_text}, {minutes} {minute_text}"
  else:
    if hours == 0:
      uptime_string = f"{up_text} {days} {day_text}, {minutes} {minute_text}"
    else:
      uptime_string = f"{up_text} {days} {day_text}, {hours} {hour_text}"
  return uptime_string

# Cached machine SN result from oem_config
_machine_sn_cache = None
_machine_sn_verified_cache = None

def is_photonicat2():
  """Check if device is Photonicat 2 based on device tree compatible string"""
  try:
    board_name_raw = open("/proc/device-tree/compatible", "r").read().strip()
    return "photonicat2" in board_name_raw
  except (FileNotFoundError, PermissionError):
    return False

def get_machine_sn():
  """Get machine serial number string (for backward compat)."""
  global _machine_sn_cache, _machine_sn_verified_cache
  if _machine_sn_cache is not None:
    return _machine_sn_cache
  sn, verified = oem_config.get_machine_sn()
  _machine_sn_cache = sn
  _machine_sn_verified_cache = verified
  return sn

def get_machine_sn_verified():
  """Get (sn, is_verified) tuple. SN is verified if Ed25519 signature is valid."""
  global _machine_sn_cache, _machine_sn_verified_cache
  if _machine_sn_cache is not None:
    return (_machine_sn_cache, _machine_sn_verified_cache)
  sn, verified = oem_config.get_machine_sn()
  _machine_sn_cache = sn
  _machine_sn_verified_cache = verified
  return (sn, verified)

def get_battery_charge_discharge_remain_time(): #only work for photonicat2
    if not os.path.exists("/sys/class/power_supply/battery"):
        return {"error": "Battery status not found"}
    
    try:
        # Read battery data from /sys/class/power_supply/battery
        with open("/sys/class/power_supply/battery/energy_full_design", "r") as f:
            full_energy_uWh = int(f.read().strip())
        with open("/sys/class/power_supply/battery/capacity", "r") as f:
            soc = int(f.read().strip())
        with open("/sys/class/power_supply/battery/status", "r") as f:
            status = f.read().strip()

        # Try power_now first, fallback to voltage_now * current_now
        power_now_uW = None
        try:
            with open("/sys/class/power_supply/battery/power_now", "r") as f:
                power_now_uW = int(f.read().strip())
        except (FileNotFoundError, ValueError):
            try:
                with open("/sys/class/power_supply/battery/voltage_now", "r") as f:
                    v_uV = int(f.read().strip())
                with open("/sys/class/power_supply/battery/current_now", "r") as f:
                    c_uA = int(f.read().strip())
                power_now_uW = int(v_uV * c_uA / 1000000)  # uV * uA / 1e6 = uW
            except (FileNotFoundError, ValueError):
                pass

        if power_now_uW is None:
            return {"remaining_time": None}

        is_charging = status == "Charging" or power_now_uW > 0
        # Calculate remaining time
        if power_now_uW > 0:
            # Charging: calculate time to reach 90%
            if soc >= 90:
                remaining_time_hours = 0
            else:
                remaining_time_hours = full_energy_uWh * (90 - soc) / 100 / power_now_uW
        elif power_now_uW < 0:
            remaining_time_hours = full_energy_uWh * soc / 100 / (power_now_uW * -1)
        else:
            remaining_time_hours = None

        if remaining_time_hours is None:
            remaining_time = "-"
        elif remaining_time_hours == 0:
            remaining_time = "-"
        else:
            hours = int(remaining_time_hours)
            minutes = int(round((remaining_time_hours - hours) * 60 / 10) * 10)
            if minutes == 60:
                hours += 1
                minutes = 0
            remaining_time = f"{hours}:{minutes:02d}"

        if is_charging and soc >= 90:
            remaining_time = "-"

        if is_charging and isinstance(remaining_time_hours, (int, float)) and remaining_time_hours > 0 and remaining_time_hours < 1/6:
            remaining_time = "< 0:10"

        return {
            "remaining_time_hours": remaining_time_hours,
            "remaining_time": remaining_time,
        }

    except FileNotFoundError as e:
        return {"error": f"File not found: {e}"}
    except ValueError as e:
        return {"error": f"Invalid data format: {e}"}
    except Exception as e:
        return {"error": f"An error occurred: {e}"}

# 检测 Mutil Wan 的启动状态

from pcat_util.openwrt import uci_get as _openwrt_uci_get
from pcat_util.openwrt import uci_set as _openwrt_uci_set
from pcat_util.openwrt import uci_commit as _openwrt_uci_commit
from pcat_util.openwrt import ubus_call as _openwrt_ubus_call

def uci_get(key):
    return _openwrt_uci_get(key)

def uci_set(key, value):
  kv = key + "=" + value
  return 0 if _openwrt_uci_set(kv) else 1

def uci_call(key, value):
  # This is actually 'uci call' which is rare; keep subprocess
  result = subprocess.run(["uci", "call", key, value],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  return result.returncode

def uci_commit(key):
  return 0 if _openwrt_uci_commit(key) else 1

def luci_reload():
  result = subprocess.run(["luci-reload"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  exit_code = result.returncode
  if exit_code != 0:
    # Suppress error message for failed operations
    pass
  return exit_code

def network_restart():
  result = subprocess.run(["/etc/init.d/network", "restart"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  exit_code = result.returncode
  if exit_code != 0:
    # Suppress error message for failed operations
    pass
  return exit_code

def wifi_reload():
  result = subprocess.run(["/sbin/wifi", "reload"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  exit_code = result.returncode
  if exit_code != 0:
    # Suppress error message for failed operations
    pass
  return exit_code

def does_wan_have_carrier():
  carrier_file = "/sys/class/net/eth0/carrier"
  if os.path.exists(carrier_file):
    try:
      with open(carrier_file, 'r') as f:
        carrier = f.read().strip()
      return carrier == "1"
    except Exception as e:
      # Handle case where carrier file is not readable or contains invalid data
      # This can happen when eth0 exists but has no physical carrier (cellular only mode)
      pcat_logger.logger.debug(f"Cannot read carrier file {carrier_file}: {e}")
      return False
  return False

def does_lan_have_carrier():
  carrier_file = "/sys/class/net/eth1/carrier"
  if os.path.exists(carrier_file):
    try:
      with open(carrier_file, 'r') as f:
        carrier = f.read().strip()
      return carrier == "1"
    except Exception as e:
      # Handle case where carrier file is not readable or contains invalid data
      # This can happen when eth1 exists but has no physical carrier
      pcat_logger.logger.debug(f"Cannot read carrier file {carrier_file}: {e}")
      return False
  return False

# WAN 接口是否开启
def is_wan_interface_enabled():
  try:
    # Check UCI disabled flag - this reflects user intent
    wan_disabled = uci_get("network.wan.disabled")
    wan6_disabled = uci_get("network.wan6.disabled")

    # If both are explicitly disabled, return False
    if wan_disabled == "1" and wan6_disabled == "1":
      return False

    # If either interface is configured to be enabled (disabled=0 or not set), return True
    if wan_disabled == "0" or wan6_disabled == "0":
      return True

    # If disabled flag not set, default to enabled (OpenWrt convention)
    return True
  except:
    return True

def get_wan_ethernet_speed(locales=None):
  """
  Get WAN ethernet interface speed in Mbps
  Returns: string like "1000 Mbps", "100 Mbps", "Disconnected", or "Disabled"
  """
  try:
    # Check if WAN interface is enabled first
    if not is_wan_interface_enabled():
      return locales.get("disabled", "Disabled") if locales else "Disabled"
    
    # Check if eth0 interface exists and get its speed
    speed_file = "/sys/class/net/eth0/speed"
    carrier_file = "/sys/class/net/eth0/carrier"
    
    # Check if interface is physically connected
    if os.path.exists(carrier_file):
      with open(carrier_file, 'r') as f:
        carrier = f.read().strip()
      if carrier != "1":
        return locales.get("disconnected", "Disconnected") if locales else "Disconnected"
    else:
      return locales.get("disconnected", "Disconnected") if locales else "Disconnected"
    
    # Get interface speed
    if os.path.exists(speed_file):
      with open(speed_file, 'r') as f:
        speed = f.read().strip()
      if speed == "1000":
        return "1000 Mbps"
      elif speed == "100":
        return "100 Mbps"
      elif speed == "10":
        return "10 Mbps"
      else:
        return f"{speed} Mbps"
    else:
      return locales.get("unknown", "Unknown") if locales else "Unknown"
  except:
    return locales.get("unknown", "Unknown") if locales else "Unknown"

def get_lan_ethernet_speed(locales=None):
  """
  Get LAN ethernet interface speed in Mbps for eth1 (LAN port)
  Returns: string like "1000 Mbps", "100 Mbps", "Disconnected", or "Disabled"
  """
  try:
    # Check if LAN interface is enabled first
    if not is_lan_interface_enabled():
      return locales.get("disabled", "Disabled") if locales else "Disabled"
    
    # Check if eth1 interface exists and get its speed
    speed_file = "/sys/class/net/eth1/speed"
    carrier_file = "/sys/class/net/eth1/carrier"
    
    # Check if interface is physically connected
    if os.path.exists(carrier_file):
      with open(carrier_file, 'r') as f:
        carrier = f.read().strip()
      if carrier != "1":
        return locales.get("disconnected", "Disconnected") if locales else "Disconnected"
    else:
      return locales.get("disconnected", "Disconnected") if locales else "Disconnected"
    
    # Get interface speed
    if os.path.exists(speed_file):
      with open(speed_file, 'r') as f:
        speed = f.read().strip()
      if speed == "1000":
        return "1000 Mbps"
      elif speed == "100":
        return "100 Mbps"
      elif speed == "10":
        return "10 Mbps"
      else:
        return f"{speed} Mbps"
    else:
      return locales.get("unknown", "Unknown") if locales else "Unknown"
    
  except:
    return locales.get("unknown", "Unknown") if locales else "Unknown"

def find_br_lan_device_index():
  device_index = 0
  while True:
    try:
      device_name = uci_get(f"network.@device[{device_index}].name")
      if device_name == "br-lan":
        return device_index
      device_index += 1
    except:
      raise Exception("Failed to find br-lan device")

def is_wifi_interface_enabled(dev_name):
  if dev_name == "radio0":
    return is_onboard_wifi_enabled()
  elif dev_name == "radio1":
    return is_pcie_wifi_enabled()
  else:
    return False

def is_onboard_wifi_enabled():
  return _is_radio_enabled("radio0")

def is_pcie_wifi_enabled():
  return _is_radio_enabled("radio1")

def _is_radio_enabled(radio_name):
  """Check if a radio is enabled. On OpenWrt 26.04.1+, the 'disabled' UCI key
  may be absent when the radio is enabled, so we first try UCI and fall back
  to 'wifi status' JSON output."""
  try:
    val = uci_get(f"wireless.{radio_name}.disabled")
    if val == "1":
      return False
    if val == "0":
      return True
    # val is "" (key absent) – try wifi status for a definitive answer
    return _is_radio_up_via_wifi_status(radio_name)
  except:
    return True

def _is_radio_up_via_wifi_status(radio_name):
  """Use 'wifi status <radio>' to check if a radio is up (works on 26.04.1+)."""
  try:
    result = subprocess.run(
      ["wifi", "status", radio_name],
      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
      timeout=5, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
      status = json.loads(result.stdout)
      radio = status.get(radio_name, {})
      # 'disabled' key in wifi status is authoritative
      if radio.get("disabled", False):
        return False
      return radio.get("up", True)
  except Exception as e:
    pcat_logger.logger.debug(f"wifi status check failed for {radio_name}: {e}")
  # If wifi status fails, absent UCI key means not disabled = enabled
  return True


# LAN 接口是否开启, 处于关闭状态时，可以降低功耗增加续航
# 在设备中 eth1 是 LAN 网口
def is_lan_interface_enabled():
  try:
    device_index = find_br_lan_device_index()
    # Check if ports value is not empty
    ports = uci_get(f"network.@device[{device_index}].ports").strip()
    return ports != ""  # If not empty, LAN interface is enabled
  except ValueError as e:
    if "too many values to unpack" in str(e):
      pcat_logger.logger.error(f"Tuple unpacking error in is_lan_interface_enabled: {e}")
      import traceback
      pcat_logger.logger.error(f"Traceback: {traceback.format_exc()}")
    else:
      pcat_logger.logger.error(f"ValueError in is_lan_interface_enabled: {e}")
    return False
  except Exception as e:
    pcat_logger.logger.error(f"Error in is_lan_interface_enabled: {e}")
    return False

def enable_lan_interface(enable_flag, restart=False): 
  if enable_flag == is_lan_interface_enabled():
    pcat_logger.logger.info("Nothing changed for lan interface")
    return False
  pcat_logger.logger.info("Enable LAN interface: %s", enable_flag)

  try:
    device_index = find_br_lan_device_index()
    # Set the ports value based on enable_flag
    if enable_flag:
      uci_set(f"network.@device[{device_index}].ports", "eth1")
    else:
      uci_set(f"network.@device[{device_index}].ports", "")
    uci_commit("network")
    luci_reload()
    if restart:
      network_restart()
  except:
    pcat_logger.logger.error("Got error when config LAN interface")
    return True
  return True

# 开启 / 关闭 WAN 接口
def enable_wan_interface(enable_flag, restart=True):
  # Check if the interface state already matches the requested state
  old_flag = is_wan_interface_enabled()
  if old_flag == enable_flag:
    pcat_logger.logger.info("Nothing changed for wan interface")
    return False

  try:
    if enable_flag:
      uci_set("network.wan.disabled", "0")
      uci_set("network.wan6.disabled", "0")
    else:
      uci_set("network.wan.disabled", "1")
      uci_set("network.wan6.disabled", "1")

    uci_commit("network")
    luci_reload()
    if restart:
      network_restart()

  except:
    pcat_logger.logger.error("Got error when config wan interface")
    return True
  return True

_wwan_power_cache = {'value': True, 'time': 0}

def is_wwan_powered():
  current_time = time.time()
  if current_time - _wwan_power_cache['time'] < 2:
    return _wwan_power_cache['value']
  try:
    output = subprocess.check_output(["rfkill", "list", "wwan"], stderr=subprocess.DEVNULL).decode('utf-8')
    result = "Soft blocked: yes" not in output
    _wwan_power_cache['value'] = result
    _wwan_power_cache['time'] = current_time
    return result
  except Exception:
    _wwan_power_cache['value'] = True
    _wwan_power_cache['time'] = current_time
    return True

# 数据流量接口是否开启
def is_wwan_interface_enabled():
  try:
    # Check UCI configuration first - this reflects user intent
    lte_disabled = uci_get("network.wwan_lte.disabled")
    g5_disabled = uci_get("network.wwan_5g.disabled")
    fm350_disabled = uci_get("network.wwan_fm350.disabled")

    # If all are explicitly disabled, return False
    if lte_disabled == "1" and g5_disabled == "1" and fm350_disabled == "1":
      return False

    # If any interface is configured to be enabled, return True
    # This reflects the user's configuration choice regardless of connection status
    if lte_disabled == "0" or g5_disabled == "0" or fm350_disabled == "0":
      return True
      
    # Fallback to original runtime checks for compatibility
    try:
      status_json_4g = subprocess.check_output(["ifstatus", "wwan_lte"], stderr=subprocess.DEVNULL).decode('utf-8')
      info_4g = json.loads(status_json_4g)
      if info_4g.get("up", False):
        return True
    except:
      pass
    try:
      status_json_5g = subprocess.check_output(["ifstatus", "wwan_5g"], stderr=subprocess.DEVNULL).decode('utf-8')
      info_5g = json.loads(status_json_5g)
      if info_5g.get("up", False):
        return True
    except:
      pass
    try:
      status_json_fm350 = subprocess.check_output(["ifstatus", "wwan_fm350"], stderr=subprocess.DEVNULL).decode('utf-8')
      info_fm350 = json.loads(status_json_fm350)
      if info_fm350.get("up", False):
        return True
    except:
      pass

    try:
      ip_output = subprocess.check_output(["ip", "link", "show", "wwan0"], stderr=subprocess.DEVNULL).decode('utf-8')
      return ("state UP" in ip_output)
    except:
      pass

    return False

  except Exception:
    return False


# 开启 / 关闭数据流量接口
def enable_wwan_interface(enable_flag, restart=True):
  old_flag = is_wwan_interface_enabled()
  if old_flag == enable_flag:
    pcat_logger.logger.info("Nothing changed for wwan interface")
    return False

  try:
    if enable_flag:
      val = "0"
      status_val = "up"
    else:
      val = "1"
      status_val = "down"
      
    uci_set("network.wwan_5g.disabled", val)
    uci_set("network.wwan_5g_v6.disabled", val)
    uci_set("network.wwan_lte.disabled", val)
    uci_set("network.wwan_lte_v6.disabled", val)
    uci_set("network.wwan_fm350.disabled", val)

    #we try to {status_val} ubus call network.interface.wwan_5g and ubus call network.interface.wwan_lte
    _openwrt_ubus_call("network.interface.wwan_5g", status_val)
    _openwrt_ubus_call("network.interface.wwan_lte", status_val)
    _openwrt_ubus_call("network.interface.wwan_fm350", status_val)

    uci_commit("network")
    luci_reload()
    
    # Additional recovery mechanism for 4G/5G connection
    if enable_flag:
      pcat_logger.logger.info("Attempting to restart 4G/5G interfaces for recovery")
      # Give the system a moment to process the configuration changes
      time.sleep(1)
      
      # Try to restart the modem interfaces to ensure proper connection recovery
      try:
        subprocess.call(["ifdown", "wwan_5g"], timeout=10)
        subprocess.call(["ifup", "wwan_5g"], timeout=10)
      except:
        pass
        
      try:
        subprocess.call(["ifdown", "wwan_lte"], timeout=10)
        subprocess.call(["ifup", "wwan_lte"], timeout=10)
      except:
        pass

      try:
        subprocess.call(["ifdown", "wwan_fm350"], timeout=10)
        subprocess.call(["ifup", "wwan_fm350"], timeout=10)
      except:
        pass

    #network_restart()
  except Exception as e:
    pcat_logger.logger.error(f"Got error when config wwan interface: {e}")
    return False
  return True

# 开启 / 关闭数据流量接口, dev_name: radio0, radio1
def enable_wifi_interface(dev_name, enable_flag, restart=True):
  old_flag = is_wifi_interface_enabled(dev_name)
  if old_flag == enable_flag:
    pcat_logger.logger.info("Nothing changed for wifi interface")
    return False

  try:
    if enable_flag:
      uci_set(f"wireless.{dev_name}.disabled", "0")
    else:
      uci_set(f"wireless.{dev_name}.disabled", "1")
    uci_commit("wireless")
    if restart:
      luci_reload()
      wifi_reload()
  except:
    pcat_logger.logger.error(f"Got error when config wifi interface {dev_name}")
    return False
  return True


def set_network_mode_config(network_mode):
  if network_mode == "auto":
    modem_auto_stop_if_wired = 1
    wan_flag = True
    wwan_flag = True
  elif network_mode == "eth_only":
    modem_auto_stop_if_wired = 0
    wan_flag = True
    wwan_flag = False 
  elif network_mode == "cell_only":
    modem_auto_stop_if_wired = 0
    wan_flag = False
    wwan_flag = True
  elif network_mode == "all_off":
    modem_auto_stop_if_wired = 1
    wwan_flag = False
    wan_flag = False 
  else:
    modem_auto_stop_if_wired = 1  # Default to auto
    wan_flag = True
    wwan_flag = True
  return modem_auto_stop_if_wired, wan_flag, wwan_flag

def config_ethernet_wwan(network_mode):
  modem_auto_stop_if_wired, wan_flag, wwan_flag = set_network_mode_config(network_mode)
  changed_wan_flag = enable_wan_interface(wan_flag)
  changed_wwan_flag = enable_wwan_interface(wwan_flag)


def config_network_toggle(network_mode, wan_flag, wwan_flag, lan_flag, onboard_wifi_flag, pcie_wifi_flag):
  # Configure modem auto-stop setting based on network mode
  wan_flag = True
  wwan_flag = True
  if isinstance(network_mode, str):
    pcat_logger.logger.info("Configuring network mode: %s", network_mode)
    modem_auto_stop_if_wired, wan_flag, wwan_flag = set_network_mode_config(network_mode)
    # Send modem configuration via socket client
    try:
        socket_client.set_auto_wan(modem_auto_stop_if_wired)
    except Exception as e:
      pcat_logger.logger.warning("Failed to configure modem auto-stop setting: %s", e)
  
  changed_wan_flag = enable_wan_interface(wan_flag, restart=False)
  changed_lan_flag = enable_lan_interface(lan_flag, restart=False)
  changed_wwan_flag = enable_wwan_interface(wwan_flag)
  changed_onboard_wifi_flag = enable_wifi_interface("radio0", onboard_wifi_flag, restart=True)
  changed_pcie_wifi_flag = enable_wifi_interface("radio1", pcie_wifi_flag, restart=True)
  changed_brlan_flag = ensure_brlan_up(restart=False)
  if changed_wan_flag or changed_lan_flag or changed_brlan_flag:
    network_restart()

  # 🔍 Sanity checking: verify interface states match configuration
  try:
    operstate_eth0 = get_real_operstate("eth0")
    operstate_eth1 = get_real_operstate("eth1")
    operstate_brlan = get_real_operstate("br-lan")

    # 📡 Detect WiFi phy mapping (USB vs PCIe)
    usb_phy, pcie_phy = get_wifi_phy_mapping()

    # Check WiFi interfaces based on actual hardware mapping
    operstate_usb_wifi = "unknown"
    operstate_pcie_wifi = "unknown"

    if usb_phy:
      try:
        operstate_usb_wifi = get_real_operstate(f"{usb_phy}-ap0")
      except:
        operstate_usb_wifi = "unknown"

    if pcie_phy:
      try:
        operstate_pcie_wifi = get_real_operstate(f"{pcie_phy}-ap0")
      except:
        operstate_pcie_wifi = "unknown"

    try:
      operstate_wwan0 = get_real_operstate("wwan0")
    except:
      operstate_wwan0 = "unknown"

    # 📊 Log current interface states
    pcat_logger.logger.info("🔍 Interface states after configuration:")
    pcat_logger.logger.info(f"  📡 eth0 (WAN): {operstate_eth0} (expected: {'up' if wan_flag else 'down'})")
    pcat_logger.logger.info(f"  🏠 eth1 (LAN): {operstate_eth1} (expected: {'up' if lan_flag else 'down'})")
    pcat_logger.logger.info(f"  🌉 br-lan: {operstate_brlan} (expected: always up)")
    pcat_logger.logger.info(f"  📶 USB WiFi ({usb_phy}-ap0): {operstate_usb_wifi} (expected: {'up' if onboard_wifi_flag else 'down'})")
    pcat_logger.logger.info(f"  📶 PCIe WiFi ({pcie_phy}-ap0): {operstate_pcie_wifi} (expected: {'up' if pcie_wifi_flag else 'down'})")
    pcat_logger.logger.info(f"  📱 wwan0: {operstate_wwan0} (expected: {'up' if wwan_flag else 'down'})")

    # ✅ Check if states match expectations
    wan_ok = is_wan_interface_enabled() == wan_flag
    lan_ok = is_lan_interface_enabled() == lan_flag
    brlan_ok = (operstate_brlan == "up") # br-lan should always be up
    usb_wifi_ok = is_onboard_wifi_enabled() == onboard_wifi_flag
    pcie_wifi_ok = is_pcie_wifi_enabled() == pcie_wifi_flag
    wwan0_ok = is_wwan_interface_enabled() == wwan_flag

    # 🔄 Fallback checks using is_*_enabled functions for mismatched interfaces
    wan_fallback_ok = None
    lan_fallback_ok = None
    wwan_fallback_ok = None

    if not wan_ok:
      wan_fallback_ok = is_wan_interface_enabled() == wan_flag
      pcat_logger.logger.info(f"🔄 WAN fallback check: is_wan_interface_enabled()={is_wan_interface_enabled()}, expected={wan_flag}")

    if not lan_ok:
      lan_fallback_ok = is_lan_interface_enabled() == lan_flag
      pcat_logger.logger.info(f"🔄 LAN fallback check: is_lan_interface_enabled()={is_lan_interface_enabled()}, expected={lan_flag}")

    if not wwan0_ok:
      wwan_fallback_ok = is_wwan_interface_enabled() == wwan_flag
      pcat_logger.logger.info(f"🔄 WWAN fallback check: is_wwan_interface_enabled()={is_wwan_interface_enabled()}, expected={wwan_flag}")

    # 📊 Print summary table
    pcat_logger.logger.info("📊 Interface Validation Summary:")
    pcat_logger.logger.info("┌─────────────┬──────────┬──────────┬─────────┬─────────┐")
    pcat_logger.logger.info("│ Interface   │ Expected │ Operstate│ is_*_en │ CORRECT │")
    pcat_logger.logger.info("├─────────────┼──────────┼──────────┼─────────┼─────────┤")

    # Calculate CORRECT status for each interface
    wan_correct = wan_ok or (wan_fallback_ok if wan_fallback_ok is not None else False)
    lan_correct = lan_ok or (lan_fallback_ok if lan_fallback_ok is not None else False)
    brlan_correct = brlan_ok
    usb_wifi_correct = usb_wifi_ok
    pcie_wifi_correct = pcie_wifi_ok
    wwan_correct = wwan0_ok or (wwan_fallback_ok if wwan_fallback_ok is not None else False)

    # Get is_*_enabled function results
    wan_is_enabled = is_wan_interface_enabled()
    lan_is_enabled = is_lan_interface_enabled()
    brlan_is_enabled = is_brlan_up()
    usb_wifi_is_enabled = is_onboard_wifi_enabled()
    pcie_wifi_is_enabled = is_pcie_wifi_enabled()
    wwan_is_enabled = is_wwan_interface_enabled()

    # Format status with proper alignment (emojis display as 1 char but need 2 char padding)
    def format_status(status_bool, na_if_none=False):
        if na_if_none and status_bool is None:
            return 'N/A   '  # 6 chars total for alignment
        return ('✅   ' if status_bool else '❌   ')  # 5 chars total (emoji + 3 spaces)

    wan_is_status = format_status(wan_is_enabled)
    lan_is_status = format_status(lan_is_enabled)
    brlan_is_status = format_status(brlan_is_enabled)
    usb_wifi_is_status = format_status(usb_wifi_is_enabled)
    pcie_wifi_is_status = format_status(pcie_wifi_is_enabled)
    wwan_is_status = format_status(wwan_is_enabled)

    wan_correct_status = format_status(wan_correct)
    lan_correct_status = format_status(lan_correct)
    brlan_correct_status = format_status(brlan_correct)
    usb_wifi_correct_status = format_status(usb_wifi_correct)
    pcie_wifi_correct_status = format_status(pcie_wifi_correct)
    wwan_correct_status = format_status(wwan_correct)

    # Format interface names with detected phy mapping
    usb_wifi_name = f"{usb_phy or 'USB'}-ap0"[:11]  # Truncate to fit column
    pcie_wifi_name = f"{pcie_phy or 'PCIe'}-ap0"[:11]

    pcat_logger.logger.info(f"│ eth0 (WAN)  │ {'up' if wan_flag else 'down':8} │ {operstate_eth0:8} │ {wan_is_status} │ {wan_correct_status} │")
    pcat_logger.logger.info(f"│ eth1 (LAN)  │ {'up' if lan_flag else 'down':8} │ {operstate_eth1:8} │ {lan_is_status} │ {lan_correct_status} │")
    pcat_logger.logger.info(f"│ br-lan      │ {'up':8} │ {operstate_brlan:8} │ {brlan_is_status} │ {brlan_correct_status} │")
    pcat_logger.logger.info(f"│ {usb_wifi_name:11} │ {'up' if onboard_wifi_flag else 'down':8} │ {operstate_usb_wifi:8} │ {usb_wifi_is_status} │ {usb_wifi_correct_status} │")
    pcat_logger.logger.info(f"│ {pcie_wifi_name:11} │ {'up' if pcie_wifi_flag else 'down':8} │ {operstate_pcie_wifi:8} │ {pcie_wifi_is_status} │ {pcie_wifi_correct_status} │")
    pcat_logger.logger.info(f"│ wwan0       │ {'up' if wwan_flag else 'down':8} │ {operstate_wwan0:8} │ {wwan_is_status} │ {wwan_correct_status} │")
    pcat_logger.logger.info("└─────────────┴──────────┴──────────┴─────────┴─────────┘")

    # Final validation result
    all_operstate_ok = wan_ok and lan_ok and brlan_ok and usb_wifi_ok and pcie_wifi_ok and wwan0_ok
    fallback_fixes = (wan_fallback_ok if wan_fallback_ok is not None else True) and \
                    (lan_fallback_ok if lan_fallback_ok is not None else True) and \
                    (wwan_fallback_ok if wwan_fallback_ok is not None else True)

    all_correct = wan_correct and lan_correct and brlan_correct and usb_wifi_correct and pcie_wifi_correct and wwan_correct

    if all_operstate_ok:
      pcat_logger.logger.info("✅ All interface operstate checks passed!")
    elif all_correct:
      pcat_logger.logger.warning("⚠️ Some operstate checks failed, but all interfaces are correctly configured")
    elif fallback_fixes:
      pcat_logger.logger.warning("⚠️ Some operstate checks failed, but fallback UCI configuration checks passed")
    else:
      pcat_logger.logger.error("❌ Interface validation failed - configuration mismatch detected")



  except Exception as e:
    pcat_logger.logger.error(f"❌ Error during sanity checking: {e}")

def get_real_operstate(ifname):
  operstate_path = f"/sys/class/net/{ifname}/operstate"
  try:
    with open(operstate_path, "r") as f:
      return f.read().strip()
  except FileNotFoundError:
    # Interface folder doesn't exist, means interface is down/not present
    pcat_logger.logger.debug(f"Interface {ifname} not found in /sys/class/net/ - assuming down")
    return "down"
  except Exception as e:
    pcat_logger.logger.warning(f"Error reading operstate for {ifname}: {e}")
    return "unknown"

def get_wifi_phy_mapping():
  """
  Determine which phy (phy0 or phy1) corresponds to USB vs PCIe WiFi
  Returns: (usb_phy, pcie_phy) tuple
  """
  usb_phy = None
  pcie_phy = None

  try:
    for phy in ['phy0', 'phy1']:
      phy_path = f"/sys/class/ieee80211/{phy}"
      if os.path.exists(phy_path):
        # Read the symlink to get device path
        device_path = os.readlink(phy_path)
        pcat_logger.logger.debug(f"📡 {phy} device path: {device_path}")

        if 'usb' in device_path.lower():
          usb_phy = phy
          pcat_logger.logger.info(f"📶 Found USB WiFi: {phy}")
        elif 'pcie' in device_path.lower():
          pcie_phy = phy
          pcat_logger.logger.info(f"📶 Found PCIe WiFi: {phy}")

  except Exception as e:
    pcat_logger.logger.error(f"❌ Error detecting WiFi phy mapping: {e}")

  return usb_phy, pcie_phy

def is_brlan_up():
  """
  Check if br-lan interface is up using operstate
  Returns: True if br-lan is up, False otherwise
  """
  try:
    operstate = get_real_operstate("br-lan")
    is_up = operstate == "up"
    pcat_logger.logger.debug(f"br-lan operstate: {operstate}, is_up: {is_up}")
    return is_up
  except Exception as e:
    pcat_logger.logger.error(f"❌ Error checking br-lan status: {e}")
    return False


def ensure_brlan_up(restart=True):
  incorrect_setting = False
  try:
    # Check if br-lan interface exists and bring it up if needed
    result = subprocess.run(["ip", "link", "show", "br-lan"], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
      # Check if interface is up
      if "UP" not in result.stdout:
        # Bring the interface up
        subprocess.run(["ip", "link", "set", "br-lan", "up"], capture_output=True, timeout=5)
        incorrect_setting = True
        if restart:
          network_restart()
        pcat_logger.logger.info("Brought br-lan interface up")
      else:
        pcat_logger.logger.debug("br-lan interface already up")
    else:
      pcat_logger.logger.warning("br-lan interface not found")
  except subprocess.TimeoutExpired:
    pcat_logger.logger.error("Timeout ensuring br-lan interface is up")
  except Exception as e:
    pcat_logger.logger.error("Error ensuring br-lan interface is up: %s", e)
  return incorrect_setting

# 本地包 (pcat 开头的和 quectel-cm) 版本信息
def pcat_package_version_info():
  try:
    cmdout = subprocess.check_output("opkg list-installed | grep \"pcat-\|quectel-cm\"", shell=True)
  except subprocess.CalledProcessError:
    print("Warning: opkg command not found!")
    return []

  cmdout_a = cmdout.split(b'\n')
  packages = []
  for line in cmdout_a:
    if line == b'':
      continue
    name, version = line.split(b' - ')
    packages.append({"name": name.decode("utf-8"), "version": version.decode("utf-8")})
  return packages

# eg: b'pcat-manager_1.0.2-0_aarch64_cortex-a53.ipk\npcat-manager-web_1.0.2-0_aarch64_cortex-a53.ipk\n'
def remote_pcat_package_version_info():
  try:
    remote_version_info = requests.get(pcat_config.package_version_url).content
  except:
    print("Warning: Failed to get version info!")
    return []

  info_a = remote_version_info.split(b'\n')
  version_list = []
  for line in info_a:
    if line != b'':
      version = line.split(b'_')[1]
      name = line.split(b'_')[0].decode("utf-8")
      if name.startswith("pcat-") or name.startswith("quectel-cm"):
        version_list.append({"name":name, "full_name":line.decode("utf-8"), "version":version.decode("utf-8")})
  print("Got remote version info", version_list)
  return version_list

def _get_wan_user_agent():
  """Generic user agent without hardware identifiers."""
  try:
    board_name_raw = open("/proc/device-tree/compatible", "r").read().strip()
    hdwr = "pcat2" if "photonicat2" in board_name_raw else "pcat"
  except:
    hdwr = "pcat"
  return hdwr + "/" + pcat_config.version

def get_wan_ip():
  url_list = oem_config.get_ip_check_urls()
  if not url_list:
    url_list = ["https://api.ipify.org", "https://ifconfig.me/ip"]
  wan_ip = ""
  headers = {'User-Agent': _get_wan_user_agent()}
  for url in url_list:
    pcat_logger.logger.info("Fetch wan ip from " + url)
    try:
      r = requests.get(url, headers=headers, timeout=8, verify=True)
      if r.ok:
        wan_ip = r.content.decode("utf-8").strip()
        pcat_logger.logger.info("Get response from " + url + ":" + wan_ip)
        break
    except:
      try:
        r = requests.get(url, headers=headers, timeout=8, verify=False)
        if r.ok:
          wan_ip = r.content.decode("utf-8").strip()
          pcat_logger.logger.info("Get response from " + url + ":" + wan_ip)
          break
      except:
        pass
      pcat_logger.logger.warning("Failed to get wan ip from url: %s", url)
  return wan_ip

def get_wan_ipv6():
  url = oem_config.get_ipv6_check_url()
  if not url:
    return ""
  try:
    r = requests.get(url, timeout=8, verify=True)
    if r.ok:
      ipv6 = r.content.decode("utf-8").strip()
      if ":" in ipv6:
        pcat_logger.logger.info("Get IPv6 from " + url + ": " + ipv6)
        return ipv6
  except:
    pass
  try:
    r = requests.get(url, timeout=8, verify=False)
    if r.ok:
      ipv6 = r.content.decode("utf-8").strip()
      if ":" in ipv6:
        pcat_logger.logger.info("Get IPv6 from " + url + ": " + ipv6)
        return ipv6
  except:
    pass
  pcat_logger.logger.warning("Failed to get WAN IPv6 from: %s", url)
  return ""

def get_local_wan_ip():
  """Get the local WAN interface IP address from OpenWrt"""
  wan_ip = ""
  try:
    result = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
      for line in result.stdout.strip().split('\n'):
        if 'default via' in line:
          parts = line.split()
          if 'dev' in parts:
            dev_index = parts.index('dev')
            if dev_index + 1 < len(parts):
              wan_interface = parts[dev_index + 1]
              wan_ip_result = subprocess.run(['ip', 'addr', 'show', wan_interface], capture_output=True, text=True, timeout=5)
              if wan_ip_result.returncode == 0:
                for ip_line in wan_ip_result.stdout.split('\n'):
                  if 'inet ' in ip_line and not '127.0.0.1' in ip_line:
                    wan_ip = ip_line.strip().split()[1].split('/')[0]
                    break
              break
  except Exception as e:
    pcat_logger.logger.warning(f"Failed to get local WAN IP: {e}")
    
  if not wan_ip:
    wan_ip = _openwrt_uci_get("network.wan.ipaddr")
  
  return wan_ip

def _get_openwrt_version():
  rev = ""
  commit = ""
  try:
    with open("/etc/openwrt_release", "r") as f:
      for line in f:
        m = re.match(r"DISTRIB_REVISION='(.+)'", line)
        if m:
          rev = m.group(1)
          break
  except FileNotFoundError:
    pass

  try:
    with open("/etc/openwrt_version", "r") as f:
      commit = f.read().strip()
  except FileNotFoundError:
    pass

  parts = []
  if rev:
    parts.append(rev)
  if commit:
    parts.append(commit)
    
  return " / ".join(parts)

class App (threading.Thread):

  def __init__(self):
    threading.Thread.__init__(self)

    # Clean temp directory everytime when bootup
    current_working_path = pathlib.Path(__file__).parent.resolve()
    save_dir_path = current_working_path.joinpath("temp")
    shutil.rmtree(save_dir_path, ignore_errors=True)

    self.board_info = self.get_board_info()
    # Patch for displaying kernel build date additionally for kernel version
    board_info = self.board_info
    kernel_ver = subprocess.check_output(["uname", "-r"], encoding="utf-8").strip() 
    kernel_date_str = subprocess.check_output(["uname", "-v"], encoding="utf-8").strip() 
    #raw = "#0 SMP PREEMPT Wed Jul  2 09:59:17 2025", I want 6.12.28 - Jul 2 2025
    parts = kernel_date_str.split()
    kernel_date = parts[4] + " " + parts[5] + " " + parts[7]
    
    board_info["kernel"] = kernel_ver + " - " + kernel_date
    self.board_info = board_info
    self.tick_count = 0
    self.network_stats = network_stats.NetworkStats(pcat_config.net_stats_db)
    self.net_download_speed = 0
    self.net_upload_speed = 0
    self.system_status = 0 # 0 => normal, 20 => prepare_rebooting, 25 => rebooting
    self.wan_ip = ""
    self.wan_ipv6 = ""
    self.local_wan_ip = ""
    board_info["openwrt_version"]  = _get_openwrt_version()

    # Idle shutdown monitoring
    self.idle_shutdown_enabled = pcat_config.get_user_config_key("idle_shutdown_enabled", False)
    self.idle_shutdown_timeout = pcat_config.get_user_config_key("idle_shutdown_timeout", 10) * 60  # Convert to seconds
    self.idle_shutdown_battery_only = pcat_config.get_user_config_key("idle_shutdown_battery_only", False)
    self.idle_start_time = None  # When idle period started
    self.last_wifi_client_count = 0
    self.idle_status = "unknown"  # "active", "idle", or "unknown"
    self.idle_remaining_seconds = 0

    # Low battery shutdown monitoring
    self.low_battery_shutdown_enabled = pcat_config.get_user_config_key("low_battery_shutdown_enabled", False)
    self.low_battery_threshold = pcat_config.get_user_config_key("low_battery_threshold", 20)  # percentage
    self.low_battery_initial_percent = None  # battery level at boot, to skip if already low
    self.low_battery_status = "unknown"  # "ok", "low", "disabled", "ignored"

    # Power dip tracking
    self.last_battery_voltage = None
    self.last_charger_connected = None  # None = unknown, True/False = DC state
    self._system_start_logged = False
    self._initial_battery_recorded = False

  def _update_wan_ip(self):
    try:
      result = get_wan_ip()
      if result:
        self.wan_ip = result
    except Exception as e:
      pcat_logger.logger.warning(f"Background WAN IP update failed: {e}")

  def _update_wan_ipv6(self):
    try:
      result = get_wan_ipv6()
      if result:
        self.wan_ipv6 = result
    except Exception as e:
      pcat_logger.logger.warning(f"Background WAN IPv6 update failed: {e}")

  def get_wifi_client_count(self):
    """Get the total number of connected WiFi clients across all interfaces"""
    try:
      return command_parser.get_wifi_clients_count()
    except Exception as e:
      pcat_logger.logger.warning(f"Failed to get WiFi client count: {e}")
      return 0

  def check_idle_shutdown(self):
    """Check idle status and trigger shutdown if needed (OR mode with other power modes)"""
    # Reload settings in case they changed
    self.idle_shutdown_enabled = pcat_config.get_user_config_key("idle_shutdown_enabled", False)
    self.idle_shutdown_timeout = pcat_config.get_user_config_key("idle_shutdown_timeout", 10) * 60
    self.idle_shutdown_battery_only = pcat_config.get_user_config_key("idle_shutdown_battery_only", False)

    if not self.idle_shutdown_enabled:
      self.idle_status = "disabled"
      self.idle_remaining_seconds = 0
      return

    # If battery_only mode, skip when charger is connected
    if self.idle_shutdown_battery_only:
      charger_voltage_mv = socket_client.charger_voltage
      charger_v = charger_voltage_mv / 1000.0 if charger_voltage_mv and charger_voltage_mv > 100 else (charger_voltage_mv or 0)
      if charger_v >= 1.0:
        # Charger is connected, reset idle timer and skip
        self.idle_status = "active"
        self.idle_start_time = None
        self.idle_remaining_seconds = self.idle_shutdown_timeout
        return

    client_count = self.get_wifi_client_count()
    current_time = time.time()

    if client_count > 0:
      # Clients connected - reset idle timer
      if self.idle_status == "idle" and self.idle_start_time is not None:
        power_event_log.log_event("idle_countdown_reset", f"WiFi client(s) reconnected ({client_count})", extra={
          "client_count": client_count,
        })
      self.idle_status = "active"
      self.idle_start_time = None
      self.idle_remaining_seconds = self.idle_shutdown_timeout
      self.last_wifi_client_count = client_count
    else:
      # No clients connected
      if self.idle_start_time is None:
        # Start idle timer
        self.idle_start_time = current_time
        self.idle_status = "idle"
        pcat_logger.logger.info("Idle shutdown: No WiFi clients detected, starting countdown")
        power_event_log.log_event("idle_countdown_started", "No WiFi clients, countdown started", extra={
          "timeout_minutes": round(self.idle_shutdown_timeout / 60, 1),
        })

      # Calculate remaining time
      elapsed = current_time - self.idle_start_time
      self.idle_remaining_seconds = max(0, self.idle_shutdown_timeout - elapsed)

      # Check if timeout reached
      if elapsed >= self.idle_shutdown_timeout:
        pcat_logger.logger.warning(f"Idle shutdown triggered: No WiFi clients for {self.idle_shutdown_timeout/60:.1f} minutes")
        power_event_log.log_event("idle_shutdown_triggered", f"No WiFi clients for {self.idle_shutdown_timeout/60:.1f} minutes", extra={
          "timeout_minutes": round(self.idle_shutdown_timeout / 60, 1),
        })
        # Trigger shutdown (use poweroff for OpenWrt compatibility)
        try:
          subprocess.run(["poweroff"], check=True)
        except Exception as e:
          pcat_logger.logger.error(f"Failed to trigger idle shutdown: {e}")

  def reset_idle_monitoring(self):
    """Reset idle monitoring state (called when settings change)"""
    self.idle_start_time = None
    self.idle_status = "unknown"
    self.idle_remaining_seconds = 0
    pcat_logger.logger.info("Idle monitoring reset")

  def check_low_battery_shutdown(self):
    """Check battery level and trigger shutdown if it drops below threshold (OR mode with other power modes)"""
    self.low_battery_shutdown_enabled = pcat_config.get_user_config_key("low_battery_shutdown_enabled", False)
    self.low_battery_threshold = pcat_config.get_user_config_key("low_battery_threshold", 20)

    if not self.low_battery_shutdown_enabled:
      self.low_battery_status = "disabled"
      return

    charge_percent = socket_client.charge_percent
    if charge_percent is None:
      return

    # Record initial battery level on first valid reading
    # Use a 2% tolerance above threshold to account for SOC estimation jumps after reboot
    boot_tolerance = 2
    if self.low_battery_initial_percent is None:
      self.low_battery_initial_percent = charge_percent
      if charge_percent <= self.low_battery_threshold + boot_tolerance:
        # Device powered on at or near threshold - ignore to avoid immediate shutdown
        self.low_battery_status = "ignored"
        pcat_logger.logger.info(f"Low battery shutdown: Boot battery level {charge_percent}% <= threshold {self.low_battery_threshold}% + {boot_tolerance}% tolerance, ignoring until it rises above threshold")
        return

    # If we were ignoring because boot level was low, check if battery has risen above threshold + tolerance
    if self.low_battery_status == "ignored":
      if charge_percent > self.low_battery_threshold + boot_tolerance:
        # Battery has clearly risen above threshold, start monitoring normally
        self.low_battery_status = "ok"
        pcat_logger.logger.info(f"Low battery shutdown: Battery rose to {charge_percent}%, now monitoring normally")
      else:
        return  # Still ignoring

    if charge_percent > self.low_battery_threshold:
      self.low_battery_status = "ok"
    else:
      # Battery has dipped below threshold
      self.low_battery_status = "low"
      pcat_logger.logger.warning(f"Low battery shutdown triggered: Battery {charge_percent}% <= threshold {self.low_battery_threshold}%")
      power_event_log.log_event("low_battery_shutdown_triggered", f"Battery {charge_percent}% <= {self.low_battery_threshold}%", extra={
        "battery_percent": charge_percent,
        "threshold": self.low_battery_threshold,
      })
      try:
        subprocess.run(["poweroff"], check=True)
      except Exception as e:
        pcat_logger.logger.error(f"Failed to trigger low battery shutdown: {e}")

  def reset_low_battery_monitoring(self):
    """Reset low battery monitoring state (called when settings change)"""
    self.low_battery_initial_percent = None
    self.low_battery_status = "unknown"
    pcat_logger.logger.info("Low battery monitoring reset")

  def check_power_dip(self):
    """Detect significant voltage drops and log them as power dip events."""
    try:
      voltage_mv = socket_client.battery_voltage
      charger_voltage_mv = socket_client.charger_voltage
      if voltage_mv is None:
        return

      voltage_v = voltage_mv / 1000.0 if voltage_mv > 100 else voltage_mv
      charger_v = charger_voltage_mv / 1000.0 if charger_voltage_mv and charger_voltage_mv > 100 else (charger_voltage_mv or 0)

      if self.last_battery_voltage is not None:
        last_v = self.last_battery_voltage
        drop = last_v - voltage_v
        # Log if voltage dropped more than 0.3V in 30 seconds
        if drop > 0.3:
          power_event_log.log_event("power_dip", f"Voltage drop {last_v:.2f}V -> {voltage_v:.2f}V ({drop:.2f}V)", extra={
            "voltage_before": round(last_v, 2),
            "voltage_after": round(voltage_v, 2),
            "drop": round(drop, 2),
            "charger_voltage": round(charger_v, 2),
          })

      # Detect DC charger plug/unplug transitions
      charger_connected = charger_v >= 1.0
      if self.last_charger_connected is not None and charger_connected != self.last_charger_connected:
        if charger_connected:
          power_event_log.log_event("dc_plugged_in", f"DC power connected ({charger_v:.2f}V)", extra={
            "charger_voltage": round(charger_v, 2),
          })
        else:
          power_event_log.log_event("dc_unplugged", f"DC power disconnected ({charger_v:.2f}V)", extra={
            "charger_voltage": round(charger_v, 2),
          })
      self.last_charger_connected = charger_connected

      self.last_battery_voltage = voltage_v
    except Exception:
      pass

  def run(self):
    pcat_logger.logger.info("App thread run.")
    global modem_client
    threading.Timer(3.0, lambda: modem_client.start_query("all")).start()
    while True:
      # Log system_start and initial battery record once battery data is available
      if not self._system_start_logged:
        if socket_client.battery_voltage is not None:
          self._system_start_logged = True
          power_event_log.log_event("system_start", "System started", extra={
            "idle_shutdown_enabled": self.idle_shutdown_enabled,
            "low_battery_shutdown_enabled": self.low_battery_shutdown_enabled,
          })
        elif self.tick_count >= 30:
          self._system_start_logged = True
          power_event_log.log_event("system_start", "System started (no battery data)", extra={
            "idle_shutdown_enabled": self.idle_shutdown_enabled,
            "low_battery_shutdown_enabled": self.low_battery_shutdown_enabled,
          })

      # Record first battery history entry as soon as data is available
      if not self._initial_battery_recorded and socket_client.battery_voltage is not None:
        self._initial_battery_recorded = True
        try:
          v = socket_client.battery_voltage
          c = socket_client.battery_current
          soc = socket_client.charge_percent
          charging = socket_client.on_charging
          v_val = v / 1000.0 if v > 100 else v
          c_val = c if c is not None else 0
          temp = socket_client.board_temperature
          pcat_logger.logger.info(f"Initial battery record: v={v_val} c={c_val} soc={soc} charging={charging} temp={temp}")
          battery_history.record(soc, round(v_val, 3), round(c_val, 0), charging, temperature=temp)
        except Exception as e:
          pcat_logger.logger.error(f"Initial battery record error: {e}")

      # Retry board_info if it was empty at startup (ubus may not have been ready)
      if self.tick_count % 5 == 0 and not self.board_info.get("hostname"):
        info = self.get_board_info()
        if info.get("hostname"):
          info["kernel"] = self.board_info.get("kernel", info.get("kernel", ""))
          info["openwrt_version"] = self.board_info.get("openwrt_version", info.get("openwrt_version", ""))
          self.board_info = info

      # Fast queries for unknown values (every 3s until discovered)
      if self.tick_count % 3 == 0:
        if socket_client.stm32_firmware_version is None:
          socket_client.query_stm32_version()
        if socket_client.modem_apn is None or socket_client.device_type == "none":
          socket_client.modem_apn_get()

      # Routine device status queries (every 10s)
      if self.tick_count % 10 == 0:
        if socket_client.stm32_firmware_exist():
          socket_client.query_schedule_power_events()
          socket_client.query_charger_on_auto_start()
          socket_client.query_power_on_mode()
          socket_client.charge_threshold_get()
          socket_client.query_pmu_status()
        socket_client.query_network_route_mode_status()
        socket_client.query_modem_status()

      if self.tick_count % 60 == 0:
        modem_client.start_query("")
      if self.wan_ip == "":
        if self.tick_count % 30 == 0:
          threading.Thread(target=self._update_wan_ip, daemon=True).start()
      else:
        if self.tick_count % 600 == 0:
          threading.Thread(target=self._update_wan_ip, daemon=True).start()

      if self.wan_ipv6 == "":
        if self.tick_count % 30 == 0:
          threading.Thread(target=self._update_wan_ipv6, daemon=True).start()
      else:
        if self.tick_count % 600 == 0:
          threading.Thread(target=self._update_wan_ipv6, daemon=True).start()

      if self.tick_count % 30 == 0:
        self.local_wan_ip = get_local_wan_ip()

      if self.tick_count % 30 == 0:
        self.network_stats.log_stats()

      # Smart fan control moved to timer manager (1-minute interval)

      # Check idle shutdown every 30 seconds
      if self.tick_count % 30 == 0:
        self.check_idle_shutdown()

      # Check low battery shutdown every 30 seconds
      if self.tick_count % 30 == 0:
        self.check_low_battery_shutdown()

      # Check for power dip (voltage drop) every 30 seconds
      if self.tick_count % 30 == 0:
        self.check_power_dip()

      # Record battery history every 5 minutes
      if self.tick_count % 300 == 0:
        try:
          v = socket_client.battery_voltage
          c = socket_client.battery_current
          soc = socket_client.charge_percent
          charging = socket_client.on_charging
          pcat_logger.logger.info(f"Battery history tick={self.tick_count}: v={v} c={c} soc={soc} charging={charging}")
          if v is not None:
            v_val = v / 1000.0 if v > 100 else v
            c_val = c if c is not None else 0
            temp = socket_client.board_temperature
            battery_history.record(soc, round(v_val, 3), round(c_val, 0), charging, temperature=temp)
          else:
            pcat_logger.logger.warning("Battery history skipped: voltage is None")
        except Exception as e:
          pcat_logger.logger.error(f"Battery history record error: {e}")

      # Cleanup old battery history once a day
      if self.tick_count % 86400 == 0:
        battery_history.cleanup()

      self.net_download_speed = self.network_stats.log_rx_speed()
      self.net_upload_speed = self.network_stats.log_tx_speed()
      self.tick_count += 1
      time.sleep(1)

  def get_board_info(self):
    """    
    return {'kernel': '4.19.193-photonicat', 'hostname': 'OpenWrt', 'system': 'ARMv8 Processor rev 0', 
      'model': 'Rockchip RK3568 photonicat Linux Board', 'board_name': 'rockchip,rk3568-photonicat', 
      'rootfs_type': 'squashfs', 'release': {'distribution': 'OpenWrt', 'version': '21.02', 
      'target': 'armvirt/64', 'revision': 'R21.8.6', 'description': 'OpenWrt '}}
    """

    return _openwrt_ubus_call("system", "board")

  def get_system_info(self):
    """
    return {'localtime': 1654875214, 'uptime': 160914, 'load': [8800, 16416, 19680],
      'memory': {'total': 951328768, 'free': 581607424, 'shared': 8155136, 'buffered': 60932096,
      'available': 757010432, 'cached': 136847360}, 'swap': {'total': 475000832, 'free': 475000832}}
    """
    return _openwrt_ubus_call("system", "info")

  def get_dhcp_clients_count(self):
    try:
      with open('/tmp/dhcp.leases', 'r') as f:
        return sum(1 for line in f if line.strip())
    except FileNotFoundError:
      return 0
    except Exception:
      return 0

  def get_clients(self):
    mock = False
    mock_clients_data = {'freq': 2457, 'clients': {'7c:03:ab:48:31:6c': {'auth': True, 'assoc': True, 'authorized': True, 'preauth': False, 'wds': False, 
      'wmm': True, 'ht': True, 'vht': False, 'he': False, 'wps': False, 'mfp': False, 'rrm': [0, 0, 0, 0, 0], 'aid': 2, 'bytes':
       {'rx': 22556457, 'tx': 318005089}, 'airtime': {'rx': 0, 'tx': 460871086}, 'packets': {'rx': 158290, 'tx': 245375}, 'rate': 
       {'rx': 1000, 'tx': 1000}, 'signal': -41, 'capabilities': {}}, 'e4:42:a6:56:4e:e7': {'auth': True, 'assoc': True, 'authorized': True, 
       'preauth': False, 'wds': False, 'wmm': True, 'ht': True, 'vht': False, 'he': False, 'wps': False, 'mfp': False, 'rrm': [0, 0, 0, 0, 0], 
       'aid': 1, 'bytes': {'rx': 4549039, 'tx': 120524910}, 'airtime': {'rx': 0, 'tx': 174426269}, 'packets': {'rx': 41293, 'tx': 91143}, 
       'rate': {'rx': 1000, 'tx': 1000}, 'signal': -49, 'capabilities': {}}}}
    def _get_hostapd_clients(iface_names):
      for iface in iface_names:
        result = _openwrt_ubus_call(iface, "get_clients")
        if result and "clients" in result:
          return list(result["clients"].keys())
      return []

    clients_mac = []
    if mock:
      cmd_json = mock_clients_data
      clients_mac = list(cmd_json["clients"].keys())
    else:
      clients_mac += _get_hostapd_clients(["hostapd.wlan0", "hostapd.wlan0-1", "hostapd.phy0-ap0"])
      clients_mac += _get_hostapd_clients(["hostapd.wlan1", "hostapd.wlan1-1", "hostapd.phy1-ap0"])

    dhcp_info = []
    dhch_leases_file = '/tmp/dhcp.leases'
    if mock:
      dhch_leases_file = "/tmp/dhcp_mock_data.txt"

    """
    1654879269 7c:03:ab:48:31:6c 172.16.0.177 RedmiNote7Pro-qichun 01:7c:03:ab:48:31:6c
    1654880275 e4:42:a6:56:4e:e7 172.16.0.178 c2h2hp1070 *
    """

    try:
      dhcp_file = open(dhch_leases_file, 'r')
      dhcp_info = dhcp_file.readlines()
      dhcp_file.close()
    except Exception as e:
      print("Failed to read /tmp/dhcp.leases", e)

    response_json = {}
    response_json["devices"] = []
    for client_mac in clients_mac:

      device = {}
      device["mac"] = client_mac
      for line_item in dhcp_info:
        host_info = line_item.split(" ")
        if len(host_info) < 4:
          break
        if host_info[1] == client_mac:
          device["ip"] = host_info[2]
          device["name"] = host_info[3]
          break
      response_json["devices"].append(device)

    return response_json
