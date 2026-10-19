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
import ipaddress
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
from pcat_util import lan_clients
from pcat_util import sys_stats
from pcat_util import pkg_format
from network_stats import get_sta_devices as _get_sta_devices


socket_client = None
modem_client = None
flask_app = None
main_app = None # Potin to App instance
timer_manager = None

# Timestamp of the last client HTTP request, set by a Flask before_request hook.
# The App loop uses it to poll the STM32 for live SoC/route/modem state at the
# fast 10s cadence only while a client (the browser or the always-on mini
# display, both HTTP-GET pollers) is actually watching, and fall back to a
# slower cadence when the device is truly headless. Plain float assignment is
# GIL-atomic across the Flask and App threads; no lock needed.
_last_web_activity = 0.0

def note_web_activity():
  global _last_web_activity
  _last_web_activity = time.time()

def web_active_within(seconds):
  """True if a client made an HTTP request within the last `seconds`."""
  return (time.time() - _last_web_activity) < seconds

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
  
# NVMe SSD in the M.2 slot: presence, model and capacity.
def nvme_status(disk="nvme0n1"):
  """Report the fitted NVMe SSD as {present, model, size, free}.

  Capacity comes from the block device (sysfs reports 512-byte sectors
  regardless of the drive's native block size), so it is known even when the
  disk is unpartitioned or unmounted. Free space needs a mounted filesystem
  and stays None otherwise — an unmounted disk has no meaningful "free", and
  reporting 0 would read as "full". Sizes are raw bytes; the UI formats them.
  """
  status = {"present": False, "model": "", "size": None, "free": None}
  sysfs = "/sys/block/{0}".format(disk)
  if not os.path.exists(sysfs):
    return status
  status["present"] = True
  try:
    with open("{0}/device/model".format(sysfs)) as f:
      status["model"] = f.read().strip()
  except OSError:
    pass
  try:
    with open("{0}/size".format(sysfs)) as f:
      status["size"] = int(f.read().strip()) * 512
  except (OSError, ValueError):
    pass
  # Sum the mounted partitions of this disk, so a multi-partition drive is
  # not reported as only its first slice.
  free = None
  try:
    with open("/proc/mounts") as f:
      for line in f:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/{0}".format(disk)):
          continue
        try:
          st = os.statvfs(parts[1])
        except OSError:
          continue
        free = (free or 0) + st.f_bavail * st.f_frsize
  except OSError:
    pass
  status["free"] = free
  return status

def sd_card_info(disk="mmcblk1"):
  """Report the SD card as {present, model, size, free}, mirroring
  nvme_status(). Model comes from the MMC CID product name (device/name);
  free space needs a mounted filesystem and stays None otherwise."""
  status = {"present": False, "model": "", "size": None, "free": None}
  sysfs = "/sys/block/{0}".format(disk)
  if not os.path.exists(sysfs):
    return status
  status["present"] = True
  try:
    with open("{0}/device/name".format(sysfs)) as f:
      status["model"] = f.read().strip()
  except OSError:
    pass
  try:
    with open("{0}/size".format(sysfs)) as f:
      status["size"] = int(f.read().strip()) * 512
  except (OSError, ValueError):
    pass
  free = None
  try:
    with open("/proc/mounts") as f:
      for line in f:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/{0}".format(disk)):
          continue
        try:
          st = os.statvfs(parts[1])
        except OSError:
          continue
        free = (free or 0) + st.f_bavail * st.f_frsize
  except OSError:
    pass
  status["free"] = free
  return status

def _next_pow2(value):
  size = 1
  while size < value:
    size *= 2
  return size

def hw_variant():
  """RAM + eMMC marketing sizes as e.g. "4+64GB" for the model row.
  RAM rounds up in binary GiB (MemTotal 15.6 GiB → 16); storage rounds up
  in the decimal GB it is sold in (125e9 B → 128). Empty when unreadable."""
  try:
    ram_bytes = 0
    with open("/proc/meminfo") as f:
      for line in f:
        if line.startswith("MemTotal:"):
          ram_bytes = int(line.split()[1]) * 1024
          break
    with open("/sys/block/mmcblk0/size") as f:
      emmc_bytes = int(f.read().strip()) * 512
    if not ram_bytes or not emmc_bytes:
      return ""
    return "{0}+{1}GB".format(_next_pow2(ram_bytes / 2**30),
                              _next_pow2(emmc_bytes / 1e9))
  except (OSError, ValueError):
    return ""

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

_is_photonicat2_cache = None

def is_photonicat2():
  """Check if device is Photonicat 2 based on device tree compatible string"""
  global _is_photonicat2_cache
  if _is_photonicat2_cache is None:
    try:
      board_name_raw = open("/proc/device-tree/compatible", "r").read().strip()
      _is_photonicat2_cache = "photonicat2" in board_name_raw
    except (FileNotFoundError, PermissionError):
      _is_photonicat2_cache = False
  return _is_photonicat2_cache

def battery_history_extras(socket_client):
  """(current_mA, temperature) to store with a battery-history row.

  Both come from Photonicat 2 hardware only: the fuel gauge's current_now in
  sysfs and the MCU's board temperature. A Photonicat 1 has no current sensor
  (the socket client reads None) and its MCU reports a constant 0 for the
  temperature, so storing "0" there would draw flat, fake power and
  temperature lines on the history chart. Store NULL instead.
  """
  if not is_photonicat2():
    return None, None
  # Photonicat 2 keeps its long-standing behaviour: a failed gauge read is
  # recorded as 0 mA, exactly as before the Photonicat 1 split.
  c = socket_client.battery_current
  return round(c if c is not None else 0, 0), socket_client.board_temperature


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

# ── remaining-time settling ──────────────────────────────────────────────────
# power_now swings wildly for several seconds either side of a DC plug/unplug:
# the rail collapses or surges, the charger negotiates, and the gauge has not
# yet caught up. An estimate taken from those samples is nonsense -- "0:10 left"
# on a full battery, or "9:40 to full" a second after plugging in. Hold the
# estimate back until the new direction has been steady for this long, and keep
# showing "-" (unknown) until it has.
_SETTLE_SECONDS = 15

# Direction ("charging"/"discharging") and the monotonic timestamp at which it
# was first seen. Rebuilt from scratch on any direction change.
_battery_dir_state = {"direction": None, "since": 0.0}
_battery_dir_lock = threading.Lock()


def _battery_direction_settled(is_charging):
    """True once the charge direction has held steady for _SETTLE_SECONDS.

    Called from the dashboard request handler, so the sample rate follows the
    browser's poll rather than a fixed loop -- hence wall-clock timing rather
    than a sample count. time.monotonic() so a clock step (NTP settling at
    boot, which is exactly when a device is likely to be plugged in) cannot
    make the window elapse instantly or never.
    """
    direction = "charging" if is_charging else "discharging"
    now = time.monotonic()
    with _battery_dir_lock:
        if _battery_dir_state["direction"] != direction:
            # Plug, unplug, or a load swing that flipped the sign: restart the
            # window. Whatever the previous estimate was, it described a state
            # the device is no longer in.
            _battery_dir_state["direction"] = direction
            _battery_dir_state["since"] = now
            return False
        return (now - _battery_dir_state["since"]) >= _SETTLE_SECONDS


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

        # Suppress the estimate until the direction has been steady for
        # _SETTLE_SECONDS. This must run on every call, settled or not, so the
        # window keeps tracking; the return below is what withholds the number.
        settled = _battery_direction_settled(is_charging)
        if not settled:
            return {
                "remaining_time_hours": None,
                "remaining_time": "-",
                "settling": True,
            }

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
from pcat_util.openwrt import uci_show_dict as _openwrt_uci_show_dict
from pcat_util import wifi_wan
from pcat_util import wifi_radio
from pcat_util import wan_manager
from pcat_util import nas_manager

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
  # Not shipped on LuCI-less builds. A missing binary must count as a failed
  # reload, not an exception: enable_wifi_interface() calls this right before
  # wifi_reload() inside one try block, and the raise was eating the actual
  # apply -- uci said disabled, the AP kept broadcasting.
  try:
    result = subprocess.run(["luci-reload"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  except OSError:
    return 127
  return result.returncode

def network_restart():
  result = subprocess.run(["/etc/init.d/network", "restart"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  exit_code = result.returncode
  if exit_code != 0:
    # Suppress error message for failed operations
    pass
  return exit_code

def wifi_reload():
  try:
    result = subprocess.run(["/sbin/wifi", "reload"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  except OSError:
    return 127
  return result.returncode

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

# Which interface actually carries the default route (the "active WAN").
# network_route_mode only says wired/mobile and can't tell WAN from LAN when
# both ethernet ports have carrier, so the status bar lit up the underline on
# both. Resolve the real egress from the lowest-metric default route's device:
#   eth0/ppp* -> "wan", eth1 -> "lan", phy*-sta* -> "wifi",
#   anything else (eth2/wwan/usb*) -> "mobile".
# Returns "" when there is no default route.
#
# Memoized with a short TTL: this is called on hot paths -- /api/v1/wan_priority
# .json (device_control polls every 5s), the network-interfaces regen, and the
# 30s WAN-IP keeper -- and each call forked `ip route`. The egress class only
# changes on a failover (a seconds-scale event) and every consumer already
# tolerates >=5s staleness (the dashboard wraps it in a 5s cache), so a 4s memo
# collapses the forks with no user-visible lag. Failover itself is driven by
# kernel metrics + the reconcile keepers, not by this display value.
_active_egress_cache = {"value": "", "time": 0.0}
_ACTIVE_EGRESS_TTL = 4.0

def get_active_egress():
  now = time.time()
  if now - _active_egress_cache["time"] < _ACTIVE_EGRESS_TTL:
    return _active_egress_cache["value"]
  value = _compute_active_egress()
  _active_egress_cache["value"] = value
  _active_egress_cache["time"] = now
  return value

def _egress_kind_for_dev(dev):
  """Map a default-route netdev to the UI/egress slot id, or "" if unknown."""
  if not dev:
    return ""
  if dev == "eth0":
    return "wan"
  if dev == "eth1":
    return "lan"
  # Smart WAN upstream STA netdev (e.g. phy0-sta0)
  if dev.startswith("phy") and "-sta" in dev:
    return "wifi"
  # PPPoE over the wired WAN (pppoe-wan). It is still the WAN slot: without
  # this it fell through to the "mobile" catch-all below and the UI showed
  # the cellular row as Active while the wired link was actually carrying
  # all the traffic (and the modem was down with no carrier at all).
  if dev.startswith("ppp"):
    return "wan"
  return "mobile"


def _route_link_usable(dev):
  """False when the route's L2 link is known-dead (cable out). Demoted eth
  routes often linger until netifd tears them down; treating them as active
  keeps the green 🟢 on ethernet and leaves a stale public IP after unplug."""
  if not dev:
    return False
  if dev == "eth0" or dev.startswith("ppp"):
    return does_wan_have_carrier()
  if dev == "eth1":
    return does_lan_have_carrier()
  return True


def _compute_active_egress():
  try:
    out = subprocess.run(
      ["ip", "-j", "route", "show", "default"],
      capture_output=True, text=True, timeout=3).stdout.strip()
    routes = json.loads(out) if out else []
    if not routes:
      return ""
    # lowest metric wins (absent metric == 0, i.e. highest priority); skip
    # routes on a known-dead ethernet link so unplug immediately demotes the
    # UI to the next live path (or "" if nothing else is up).
    routes = sorted(routes, key=lambda r: r.get("metric", 0))
    for route in routes:
      dev = route.get("dev", "")
      if not _route_link_usable(dev):
        continue
      kind = _egress_kind_for_dev(dev)
      if kind:
        return kind
    return ""
  except Exception as e:
    pcat_logger.logger.debug(f"get_active_egress failed: {e}")
    return ""


def invalidate_active_egress_cache():
  """Drop the short-TTL egress memo so the next reader sees a fresh route."""
  _active_egress_cache["time"] = 0.0


# Every netdev carrying a default route, lowest metric first (pppoe-wan, wwan0,
# phy1-sta0, ...). Same `ip -j route` source as get_active_egress(), but the
# device names rather than the UI slot, for the live throughput chart on
# /stats. Memoized with the same short TTL: that chart polls every second and
# must not fork `ip route` each time.
_default_route_cache = {"value": [], "time": 0.0}

def get_default_route_devs():
  now = time.time()
  if now - _default_route_cache["time"] < _ACTIVE_EGRESS_TTL:
    return _default_route_cache["value"]
  devs = []
  try:
    out = subprocess.run(
      ["ip", "-j", "route", "show", "default"],
      capture_output=True, text=True, timeout=3).stdout.strip()
    routes = json.loads(out) if out else []
    for route in sorted(routes, key=lambda r: r.get("metric", 0)):
      dev = route.get("dev", "")
      if dev and dev not in devs:
        devs.append(dev)
  except Exception as e:
    pcat_logger.logger.debug(f"get_default_route_devs failed: {e}")
  _default_route_cache["value"] = devs
  _default_route_cache["time"] = now
  return devs


def _read_netdev_counter(dev, direction):
  """/sys/class/net/<dev>/statistics/<rx|tx>_bytes as int, 0 when unreadable
  (device gone between listing and reading)."""
  try:
    with open("/sys/class/net/{0}/statistics/{1}_bytes".format(dev, direction)) as f:
      return int(f.read().strip())
  except (OSError, ValueError):
    return 0

# WAN 接口是否开启
def is_wan_interface_enabled():
  try:
    # Check UCI disabled flag - this reflects user intent
    net = _openwrt_uci_show_dict("network")
    wan_disabled = net.get("network.wan.disabled", "")
    wan6_disabled = net.get("network.wan6.disabled", "")

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
  net = _openwrt_uci_show_dict("network")
  device_index = 0
  while True:
    key = f"network.@device[{device_index}].name"
    if key not in net:
      raise Exception("Failed to find br-lan device")
    if net[key] == "br-lan":
      return device_index
    device_index += 1

def is_wifi_interface_enabled(dev_name):
  if dev_name in ("radio0", "radio1"):
    return _is_radio_enabled(dev_name)
  return False

def _radio_for_class(cls):
  """radioN for a physical card ('onboard'/'pcie'), matched by device path:
  radio numbering is not stable across boots on this hardware, and driving
  the Device Control toggles by index can point 'Onboard WiFi' at the PCIe
  card."""
  try:
    return wifi_wan._radio_by_class(cls)
  except Exception:
    return None

def is_onboard_wifi_enabled():
  return _is_radio_enabled(_radio_for_class("onboard") or "radio0")

def is_pcie_wifi_enabled():
  return _is_radio_enabled(_radio_for_class("pcie") or "radio1")

def _is_radio_enabled(radio_name):
  """Check if a radio is enabled. On OpenWrt 26.04.1+, the 'disabled' UCI key
  may be absent when the radio is enabled, so we first try UCI and fall back
  to wireless status."""
  # While Smart WAN is active the STA radio's own AP is parked (the radio is
  # up, but serving the upstream uplink, not clients). Report it as disabled
  # so the UI and the device screen present it consistently.
  try:
    if wifi_wan.active_sta_radio() == radio_name:
      return False
  except Exception:
    pass
  try:
    wireless = _openwrt_uci_show_dict("wireless")
    val1 = wireless.get(f"wireless.{radio_name}.disabled", "")
    val2 = wireless.get(f"wireless.default_{radio_name}.disabled", "")
    if val1 == "1" or val2 == "1":
      return False
    if val1 == "0" and val2 == "0":
      return True
    # val1 or val2 is "" (key absent) – try wireless status for a definitive answer
    return _is_radio_up_via_wifi_status(radio_name)
  except:
    return True

def _is_radio_up_via_wifi_status(radio_name):
  """Check if a radio is up via ubus network.wireless status (works on
  26.04.1+). Same data the 'wifi status' CLI prints, without forking it."""
  try:
    status = _openwrt_ubus_call("network.wireless", "status")
    radio = status.get(radio_name, {}) if status else {}
    if radio:
      # 'disabled' key in wireless status is authoritative
      interfaces = radio.get("interfaces", []) if radio else []
      if len(interfaces) == 0:
         return False
      if radio.get("disabled", False):
        return False
      return radio.get("up", True)
  except Exception as e:
    pcat_logger.logger.debug(f"wireless status check failed for {radio_name}: {e}")
  # If wireless status fails, absent UCI key means not disabled = enabled
  return True


# LAN 接口是否开启, 处于关闭状态时，可以降低功耗增加续航
# 在设备中 eth1 是 LAN 网口
def is_lan_interface_enabled():
  try:
    device_index = find_br_lan_device_index()
    # Check if ports value is not empty
    net = _openwrt_uci_show_dict("network")
    ports = net.get(f"network.@device[{device_index}].ports", "").strip()
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

# NIC power policy - three states per port, backed by the ethpower package.
#
# LAN "off" keeps its old meaning (the port leaves br-lan) and now also parks
# the PHY, which the old boolean never did: clearing network.@device[N].ports
# takes eth1 out of the bridge but leaves it administratively up, so it carries
# on autonegotiating into a dead cable. That costs a measured 273 mW per port on
# photonicat2, against a ~5.0 W idle draw, so "off" used to save nothing at all.
#
#   on   - PHY always powered, a cable is seen instantly. With ALDPS enabled in
#          the DTS the PHY still power-saves itself between link-pulse bursts,
#          so this now costs only ~33 mW/port over a parked PHY. Default.
#   auto - PHY parked and re-probed on a backoff while the unit is on battery.
#          Predates ALDPS, when it was worth ~273 mW/port; now it buys that last
#          ~33 mW and pays up to max_backoff (120s) of a dark, blind port. On the
#          adapter this behaves exactly like "on".
#   off  - PHY held down; no cable detection at all (LAN also leaves br-lan)
#
# The daemon reads a per-port policy (policy_eth0 / policy_eth1) and falls back
# to the global "policy" option, which is what the selector wrote before the
# WAN port got its own control.
LAN_MODES = ("auto", "on", "off")
ETH_WAN_PORT = "eth0"
ETH_LAN_PORT = "eth1"

def _ethpower_policy(port=None):
  try:
    if port:
      v = (uci_get(f"ethpower.@ethpower[0].policy_{port}") or "").strip()
      if v in LAN_MODES:
        return v
    v = (uci_get("ethpower.@ethpower[0].policy") or "").strip()
    # Fall back to the package default, which is "on" now that ALDPS does the
    # saving in hardware - not "auto", which would report a parking policy the
    # daemon is not actually running.
    return v if v in LAN_MODES else "on"
  except Exception:
    return "on"

def _set_ethpower_policy(port, mode):
  try:
    if _ethpower_policy(port) == mode:
      return False
    uci_set(f"ethpower.@ethpower[0].policy_{port}", mode)
    uci_commit("ethpower")
    subprocess.run(["/etc/init.d/eth-powersave", "reload"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pcat_logger.logger.info("%s power policy -> %s", port, mode)
    return True
  except Exception as e:
    pcat_logger.logger.error(f"Failed to set ethpower policy for {port}: {e}")
    return False

def get_lan_mode():
  try:
    if not is_lan_interface_enabled():
      return "off"
    policy = _ethpower_policy(ETH_LAN_PORT)
    # Bridged but the daemon says "off" is contradictory; treat the bridge as
    # authoritative and report the saving policy we are actually running.
    return "on" if policy == "off" else policy
  except Exception as e:
    pcat_logger.logger.error(f"Error in get_lan_mode: {e}")
    return "unknown"

def set_lan_mode(mode, restart=False):
  if mode not in LAN_MODES:
    pcat_logger.logger.error("Unknown LAN mode: %s", mode)
    return False

  changed = enable_lan_interface(mode != "off", restart=restart)
  return _set_ethpower_policy(ETH_LAN_PORT, mode) or changed

# The WAN NIC selector only governs PHY power. Whether eth0 is used for uplink
# belongs to the priority table (wan_manager owns network.wan.disabled), so
# "off" here parks the PHY - to the routing layer that just looks like an
# unplugged cable, which the failover logic already handles.
def get_wan_mode():
  try:
    return _ethpower_policy(ETH_WAN_PORT)
  except Exception as e:
    pcat_logger.logger.error(f"Error in get_wan_mode: {e}")
    return "unknown"

def set_wan_mode(mode):
  if mode not in LAN_MODES:
    pcat_logger.logger.error("Unknown WAN mode: %s", mode)
    return False

  prev = _ethpower_policy(ETH_WAN_PORT)
  changed = _set_ethpower_policy(ETH_WAN_PORT, mode)

  # A parked port cannot carry the uplink, so "off" also drops the wan slot
  # from the priority table - remembering whether it was enabled, so leaving
  # "off" puts the slot back the way the user had it. The marker lives in the
  # ethpower config because it describes this selector's doing, not the
  # user's own table edits.
  try:
    if mode == "off" and prev != "off":
      was_enabled = wan_manager.is_slot_enabled("wan")
      uci_set("ethpower.@ethpower[0].wan_slot_restore", "1" if was_enabled else "0")
      uci_commit("ethpower")
      if was_enabled:
        wan_manager.set_slot_enabled("wan", False)
        pcat_logger.logger.info("WAN NIC off: wan slot disabled in priority table")
        changed = True
    elif mode != "off" and prev == "off":
      marker = (uci_get("ethpower.@ethpower[0].wan_slot_restore") or "").strip()
      if marker == "1":
        wan_manager.set_slot_enabled("wan", True)
        pcat_logger.logger.info("WAN NIC %s: wan slot restored in priority table", mode)
        changed = True
      if marker:
        uci_set("ethpower.@ethpower[0].wan_slot_restore", "")
        uci_commit("ethpower")
  except Exception as e:
    pcat_logger.logger.error(f"Failed to couple wan slot to WAN NIC mode: {e}")

  return changed

# ── legacy "WAN off" upgrade migration ───────────────────────────────────────
# Older builds expressed "WAN off" as network.wan.disabled=1 (the interface
# toggle / cell-only mode). This build expresses it as the NIC power policy
# plus the priority table's wan slot, and neither reads the old flag: an
# upgraded box would show the NIC as Auto and seed the wan slot enabled, so
# the first table apply would silently turn a deliberately-off WAN back on.
# Map the old state into the new model exactly once. Boxes that had WAN on
# fall through untouched and read as Auto (the unset-policy default), and the
# marker blocks any re-run, so every choice made on this build — On or Off —
# sticks.
_NIC_OFF_MIGRATED_MARKER = "wan_nic_off_migrated"

def migrate_legacy_wan_off():
  try:
    if pcat_config.get_user_config_key(_NIC_OFF_MIGRATED_MARKER):
      return False
    pcat_config.set_user_config_key(_NIC_OFF_MIGRATED_MARKER, True)
    if (uci_get(f"ethpower.@ethpower[0].policy_{ETH_WAN_PORT}") or "").strip() in LAN_MODES:
      return False  # this build's selector already wrote a choice; honor it
    if is_wan_interface_enabled():
      return False  # WAN was on: the unset policy reads as Auto, as intended
    # WAN was off on the old build. Disable the wan slot in the stored table
    # first — that is what keeps a later apply() from re-enabling the
    # interface — then park the NIC selector on Off, with the same restore
    # marker set_wan_mode() uses so manually leaving Off brings the slot back.
    priority = wan_manager.get_priority()
    for entry in priority:
      if entry["id"] == "wan":
        entry["enabled"] = False
    pcat_config.set_user_config_key("wan_priority", priority)
    if uci_get("ethpower.@ethpower[0]"):
      _set_ethpower_policy(ETH_WAN_PORT, "off")
      uci_set("ethpower.@ethpower[0].wan_slot_restore", "1")
      uci_commit("ethpower")
    pcat_logger.logger.info(
      "migrated legacy WAN-off: wan slot disabled, NIC mode off")
    return True
  except Exception as e:
    pcat_logger.logger.error(f"legacy WAN-off migration failed: {e}")
    return False

def kick_eth_powersave():
  """Ask the daemon to probe the ports right now.

  Called when the user opens the network page: that is a strong hint a cable is
  about to go in, and collapsing the backoff there keeps the UI feeling immediate
  without raising the steady-state duty cycle."""
  try:
    with open("/var/run/eth-powersave.kick", "w"):
      pass
  except Exception:
    pass

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
_wwan_rfkill_paths = None  # sysfs nodes of wwan rfkill devices, discovered once

def _find_wwan_rfkill_paths():
  global _wwan_rfkill_paths
  if _wwan_rfkill_paths is not None:
    return _wwan_rfkill_paths
  paths = []
  try:
    for entry in os.listdir("/sys/class/rfkill"):
      base = os.path.join("/sys/class/rfkill", entry)
      try:
        with open(os.path.join(base, "type")) as f:
          if f.read().strip() == "wwan":
            paths.append(base)
      except OSError:
        continue
  except OSError:
    pass
  # Only cache a successful discovery; a USB modem may enumerate after boot.
  if paths:
    _wwan_rfkill_paths = paths
  return paths

def is_wwan_powered():
  # The modem thread polls this every second; reading sysfs directly avoids
  # forking 'rfkill' on each poll (~0.5 forks/sec on the ARM target).
  global _wwan_rfkill_paths
  paths = _find_wwan_rfkill_paths()
  if paths:
    try:
      for base in paths:
        with open(os.path.join(base, "soft")) as f:
          if f.read().strip() == "1":
            return False
      return True
    except OSError:
      _wwan_rfkill_paths = None  # device went away, rediscover next call

  # Fallback for systems without /sys/class/rfkill: fork rfkill, cached
  current_time = time.time()
  if current_time - _wwan_power_cache['time'] < 10:
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
    net = _openwrt_uci_show_dict("network")
    lte_disabled = net.get("network.wwan_lte.disabled", "")
    g5_disabled = net.get("network.wwan_5g.disabled", "")
    fm350_disabled = net.get("network.wwan_fm350.disabled", "")

    # If all are explicitly disabled, return False
    if lte_disabled == "1" and g5_disabled == "1" and fm350_disabled == "1":
      return False

    # If any interface is configured to be enabled, return True
    # This reflects the user's configuration choice regardless of connection status
    if lte_disabled == "0" or g5_disabled == "0" or fm350_disabled == "0":
      return True

    # Fallback to runtime checks for compatibility (same data as 'ifstatus',
    # via the persistent ubus connection instead of forking a process)
    for wwan_iface in ("wwan_lte", "wwan_5g", "wwan_fm350"):
      try:
        info = _openwrt_ubus_call(f"network.interface.{wwan_iface}", "status")
        if info and info.get("up", False):
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
    
    # Additional recovery mechanism for 4G/5G connection.
    #
    # The three interface pairs are independent, so they are bounced
    # concurrently rather than one after another: serially this cost up to
    # 6 x 10s of subprocess timeouts, which on a booting modem (where ifdown
    # routinely hits its timeout) blocked the caller for the better part of a
    # minute. There is also no settle sleep before the bounce -- uci_commit()
    # above has already returned by the time netifd is asked to act, and
    # ifdown/ifup serialise on netifd's own lock regardless.
    if enable_flag:
      pcat_logger.logger.info("Attempting to restart 4G/5G interfaces for recovery")

      def _bounce(iface):
        for action in ("ifdown", "ifup"):
          try:
            subprocess.call([action, iface], timeout=10)
          except Exception:
            pass

      bouncers = [threading.Thread(target=_bounce, args=(i,), daemon=True)
                  for i in ("wwan_5g", "wwan_lte", "wwan_fm350")]
      for t in bouncers:
        t.start()
      for t in bouncers:
        t.join(timeout=25)

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

  # Remember the choice before acting on it. The boot-time reconcile switches
  # a radio back on when it cannot tell whether anyone ever wanted it off (see
  # pcat_util/wifi_radio.py); without this record, turning a radio off here
  # would silently come undone at the next reboot.
  try:
    wifi_radio.record_intent(dev_name, enable_flag)
  except Exception as e:
    pcat_logger.logger.warning(f"Could not record wifi intent for {dev_name}: {e}")

  try:
    if enable_flag:
      uci_set(f"wireless.{dev_name}.disabled", "0")
      uci_set(f"wireless.default_{dev_name}.disabled", "0")
    else:
      uci_set(f"wireless.{dev_name}.disabled", "1")
      uci_set(f"wireless.default_{dev_name}.disabled", "1")
    uci_commit("wireless")
    if restart:
      luci_reload()
      wifi_reload()
  except Exception as e:
    pcat_logger.logger.error(f"Got error when config wifi interface {dev_name}: {e}")
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
  elif network_mode == "smart_wan":
    # Failover chain: Ethernet -> upstream WiFi (route metric 5) -> cellular.
    # Both eth and cellular stay enabled; metrics pick the egress.
    modem_auto_stop_if_wired = 1
    wan_flag = True
    wwan_flag = True
  elif network_mode == "all_off":
    modem_auto_stop_if_wired = 1
    wwan_flag = False
    wan_flag = False
  elif network_mode == "priority":
    # WAN priority table: wan_manager owns interface state; keep the daemon
    # from auto-stopping the modem so the table's ordering always holds.
    modem_auto_stop_if_wired = 0
    wan_flag = True
    wwan_flag = True
  else:
    modem_auto_stop_if_wired = 1  # Default to auto
    wan_flag = True
    wwan_flag = True
  return modem_auto_stop_if_wired, wan_flag, wwan_flag

def config_ethernet_wwan(network_mode, at_boot=False):
  """Push the saved network mode back onto the interfaces.

  at_boot suppresses the WAN network restart. OpenWrt's own init has just
  brought the network up moments earlier, so restarting it here only tears
  down an upstream that was already coming up -- most painfully a WiFi STA
  relay, which then has to re-associate and re-DHCP from scratch. The
  enable_* helpers no-op when the live state already matches, so on a normal
  boot this whole call is a couple of uci reads.

  In priority mode the table owns network.wan/wan6 and the modem interfaces --
  wan_manager.apply() writes their disabled flags from the slot enables -- so
  this path must keep its hands off them, exactly as config_network_toggle()
  already does. set_network_mode_config() reports BOTH uplinks as enabled for
  'priority' (it has to: the daemon must not stop the modem when the table
  ranks cellular above ethernet), so running the toggles anyway re-enabled a
  slot the user had deliberately turned off -- silently, on every boot, and
  racing the App thread's wan_manager.startup(). A cellular-only box came back
  from a reboot routing out ethernet again.
  """
  if network_mode == "priority":
    return
  modem_auto_stop_if_wired, wan_flag, wwan_flag = set_network_mode_config(network_mode)
  changed_wan_flag = enable_wan_interface(wan_flag, restart=not at_boot)
  changed_wwan_flag = enable_wwan_interface(wwan_flag)


def config_network_toggle(network_mode, wan_flag, wwan_flag, lan_flag, onboard_wifi_flag, pcie_wifi_flag):
  # Configure modem auto-stop setting based on network mode
  wan_flag = True
  wwan_flag = True
  # In priority mode the table (wan_manager.apply) owns WAN/cellular/STA
  # state; this toggle path only handles the LAN NIC and the AP radios.
  priority_mode = network_mode == "priority"
  if isinstance(network_mode, str):
    pcat_logger.logger.info("Configuring network mode: %s", network_mode)
    if priority_mode:
      # The daemon must not down modem interfaces behind the table's back
      # (the user may rank cellular above ethernet).
      modem_auto_stop_if_wired = 0
    else:
      modem_auto_stop_if_wired, wan_flag, wwan_flag = set_network_mode_config(network_mode)
    # Send modem configuration via socket client
    try:
        socket_client.set_auto_wan(modem_auto_stop_if_wired)
    except Exception as e:
      pcat_logger.logger.warning("Failed to configure modem auto-stop setting: %s", e)

  # Smart WAN: the onboard radio relays an upstream hotspot as a STA client,
  # so it must stay powered; in every other legacy mode the relay STA is
  # parked and the onboard AP (band/channel included) is restored from backup.
  if isinstance(network_mode, str) and not priority_mode:
    try:
      if network_mode == "smart_wan":
        onboard_wifi_flag = True
        wifi_wan.set_sta_enabled(True)
      else:
        wifi_wan.set_sta_enabled(False)
    except Exception as e:
      pcat_logger.logger.error("Failed to toggle Smart WAN STA: %s", e)

  # The radio carrying the upstream STA must not have its AP unparked (or the
  # whole radio disabled) by the AP toggles while it is relaying.
  sta_radio = None
  try:
    sta_radio = wifi_wan.active_sta_radio()
  except Exception:
    sta_radio = None
  changed_wan_flag = False if priority_mode else enable_wan_interface(wan_flag, restart=False)
  changed_lan_flag = enable_lan_interface(lan_flag, restart=False)
  changed_wwan_flag = False if priority_mode else enable_wwan_interface(wwan_flag)
  onboard_radio = _radio_for_class("onboard") or "radio0"
  pcie_radio = _radio_for_class("pcie") or "radio1"
  changed_onboard_wifi_flag = False if sta_radio == onboard_radio else enable_wifi_interface(onboard_radio, onboard_wifi_flag, restart=True)
  changed_pcie_wifi_flag = False if sta_radio == pcie_radio else enable_wifi_interface(pcie_radio, pcie_wifi_flag, restart=True)
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
def _is_pcat_package(name):
  return name.startswith("pcat-") or name.startswith("quectel-cm")

def pcat_package_version_info():
  # opkg on 26.04 images, apk on master images; see pcat_util/pkg_format.py.
  try:
    cmdout = subprocess.check_output(pkg_format.list_installed_command(),
                                     stderr=subprocess.DEVNULL, timeout=30)
  except (OSError, subprocess.SubprocessError):
    print("Warning: failed to list installed packages!")
    return []

  return [{"name": name, "version": version}
          for name, version in pkg_format.parse_installed(cmdout.decode("utf-8", "replace"))
          if _is_pcat_package(name)]

# One package file name per line, in the device's format, e.g.
#   ipk: pcat-manager_1.0.2-0_aarch64_cortex-a53.ipk
#   apk: pcat-manager-2.0.0-r63.apk
# Lines in the other format are skipped: this firmware cannot install them.
def remote_pcat_package_version_info():
  try:
    # Timeout is mandatory: this runs on the Flask request thread for
    # /pkg_update, and without it a hung/black-holed server would block that
    # worker forever (connect + read), wedging the page.
    remote_version_info = pkg_format.http_get(pcat_config.package_version_url,
                                              timeout=15).content
  except:
    print("Warning: Failed to get version info!")
    return []

  version_list = []
  for line in remote_version_info.decode("utf-8", "replace").splitlines():
    parsed = pkg_format.parse_package_file_name(line)
    if parsed and _is_pcat_package(parsed[0]):
      version_list.append({"name": parsed[0], "full_name": line.strip(), "version": parsed[1]})
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

def _parse_ip_response(text, parser):
  """Extract an IP from a response body using a parser spec. Mirrors the Go
  display's custom-metrics parser: "stdout" (default, whole trimmed body),
  "json:path.to.key" (dotted path, numeric segments index into arrays),
  "regex:pattern" (first capture group), or "line:N" (0-based line)."""
  text = (text or "").strip()
  if not parser or parser == "stdout":
    return text
  if parser.startswith("json:"):
    path = parser[len("json:"):]
    cur = json.loads(text)
    for key in path.split("."):
      if isinstance(cur, list):
        cur = cur[int(key)]
      elif isinstance(cur, dict):
        cur = cur[key]
      else:
        raise ValueError("cannot traverse path at %r" % key)
    return str(cur).strip()
  if parser.startswith("regex:"):
    m = re.search(parser[len("regex:"):], text)
    if m and m.groups():
      return m.group(1).strip()
    raise ValueError("no regex match")
  if parser.startswith("line:"):
    n = int(parser[len("line:"):])
    return text.split("\n")[n].strip()
  raise ValueError("unknown parser: %s" % parser)

def _get_public_ip_lookup_config():
  cfg = pcat_config.get_user_config_key("public_ip_lookup", None)
  return cfg if isinstance(cfg, dict) else {}

def _normalize_ip_sources(raw):
  """Coerce a config list into [{"url":..., "parser":...}], accepting either
  plain URL strings or {url, parser} dicts."""
  sources = []
  for item in raw or []:
    if isinstance(item, dict):
      url = (item.get("url") or "").strip()
      parser = (item.get("parser") or "stdout").strip() or "stdout"
    else:
      url = str(item).strip()
      parser = "stdout"
    if url:
      sources.append({"url": url, "parser": parser})
  return sources

def _fetch_public_ip(sources, headers, want_v6):
  """Try each source in order; return the first body that parses to a valid IP
  of the requested family, else ""."""
  for src in sources:
    url = src["url"]
    parser = src.get("parser", "stdout")
    pcat_logger.logger.info("Fetch wan ip from " + url)
    for verify in (True, False):
      try:
        r = requests.get(url, headers=headers, timeout=8, verify=verify)
      except Exception:
        continue
      if not r.ok:
        continue
      try:
        value = _parse_ip_response(r.content.decode("utf-8", "replace"), parser)
        ip_obj = ipaddress.ip_address(value)
      except Exception as e:
        pcat_logger.logger.warning("Parse/validate failed for %s (parser=%s): %s", url, parser, e)
        break  # body fetched but unusable; move to next source
      if (want_v6 and ip_obj.version != 6) or (not want_v6 and ip_obj.version != 4):
        pcat_logger.logger.warning("Got wrong IP family from %s: %s", url, value)
        break
      pcat_logger.logger.info("Get response from " + url + ":" + value)
      return value
    # all verify attempts failed, or the response was unusable: try next source
  pcat_logger.logger.warning("Failed to get wan ip (want_v6=%s) from all sources", want_v6)
  return ""

def get_wan_ip():
  cfg = _get_public_ip_lookup_config()
  sources = _normalize_ip_sources(cfg.get("ipv4"))
  user_agent = (cfg.get("user_agent") or "").strip()
  if not sources:
    # Fall back to OEM-provisioned URLs, then built-in public services.
    oem_urls = oem_config.get_ip_check_urls()
    if not oem_urls:
      oem_urls = ["https://api.ipify.org", "https://ifconfig.me/ip"]
    sources = _normalize_ip_sources(oem_urls)
  headers = {'User-Agent': user_agent or _get_wan_user_agent()}
  return _fetch_public_ip(sources, headers, False)

def _probe_wan_ipv6(host="6.photonicat.com"):
  """Global IPv6 the router actually sources traffic from, discovered by
  route selection toward our IPv6-only site (UDP connect sends nothing).
  Returns "" when there is no global IPv6 (link-local/ULA don't count)."""
  import socket
  try:
    infos = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_DGRAM)
    if not infos:
      return ""
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
      s.settimeout(3)
      s.connect(infos[0][4][:2])
      src = s.getsockname()[0]
    finally:
      s.close()
    low = src.lower()
    if low.startswith("fe80") or low.startswith("fc") or low.startswith("fd"):
      return ""
    return src
  except OSError:
    return ""

def get_wan_ipv6():
  cfg = _get_public_ip_lookup_config()
  sources = _normalize_ip_sources(cfg.get("ipv6"))
  user_agent = (cfg.get("user_agent") or "").strip()
  if not sources:
    url = oem_config.get_ipv6_check_url()
    if url:
      sources = _normalize_ip_sources([url])
  if sources:
    headers = {'User-Agent': user_agent or _get_wan_user_agent()}
    return _fetch_public_ip(sources, headers, True)
  # No lookup service configured: derive the address locally via routing.
  return _probe_wan_ipv6()

def get_local_wan_ip():
  """Get the local WAN interface IP address from OpenWrt.

  Only returns an address from a *usable* default-route device (carrier up for
  eth). Does not fall back to UCI `network.wan.ipaddr`: that value is a
  configured/static leftover and made the connection card keep showing a WAN
  IP after the cable was unplugged.
  """
  wan_ip = ""
  try:
    result = subprocess.run(['ip', 'route', 'show', 'default'], capture_output=True, text=True, timeout=5)
    if result.returncode == 0:
      # Prefer lowest-metric usable route (same idea as get_active_egress).
      candidates = []
      for line in result.stdout.strip().split('\n'):
        if 'default via' not in line and not line.startswith('default '):
          continue
        parts = line.split()
        if 'dev' not in parts:
          continue
        dev_index = parts.index('dev')
        if dev_index + 1 >= len(parts):
          continue
        dev = parts[dev_index + 1]
        metric = 0
        if 'metric' in parts:
          try:
            metric = int(parts[parts.index('metric') + 1])
          except (ValueError, IndexError):
            metric = 0
        candidates.append((metric, dev))
      candidates.sort(key=lambda x: x[0])
      for _metric, wan_interface in candidates:
        if not _route_link_usable(wan_interface):
          continue
        wan_ip_result = subprocess.run(
          ['ip', 'addr', 'show', wan_interface],
          capture_output=True, text=True, timeout=5)
        if wan_ip_result.returncode != 0:
          continue
        for ip_line in wan_ip_result.stdout.split('\n'):
          if 'inet ' in ip_line and '127.0.0.1' not in ip_line:
            wan_ip = ip_line.strip().split()[1].split('/')[0]
            break
        if wan_ip:
          break
  except Exception as e:
    pcat_logger.logger.warning(f"Failed to get local WAN IP: {e}")

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
    self._wifi_wan_reconciling = False
    self._wifi_wan_last_result = None
    self._wan_manager_reconciling = False
    self._ap_keeper_running = False
    self._nas_reconciling = False
    self.network_stats = network_stats.NetworkStats(pcat_config.net_stats_db)
    self.net_download_speed = 0
    self.net_upload_speed = 0
    self.system_status = 0 # 0 => normal, 20 => prepare_rebooting, 25 => rebooting
    self.wan_ip = ""
    self.wan_ipv6 = ""
    self.local_wan_ip = ""
    # Public-IP probe backoff (seconds until next attempt doubles on failure,
    # capped at 10min; reset when a default route [re]appears or on success)
    self._wan_ip_backoff = 30
    self._wan_ip_next_try = 0
    self._wan_ipv6_backoff = 30
    self._wan_ipv6_next_try = 0
    self._had_default_route = False
    # Last egress class used for public-IP tracking; a change (failover)
    # forces a re-probe so the displayed IP follows the new uplink.
    self._wan_ip_egress = ""
    # Last observed WAN eth carrier (for plug-in edge detection). None = unknown
    # so the first sample does not look like a rising edge after boot.
    self._last_wan_carrier = None
    # In-flight guards so carrier/route edges do not stack concurrent HTTP
    # lookups (each burns up to ~timeout*N sources of radio-active time).
    self._wan_ip_updating = False
    self._wan_ipv6_updating = False
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
    if self._wan_ip_updating:
      return
    self._wan_ip_updating = True
    try:
      result = get_wan_ip()
      if result:
        self.wan_ip = result
        self._wan_ip_backoff = 30
        self._wan_ip_next_try = 0
        pcat_logger.logger.info("Public IPv4 resolved: %s", result)
      else:
        # Fail after the attempt: advance backoff so an offline/captive
        # box does not re-probe every 5s forever on battery.
        self._wan_ip_next_try = time.time() + self._wan_ip_backoff
        self._wan_ip_backoff = min(self._wan_ip_backoff * 2, 600)
    except Exception as e:
      pcat_logger.logger.warning(f"Background WAN IP update failed: {e}")
      self._wan_ip_next_try = time.time() + self._wan_ip_backoff
      self._wan_ip_backoff = min(self._wan_ip_backoff * 2, 600)
    finally:
      self._wan_ip_updating = False

  def _update_wan_ipv6(self):
    if self._wan_ipv6_updating:
      return
    self._wan_ipv6_updating = True
    try:
      result = get_wan_ipv6()
      if result:
        self.wan_ipv6 = result
        self._wan_ipv6_backoff = 30
        self._wan_ipv6_next_try = 0
        pcat_logger.logger.info("Public IPv6 resolved: %s", result)
      else:
        self._wan_ipv6_next_try = time.time() + self._wan_ipv6_backoff
        self._wan_ipv6_backoff = min(self._wan_ipv6_backoff * 2, 600)
    except Exception as e:
      pcat_logger.logger.warning(f"Background WAN IPv6 update failed: {e}")
      self._wan_ipv6_next_try = time.time() + self._wan_ipv6_backoff
      self._wan_ipv6_backoff = min(self._wan_ipv6_backoff * 2, 600)
    finally:
      self._wan_ipv6_updating = False

  def _request_wan_ip_refresh(self, reason="", force=False, delay=0.0):
    """Kick a public-IP lookup promptly (plug-in, route reappear, failover).

    force=True resets exponential backoff and starts a probe even if one is
    scheduled later. delay>0 waits for DHCP/netifd to settle after carrier up
    before the first HTTP attempt (avoids a guaranteed-fail probe at t=0).
    Concurrent probes are collapsed by the in-flight guards on the workers.
    Backoff advances only on failure (inside the worker), so a plug-in race
    against DHCP does not stretch the empty-state schedule to 60s+."""
    def _kick():
      if delay > 0:
        time.sleep(delay)
      invalidate_active_egress_cache()
      # Refresh the local interface address first (cheap); public IP follows.
      try:
        if get_active_egress():
          self.local_wan_ip = get_local_wan_ip()
      except Exception:
        pass
      now = time.time()
      if force:
        self._wan_ip_backoff = 30
        self._wan_ip_next_try = 0
        self._wan_ipv6_backoff = 30
        self._wan_ipv6_next_try = 0
      has_route = get_active_egress() != ""
      if not has_route:
        # Route not ready yet (common right after carrier up). Leave next_try
        # at 0 so the 5s empty-IP path retries as soon as egress appears.
        if reason:
          pcat_logger.logger.info(
            "Public IP refresh deferred, no route yet (%s)", reason)
        return
      # Hold next_try a few seconds while the worker runs so the 5s ticker
      # does not stack a second probe on top of this one.
      if force or self.wan_ip == "":
        if force or now >= self._wan_ip_next_try:
          self._wan_ip_next_try = now + 5
          threading.Thread(target=self._update_wan_ip, daemon=True).start()
      if force or self.wan_ipv6 == "":
        if force or now >= self._wan_ipv6_next_try:
          self._wan_ipv6_next_try = now + 5
          threading.Thread(target=self._update_wan_ipv6, daemon=True).start()
      if reason:
        pcat_logger.logger.info("Public IP refresh kicked (%s)", reason)

    if delay > 0:
      threading.Thread(target=_kick, daemon=True).start()
    else:
      _kick()

  def get_wifi_client_count(self):
    """Total connected WiFi clients, or None when the count is unknown.

    None must stay None: mapping it to 0 makes a ubus hiccup look like an
    empty AP, and check_idle_shutdown() powers the device off over it."""
    try:
      return command_parser.get_wifi_clients_count()
    except Exception as e:
      pcat_logger.logger.warning(f"Failed to get WiFi client count: {e}")
      return None

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

    if client_count is None:
      # Unknown is not "empty". Reset the countdown rather than advance it:
      # a broken probe must never be what powers the device off.
      pcat_logger.logger.warning(
        "Idle shutdown: WiFi client count unavailable; countdown reset")
      self.idle_status = "active"
      self.idle_start_time = None
      self.idle_remaining_seconds = self.idle_shutdown_timeout
      return

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

    # charge_percent defaults to 100 before the first pmu-status arrives; acting on it
    # would record a bogus 100% baseline and shut down as soon as the real (low) reading comes in
    charge_percent = socket_client.charge_percent
    if charge_percent is None or not socket_client.pmu_status_received:
      return

    # External DC power present: never auto-shutdown while the charger is connected,
    # regardless of battery percentage (keep an "ignored" latch intact for after unplug)
    if socket_client.charger_connected:
      if self.low_battery_status != "ignored":
        self.low_battery_status = "ok"
      return

    # Record initial battery level on first valid on-battery reading
    # Use a 2% tolerance above threshold to account for SOC estimation jumps after reboot
    boot_tolerance = 2
    if self.low_battery_initial_percent is None:
      self.low_battery_initial_percent = charge_percent
      # A persisted latch from a previous low-battery shutdown means this is a manual
      # power-up on remaining battery - don't shut down again until battery recovers
      if pcat_config.get_user_config_key("low_battery_shutdown_latched", False) \
          or charge_percent <= self.low_battery_threshold + boot_tolerance:
        self.low_battery_status = "ignored"
        pcat_logger.logger.info(f"Low battery shutdown: Battery level {charge_percent}% (threshold {self.low_battery_threshold}% + {boot_tolerance}% tolerance, or prior low-battery shutdown), ignoring until it rises above threshold")
        return

    # If we were ignoring because boot level was low, check if battery has risen above threshold + tolerance
    if self.low_battery_status == "ignored":
      if charge_percent > self.low_battery_threshold + boot_tolerance:
        # Battery has clearly risen above threshold, start monitoring normally
        self.low_battery_status = "ok"
        pcat_config.set_user_config_key("low_battery_shutdown_latched", False)
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
      # Latch so the next manual power-up can use the remaining battery
      pcat_config.set_user_config_key("low_battery_shutdown_latched", True)
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

  def _reconcile_cpu_powersave(self):
    """Park or restore the A72 cores to match the saved powersave preference
    and the current power source (see pcat_config.apply_cpu_scheduling_for_power).

    Uses charger_connected — the charger-present flag — rather than
    on_charging, which drops to False once the battery is full and would
    otherwise read as "on battery" while the device is mains powered."""
    try:
      pcat_config.apply_cpu_scheduling_for_power(socket_client.charger_connected)
    except Exception as e:
      pcat_logger.logger.error("CPU powersave reconcile error: %s", e)

  def _reconcile_wifi_wan(self):
    """Keep the Smart WAN upstream associated to a known WiFi (phone-like).
    Runs in a background thread (a scan blocks several seconds); an overlap
    guard skips a tick if the previous reconcile is still running."""
    if self._wifi_wan_reconciling:
      return

    def _worker():
      self._wifi_wan_reconciling = True
      try:
        result = wifi_wan.reconcile()
        # A (re)join always logs (it may repeat a prior string, e.g. two joins
        # to the same SSID). Every other outcome (scan_backoff, no_saved_in_range,
        # demoted, ok_online, wifi_slot_disabled...) logs only when it *changes*,
        # so the log stays quiet on a steady state but still shows why egress is
        # (or isn't) on WiFi — otherwise "why did it never rejoin my home WiFi?"
        # is undiagnosable.
        if result and (result.startswith(("rejoined:", "scan_failed:"))
                       or result != self._wifi_wan_last_result):
          pcat_logger.logger.info("Smart WAN reconcile: %s", result)
        self._wifi_wan_last_result = result
      except Exception as e:
        pcat_logger.logger.error("Smart WAN reconcile error: %s", e)
      finally:
        self._wifi_wan_reconciling = False

    threading.Thread(target=_worker, daemon=True).start()

  def _reconcile_ap_settings(self):
    """Put the customer's hotspot name/password back when the firmware has
    regenerated /etc/config/wireless behind us (see wifi_radio.keep_ap_settings).

    Off-thread: a repair ends in `wifi reload`, which blocks for seconds and
    must not stall the tick loop. An overlap guard keeps a slow reload from
    stacking with the next tick."""
    if self._ap_keeper_running:
      return
    self._ap_keeper_running = True

    def _worker():
      try:
        note = wifi_radio.keep_ap_settings()
        if note:
          pcat_logger.logger.info("WiFi hotspot keeper: %s", note)
      except Exception as e:
        pcat_logger.logger.error("WiFi hotspot keeper error: %s", e)
      finally:
        self._ap_keeper_running = False

    threading.Thread(target=_worker, daemon=True).start()

  def _reconcile_wan_manager(self):
    """Health-check the enabled WAN priority slots and demote/restore their
    route metrics (see pcat_util.wan_manager). Runs in a background thread
    (each probe blocks a few seconds); an overlap guard skips a tick while
    the previous round is still probing."""
    if self._wan_manager_reconciling:
      return
    # Set the guard on THIS thread, before the worker starts. Setting it inside
    # the worker left a window where a second tick passed the check above and
    # spawned an overlapping round; wan_manager.reconcile() also single-flights
    # internally, but closing the race here avoids the churn of a doomed thread.
    self._wan_manager_reconciling = True

    def _worker():
      try:
        result = wan_manager.reconcile()
        if result and ("offline" in result or "portal" in result or "down" in result):
          pcat_logger.logger.info("WAN priority reconcile: %s", result)
      except Exception as e:
        pcat_logger.logger.error("WAN priority reconcile error: %s", e)
      finally:
        self._wan_manager_reconciling = False

    threading.Thread(target=_worker, daemon=True).start()

  def _reconcile_nas(self):
    """NAS keeper (see pcat_util.nas_manager.reconcile): samba bind drift,
    share liveness summary. Off-thread with the same overlap guard shape as
    the WAN keeper — a slow disk must not back the tick loop up."""
    if self._nas_reconciling:
      return
    self._nas_reconciling = True

    def _worker():
      try:
        nas_manager.reconcile()
      except Exception as e:
        pcat_logger.logger.error("NAS reconcile error: %s", e)
      finally:
        self._nas_reconciling = False

    threading.Thread(target=_worker, daemon=True).start()

  def _reconcile_wan_link(self):
    """Fast carrier + metric-drift pass (no HTTP probes): demote eth as soon
    as the cable is out, and reassert the WAN metric ladder against dialer
    rewrites (legacy fm350-mm metric=14). Cheap enough to run every few
    seconds so failover / metric recovery is not stuck behind the 30s probe
    keeper.

    Also watches the eth0 carrier edge: plug-in must re-resolve the public
    IP within seconds (DHCP settle + one HTTP lookup), not wait for the 30s
    WAN-IP tick.

    Deliberately NOT gated on _wan_manager_reconciling: the whole point is to
    demote a yanked cable immediately, and the probe keeper can run for tens of
    seconds. wan_manager serializes _health with its own lock and never holds
    it across a probe, so this pass is safe to run concurrently and is no
    longer starved for the duration of a probe round."""
    try:
      wan_carrier = does_wan_have_carrier()
      prev_carrier = self._last_wan_carrier
      self._last_wan_carrier = wan_carrier

      # Rising edge: cable just went in (or PHY woke and saw a live link).
      # Invalidate egress, refresh local IP after a short DHCP settle, and
      # force a public-IP probe — this is the "plug cable → IP appears" path
      # users notice; waiting for the 30s tick made it feel broken.
      if prev_carrier is False and wan_carrier is True:
        pcat_logger.logger.info("WAN eth carrier up: refreshing public IP")
        invalidate_active_egress_cache()
        # 2s: enough for netifd/DHCP to install a default route on most
        # links; if not ready yet the empty-IP 5s path retries with backoff.
        self._request_wan_ip_refresh(reason="eth_carrier_up", force=True, delay=2.0)
        # Also nudge the priority keeper so a previously demoted wan slot
        # can be restored without waiting for the next 30s aligned tick.
        self._reconcile_wan_manager()

      result = wan_manager.reconcile_link_state()
      # Only log / kick WiFi on a real transition string (e.g. "wan:no_carrier"),
      # not on the steady "idle" while the cable stays out. metric_drift:* is
      # also non-idle (dialer stole the route); log it but only kick WiFi when
      # eth actually lost carrier.
      if result and result not in ("idle", "not_priority"):
        pcat_logger.logger.info("WAN link state: %s", result)
        if "wan:no_carrier" in result:
          # Eth just lost carrier: kick the WiFi keeper now so a dropped STA
          # rejoins within seconds instead of waiting for the next 20s tick.
          self._reconcile_wifi_wan()
          # Drop the egress memo so the next dashboard poll does not keep
          # painting ethernet as Active while the demoted route still exists.
          invalidate_active_egress_cache()
          # Public IP belonged to the eth path (or is unknown now). Flush
          # immediately so the UI does not keep the eth public IP while we
          # are offline or mid-failover; if another uplink is already up,
          # re-probe that path promptly.
          new_egress = get_active_egress()
          if self.wan_ip or self.wan_ipv6 or self.local_wan_ip:
            pcat_logger.logger.info(
              "Public IP flushed on eth unplug (egress now %s)",
              new_egress or "none")
            self.wan_ip = ""
            self.wan_ipv6 = ""
            self.local_wan_ip = ""
          self._wan_ip_egress = new_egress
          self._had_default_route = bool(new_egress)
          if new_egress:
            self._request_wan_ip_refresh(reason="eth_unplug_failover", force=True)
    except Exception as e:
      pcat_logger.logger.error("WAN link state error: %s", e)

  def run(self):
    pcat_logger.logger.info("App thread run.")
    global modem_client
    threading.Timer(3.0, lambda: modem_client.start_query("all")).start()
    # One-shot upgrade migration: an old build's "WAN off" must survive the
    # update as NIC-off + wan slot off. Runs before wan_manager.startup() so
    # the boot apply already sees the migrated table.
    migrate_legacy_wan_off()
    # Boot-time network hardening: mwan3 off (its fwmark rules can blackhole
    # all egress), dnsmasq all_servers on (DNS keeps answering across WAN
    # failover), and in priority mode re-assert the metric ladder.
    try:
      wan_manager.startup()
    except Exception as e:
      pcat_logger.logger.error("wan_manager startup failed: %s", e)
    # NAS storage: reload the job table, mount persisted volumes, pin the
    # samba lan bind and seed the hotplug snapshot before the tick loop
    # starts diffing it.
    try:
      nas_manager.startup()
    except Exception as e:
      pcat_logger.logger.error("nas_manager startup failed: %s", e)
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
          c_val, temp = battery_history_extras(socket_client)
          full_mah = socket_client.battery_full_capacity or socket_client.battery_nominal_capacity
          pcat_logger.logger.info(f"Initial battery record: v={v_val} c={c_val} soc={soc} charging={charging} temp={temp}")
          battery_history.record(soc, round(v_val, 3), c_val, charging,
                                 temperature=temp, full_capacity_mah=full_mah)
        except Exception as e:
          pcat_logger.logger.error(f"Initial battery record error: {e}")

      # Retry board_info if it was empty at startup (ubus may not have been ready)
      if self.tick_count % 5 == 0 and not self.board_info.get("hostname"):
        info = self.get_board_info()
        if info.get("hostname"):
          info["kernel"] = self.board_info.get("kernel", info.get("kernel", ""))
          info["openwrt_version"] = self.board_info.get("openwrt_version", info.get("openwrt_version", ""))
          self.board_info = info

      # Fast queries for unknown values (every 3s until discovered). On
      # devices where a value never materializes (e.g. Home version: no
      # modem, device_type stays "none") this used to fire every 3s forever
      # (~29k socket commands/day), so demote to 60s after 10 minutes.
      unknown_poll_due = (self.tick_count % 3 == 0) if self.tick_count < 600 \
          else (self.tick_count % 60 == 0)
      if unknown_poll_due:
        if socket_client.stm32_firmware_version is None:
          socket_client.query_stm32_version()
        if socket_client.pmu_hw_version is None:
          socket_client.query_pmu_hw_version()
        # Car mode belongs here rather than only in the 300s settings poll
        # below: that poll is gated on stm32_firmware_exist(), which is still
        # False at tick 0 because the version reply has not arrived, so the
        # first read of the timeout happened five minutes after boot. Until
        # then the dashboard reported the placeholder and /timer showed a
        # made-up default instead of the configured value.
        if not socket_client.charger_on_auto_start_mode.get("known") \
                and socket_client.stm32_firmware_exist():
          socket_client.query_charger_on_auto_start()
        # Same trap for the timer-mode schedule: without this the first read
        # came five minutes after boot, and /timer showed an empty list in the
        # meantime -- which looked exactly like the saved events had been lost
        # across the reboot (issue #118).
        if not socket_client.event_list_known \
                and socket_client.stm32_firmware_exist():
          socket_client.query_schedule_power_events()
        if socket_client.modem_apn is None or socket_client.device_type == "none":
          socket_client.modem_apn_get()

      # Live device status (SoC/route/modem): these feed the dashboard and the
      # always-on mini display and each pmu-status query is an STM32 UART wake.
      # Poll every 10s while a client is watching (browser/mini display poll
      # HTTP GETs -> note_web_activity) OR while car mode is armed (its countdown
      # ticks server-side and must stay responsive); otherwise, when the device
      # is truly headless, relax to 30s -- the only consumers then are the 30s
      # dip/low-battery/idle checks, which 30s fully serves. Resumes 10s within
      # one client poll. Saves up to ~5.7k STM32 wakes/day on an idle device.
      car_mode_armed = socket_client.charger_on_auto_start_mode.get("enabled") == 'true'
      status_fast = car_mode_armed or web_active_within(45)
      status_due = (self.tick_count % 10 == 0) if status_fast \
          else (self.tick_count % 30 == 0)
      if status_due:
        if socket_client.stm32_firmware_exist():
          socket_client.query_pmu_status()
          if car_mode_armed:
            socket_client.query_charger_on_auto_start()
        socket_client.query_network_route_mode_status()
        socket_client.query_modem_status()

      # PMU *settings* (schedule events, car mode, power-on mode, charge
      # threshold, LED/beeper) only change through user actions or the timer
      # manager, and every setter re-queries its value right after the set, so
      # the periodic poll only exists to catch a *different* client's edit or a
      # drift between the optimistic applied tracker and real PMU state -- rare.
      # 300s (was 60s) cuts ~3.6k needless daemon->STM32 UART round trips/day on
      # an idle device with no behaviour change; tick 0 still populates them at
      # boot. Each setter's own read-back keeps the acting client's own view
      # instant. pmu_io_get keeps /device_control "Current" accurate so a
      # failed boot set is visible and the timer manager can re-apply.
      if self.tick_count % 300 == 0 and socket_client.stm32_firmware_exist():
        socket_client.query_schedule_power_events()
        if socket_client.charger_on_auto_start_mode.get("enabled") != 'true':
          socket_client.query_charger_on_auto_start()
        socket_client.query_power_on_mode()
        socket_client.charge_threshold_get()
        socket_client.pmu_io_get()

      if self.tick_count % 60 == 0:
        modem_client.start_query("")

      # Battery *health* (learned capacity, cycle count, internal resistance) is
      # shown only on the battery diagnostics page and changes over days --
      # capacity is re-learned after a full discharge, cycles tick ~once/day. It
      # is NOT part of the dashboard/mini-display SoC (that comes from
      # pmu-status), so 300s (was 60s) saves ~1.1k STM32 UART round trips/day
      # with no user-visible effect. Boot tick 0 still populates it.
      if self.tick_count % 300 == 0 and socket_client.stm32_firmware_exist():
        socket_client.battery_info_get()

      # Smart WAN keeper: stay joined to a known upstream WiFi like a phone
      # does (every 20s). Runs off-thread because a scan blocks several seconds.
      # 20s (was 60s) so arriving at a known hotspot rejoins, and roaming to a
      # newly-in-range saved SSID happens, within a few seconds instead of up to
      # a minute. The expensive full-band scan is still gated: when away from
      # all saved nets by _scan_backoff, and while associated by the roam-scan
      # interval — so the faster tick costs little extra radio time.
      if self.tick_count % 20 == 0:
        self._reconcile_wifi_wan()
      # Fast eth carrier + metric reassert (every 5s): unplug must demote eth
      # immediately, and dialer metric drift (legacy fm350 metric=14) must be
      # corrected without waiting for the 30s HTTP health probe cycle.
      if self.tick_count % 5 == 0:
        self._reconcile_wan_link()
      # CPU powersave follows the power source (every 5s): the saved
      # preference parks the A72 cores only while on battery, and plugging in
      # DC brings them back. Cheap — reads the cached big cores' online flags
      # and writes only on an actual change.
      if self.tick_count % 5 == 0:
        self._reconcile_cpu_powersave()
      # WAN priority keeper (every 30s): probe each enabled slot for real
      # internet and demote/restore its metrics. Off-thread; probes block.
      if self.tick_count % 30 == 0:
        self._reconcile_wan_manager()
      # Hotspot keeper (every 5 min, offset off the busy ticks): the firmware
      # rewrites /etc/config/wireless at times of its own choosing, and what
      # comes back is the factory network name and password. The boot pass
      # only covers the first few minutes; this covers "it went back to the
      # defaults without even restarting". One uci read when nothing is wrong.
      if self.tick_count % 300 == 23:
        self._reconcile_ap_settings()
      # NAS hotplug (every 5s): diff /sys/block. The photonicat2 SD slot has
      # no working card-detect line (DTS broken-cd), so insertion/removal is
      # only observable by polling. One listdir when idle; the mount/unmount
      # work on a change runs on its own thread inside nas_manager.
      if self.tick_count % 5 == 0:
        try:
          nas_manager.reconcile_hotplug()
        except Exception as e:
          pcat_logger.logger.error("NAS hotplug error: %s", e)
      # NAS keeper (every 30s, offset from the WAN keeper's tick so the two
      # never stack their subprocess bursts on the same second).
      if self.tick_count % 30 == 7:
        self._reconcile_nas()
      # Public WAN IP discovery.
      #
      # While unknown on a live uplink, re-check every 5s (aligned with the
      # link pass) and apply exponential backoff between actual HTTP probes
      # (30s doubling to a 10min cap). Checking only every 30s used to leave
      # the connection card blank for up to half a minute after the cable
      # was plugged in even though the link was already carrying traffic.
      # Skip entirely with no usable default route; flush the cached value
      # the moment we go offline. Once known, refresh every 10 minutes;
      # re-probe promptly on egress class change (failover).
      if self.tick_count % 5 == 0:
        now = time.time()
        active_egress = get_active_egress()
        has_default_route = active_egress != ""
        egress_changed = has_default_route and active_egress != self._wan_ip_egress
        route_reappeared = has_default_route and not self._had_default_route

        if route_reappeared or egress_changed:
          # Route (re)appeared or moved to a different uplink: drop the old
          # public IP (it belongs to the previous path) and probe immediately.
          if egress_changed and (self.wan_ip or self.wan_ipv6):
            pcat_logger.logger.info(
              "Public IP cleared on egress change %s -> %s",
              self._wan_ip_egress or "(none)", active_egress)
            self.wan_ip = ""
            self.wan_ipv6 = ""
          self._request_wan_ip_refresh(
            reason=("egress_change:%s" % active_egress) if egress_changed
                   else "route_up",
            force=True)
        elif not has_default_route and (self.wan_ip or self.wan_ipv6 or self.local_wan_ip):
          # Explicit flush: no usable uplink means no public address to show.
          pcat_logger.logger.info(
            "Public IP flushed (no usable default route; was v4=%s v6=%s)",
            self.wan_ip or "-", self.wan_ipv6 or "-")
          self.wan_ip = ""
          self.wan_ipv6 = ""
          self.local_wan_ip = ""
        self._had_default_route = has_default_route
        self._wan_ip_egress = active_egress if has_default_route else ""

        # Empty on a live path: probe when backoff allows (every 5s check).
        # Backoff is advanced inside the worker on failure, not here.
        if has_default_route and self.wan_ip == "" and now >= self._wan_ip_next_try \
            and not self._wan_ip_updating:
          self._wan_ip_next_try = now + 5  # in-flight hold; worker sets real backoff
          threading.Thread(target=self._update_wan_ip, daemon=True).start()
        if has_default_route and self.wan_ipv6 == "" and now >= self._wan_ipv6_next_try \
            and not self._wan_ipv6_updating:
          self._wan_ipv6_next_try = now + 5
          threading.Thread(target=self._update_wan_ipv6, daemon=True).start()

      # Periodic refresh of a known public IP (every 10 min).
      if self.tick_count % 600 == 0 and self._had_default_route:
        if self.wan_ip:
          threading.Thread(target=self._update_wan_ip, daemon=True).start()
        if self.wan_ipv6:
          threading.Thread(target=self._update_wan_ipv6, daemon=True).start()

      # Local WAN interface IP: a display-only field (dashboard payload) that
      # changes only on DHCP renew / failover, yet get_local_wan_ip() forks
      # `ip route` + `ip addr` on every call. 120s (was 30s) removes ~4.3k
      # forks/day; plug-in / route-up paths refresh it immediately via
      # _request_wan_ip_refresh, so the slow periodic path is only a backstop.
      if self.tick_count % 120 == 0 and self._had_default_route:
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
            c_val, temp = battery_history_extras(socket_client)
            full_mah = socket_client.battery_full_capacity or socket_client.battery_nominal_capacity
            battery_history.record(soc, round(v_val, 3), c_val, charging,
                                   temperature=temp, full_capacity_mah=full_mah)
          else:
            pcat_logger.logger.warning("Battery history skipped: voltage is None")
        except Exception as e:
          pcat_logger.logger.error(f"Battery history record error: {e}")

      # Cleanup old battery history and network stats once a day
      if self.tick_count % 86400 == 0:
        battery_history.cleanup()
        try:
          self.network_stats.cleanup_old()
        except Exception as e:
          pcat_logger.logger.error(f"Network stats cleanup error: {e}")

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

  def _wifi_station_macs(self):
    """MACs associated to every live AP vif (hostapd get_clients via ubus).

    Every AP is resolved from netifd -- not index-guessed names, and not
    first-answer-wins across a radio's vifs."""
    macs = []
    for ap_name in (command_parser.live_ap_ifnames() or []):
      result = _openwrt_ubus_call("hostapd." + ap_name, "get_clients")
      if result and "clients" in result:
        macs += list(result["clients"].keys())
    return macs

  def get_lan_clients(self):
    """The /stats device table: every DHCP lease merged with Wi-Fi association.

    The old Wi-Fi-only list is empty on a wired deployment (no AP, dozens of
    DHCP clients), so the table now starts from the lease file and marks the
    rows hostapd currently sees. Either source failing degrades to the other,
    never to a 500."""
    try:
      with open('/tmp/dhcp.leases', 'r') as f:
        leases = lan_clients.parse_dhcp_leases(f.read())
    except Exception as e:
      pcat_logger.logger.warning(f"Failed to read /tmp/dhcp.leases: {e}")
      leases = []
    try:
      wifi_macs = self._wifi_station_macs()
    except Exception as e:
      pcat_logger.logger.warning(f"Failed to list Wi-Fi stations: {e}")
      wifi_macs = []
    return {
      "devices": lan_clients.merge_clients(leases, wifi_macs),
      "dhcp_count": len(leases),
      "wifi_count": len({m.lower() for m in wifi_macs}),
    }

  # TCP flow count reads the whole conntrack table (~1 MB, ~200 ms on a busy
  # box), so it is refreshed at most every 20s; everything else is a few
  # small /proc reads and is fresh on every call.
  _tcp_flows_cache = {"value": 0, "time": 0.0}

  def get_sys_stats(self):
    """CPU / memory / disks / TCP connections for the /stats badges.

    CPU is cumulative jiffies (busy, total): the page diffs two of its own
    polls, so no per-client state lives here."""
    def read(path):
      try:
        with open(path) as f:
          return f.read()
      except OSError:
        return ""
    stat_text = read("/proc/stat")
    busy, total = sys_stats.parse_proc_stat(stat_text)
    per_core = sys_stats.parse_proc_stat_cores(stat_text)
    mem = sys_stats.parse_meminfo(read("/proc/meminfo"))
    disks = []
    for mount, fstype, dev in sys_stats.select_disk_mounts(read("/proc/mounts")):
      usage = sys_stats.disk_usage(mount)
      if usage:
        usage.update({"mount": mount, "fstype": fstype,
                      "kind": sys_stats.disk_kind(dev, read)})
        disks.append(usage)
    now = time.time()
    if now - self._tcp_flows_cache["time"] >= 20:
      self._tcp_flows_cache["value"] = sys_stats.count_tcp_flows(read("/proc/net/nf_conntrack"))
      self._tcp_flows_cache["time"] = now
    def read_int(path):
      try:
        return int(read(path).strip() or 0)
      except ValueError:
        return 0
    return {
      "ts": now,
      "cpu": {"busy": busy, "total": total, "cores": os.cpu_count() or 1,
              "per_core": per_core,
              "load": sys_stats.parse_loadavg(read("/proc/loadavg"))},
      "mem": mem,
      "disks": disks,
      "conn": {"tcp": self._tcp_flows_cache["value"],
               "total": read_int("/proc/sys/net/netfilter/nf_conntrack_count"),
               "max": read_int("/proc/sys/net/netfilter/nf_conntrack_max")},
    }

  def get_egress_counters(self):
    """Cumulative rx/tx byte counters of the egress netdevs, for the live
    throughput chart on /stats (the browser samples this once a second and
    keeps the last minute in memory; nothing is stored here).

    Egress = the current default-route devices, so PPPoE (pppoe-wan), cellular
    (wwan0/usb0/eth2) and a Wi-Fi STA upstream all count and a failover follows
    automatically. With no default route at all, fall back to the fixed egress
    list the dashboard speed uses, restricted to devices that exist."""
    devs = get_default_route_devs()
    if not devs:
      candidates = tuple(self.network_stats.SPEED_DEVICES) + tuple(_get_sta_devices())
      devs = [d for d in candidates if os.path.exists("/sys/class/net/%s" % d)]
    ifaces = {}
    for dev in devs:
      ifaces[dev] = {
        "rx_bytes": _read_netdev_counter(dev, "rx"),
        "tx_bytes": _read_netdev_counter(dev, "tx"),
        "kind": _egress_kind_for_dev(dev),
      }
    return {"ts": time.time(), "ifaces": ifaces}

  def get_clients(self):
    mock = False
    mock_clients_data = {'freq': 2457, 'clients': {'7c:03:ab:48:31:6c': {'auth': True, 'assoc': True, 'authorized': True, 'preauth': False, 'wds': False, 
      'wmm': True, 'ht': True, 'vht': False, 'he': False, 'wps': False, 'mfp': False, 'rrm': [0, 0, 0, 0, 0], 'aid': 2, 'bytes':
       {'rx': 22556457, 'tx': 318005089}, 'airtime': {'rx': 0, 'tx': 460871086}, 'packets': {'rx': 158290, 'tx': 245375}, 'rate': 
       {'rx': 1000, 'tx': 1000}, 'signal': -41, 'capabilities': {}}, 'e4:42:a6:56:4e:e7': {'auth': True, 'assoc': True, 'authorized': True, 
       'preauth': False, 'wds': False, 'wmm': True, 'ht': True, 'vht': False, 'he': False, 'wps': False, 'mfp': False, 'rrm': [0, 0, 0, 0, 0], 
       'aid': 1, 'bytes': {'rx': 4549039, 'tx': 120524910}, 'airtime': {'rx': 0, 'tx': 174426269}, 'packets': {'rx': 41293, 'tx': 91143}, 
       'rate': {'rx': 1000, 'tx': 1000}, 'signal': -49, 'capabilities': {}}}}
    clients_mac = []
    if mock:
      cmd_json = mock_clients_data
      clients_mac = list(cmd_json["clients"].keys())
    else:
      # Every live AP vif, resolved from netifd -- not index-guessed names,
      # and not first-answer-wins across a radio's vifs.
      clients_mac = self._wifi_station_macs()

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
