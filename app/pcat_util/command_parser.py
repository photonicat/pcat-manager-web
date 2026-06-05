#@Author    : qichunren

import pathlib
import subprocess
import os
import json
from pcat_util.openwrt import ubus_call, uci_get, uci_show
# Return list
# eg: [{'exist': True, 'hidden': '1', 'device': 'radio0', 'band': '2g', 'htmode': 'HT20', 'device_type': 'Builtin', 'ssid': 'photonicat-dac1d067', 'encryption': 'psk2', 'password': 'photonicat', 'frequency': '2g'}, 
#      {'exist': True, 'hidden': '0', 'device': 'radio1', 'band': '5g', 'htmode': 'HE80', 'device_type': 'PCIE', 'ssid': 'OpenWrt-w6', 'encryption': 'psk2', 'password': '12345678', 'frequency': '5g_ax'}]

builtin_wifi_dev = [
  "platform/fe2c0000.mmc/mmc_host/mmc2/mmc2:0001/mmc2:0001:1",
  "platform/fe2c0000.dwmmc/mmc_host/mmc2/mmc2:0001/mmc2:0001:1",
  "platform/soc/23000000.usb/xhci-hcd.0.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.1.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.2.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.3.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.4.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.5.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.6.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.7.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.8.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.9.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.10.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.11.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.12.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.13.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.14.auto/usb1/1-1/1-1.3",
  "platform/soc/23000000.usb/xhci-hcd.15.auto/usb1/1-1/1-1.3",
]

# Wi-Fi 是否开启, dev_name: radio0, radio1
def _normalize_encryption(value):
  """Normalize OpenWrt encryption strings to the canonical values used by the UI.

  OpenWrt may store cipher suffixes like '+ccmp' or '+tkip' (e.g. 'sae+ccmp',
  'psk2+ccmp').  Strip them so the UI dropdown can match correctly.
  Also map 'sae-mixed' variants to 'sae'.
  """
  if not value:
    return "none"
  v = value.split("+")[0]  # drop cipher suffix
  if v.startswith("sae-mixed"):
    v = "sae"
  if v in ("sae", "psk2", "psk", "none"):
    return v
  return value  # keep unknown values as-is

def is_wifi_interface_enabled(dev_name):
  val = uci_get("wireless." + dev_name + ".disabled")
  return val == "0" or val == ""  # empty means not disabled (enabled by default)

def _get_hostapd_client_count(iface_names):
  """Try multiple hostapd interface names, return client count from first that works."""
  for iface in iface_names:
    result = ubus_call(iface, "get_clients")
    if result and "clients" in result:
      return len(result["clients"])
  return 0

def get_wifi_clients_count():
  count_0 = 0
  count_1 = 0
  if is_wifi_interface_enabled("radio0"):
    count_0 = _get_hostapd_client_count(["hostapd.wlan0", "hostapd.wlan0-1", "hostapd.phy0-ap0"])
  if is_wifi_interface_enabled("radio1"):
    count_1 = _get_hostapd_client_count(["hostapd.wlan1", "hostapd.wlan1-1", "hostapd.phy1-ap0"])
  return count_0 + count_1

def device_exist(net_device_name):
  full_dev_path1 = "/sys/devices/" + net_device_name
  full_dev_path2 = "/sys/devices/platform/" + net_device_name
  if os.path.exists(full_dev_path1):
    return True
  else:
    if os.path.exists(full_dev_path2):
      return True
    else:
      return False

def _read_board_phy_capabilities():
  """Read /etc/board.json and return {phy_path: {is_pcie, bands: {2g|5g|6g: {eht,he,vht,ht,max_width,default_channel}}}}.

  Used by the wireless settings UI to decide whether to expose Wi-Fi 7 (BE) options
  for each radio. Matching is by UCI wireless.radioN.path -> board.json wlan.phyN.path.
  """
  try:
    with open("/etc/board.json", "r") as f:
      board = json.load(f)
  except Exception:
    return {}
  out = {}
  wlan = (board or {}).get("wlan", {}) or {}
  for _phy_name, phy in wlan.items():
    if not isinstance(phy, dict):
      continue
    path = phy.get("path", "")
    info = phy.get("info", {}) or {}
    bands = info.get("bands", {}) or {}
    caps = {"is_pcie": "pcie" in path, "bands": {}}
    for band_key, band in bands.items():
      if not isinstance(band, dict):
        continue
      caps["bands"][band_key.lower()] = {
        "eht": bool(band.get("eht")),
        "he": bool(band.get("he")),
        "vht": bool(band.get("vht")),
        "ht": bool(band.get("ht")),
        "max_width": int(band.get("max_width", 0) or 0),
        "default_channel": int(band.get("default_channel", 0) or 0),
      }
    out[path] = caps
  return out

def _frequency_from_band_htmode(band, htmode):
  if band == "2g":
    if htmode.startswith("EHT"):
      return "2g_be"
    return "2g"
  if band == "5g":
    if htmode.startswith("EHT"):
      return "5g_be"
    if htmode == "VHT40":
      return "5g"
    if htmode == "HE80":
      return "5g_ax"
  if band == "6g":
    return "6g_be"
  return ""

def parse_uci_show_wireless(is_mock_data):
  if is_mock_data:
    current_working_path = pathlib.Path(__file__).parent.parent.resolve()
    fixture_file = current_working_path.joinpath("test", "fixture_wireless.txt")
    f = open(fixture_file, "rb")
    cmd_output = f.read()
    f.close()
  else:
    cmd_output = uci_show("wireless")
  if cmd_output == "":
    return []

  cmd_output_arr = cmd_output.split(b'\n')
  cmd_output_arr = list(filter(None, cmd_output_arr))
  result_map = map(lambda x: x.split(b'='), cmd_output_arr)
  cmd_output_arr = list(result_map)
  phy_caps_by_path = {} if is_mock_data else _read_board_phy_capabilities()
  empty_caps = {"is_pcie": False, "bands": {}}
  wifi_dict = {
    "radio0": {'exist': False, 'hidden': '0', 'device': 'radio0', 'band': '', 'htmode': '', 'enabled': True, 'capabilities': empty_caps},
    "radio1": {'exist': False, 'hidden': '0', 'device': 'radio1', 'band': '', 'htmode': '', 'enabled': True, 'capabilities': empty_caps}
  }
  for item in cmd_output_arr:
    if item[1].startswith(b"'"):
      item_value = item[1][1:-1].decode('utf8')
    else:
      item_value = item[1].decode('utf8')

    # Parse radio 0
    if item[0] == b'wireless.default_radio0.ssid':
      wifi_dict["radio0"]["ssid"] = item_value
    if item[0] == b'wireless.default_radio0.encryption':
      wifi_dict["radio0"]["encryption"] = _normalize_encryption(item_value)
    if item[0] == b'wireless.default_radio0.key':
      wifi_dict["radio0"]["password"] = item_value
    if item[0] == b'wireless.radio0.band':
      wifi_dict["radio0"]["band"] = item_value
    if item[0] == b'wireless.radio0.htmode':
      wifi_dict["radio0"]["htmode"] = item_value      
    if item[0] == b'wireless.default_radio0.hidden':
      wifi_dict["radio0"]["hidden"] = item_value
    if item[0] == b'wireless.radio0.path':
      if item_value in builtin_wifi_dev:
        wifi_dict["radio0"]["device_type"] = "Onboard"
      elif "pcie" in item_value:
        wifi_dict["radio0"]["device_type"] = "PCIE"
      else:
        wifi_dict["radio0"]["device_type"] = "Unknown"
      wifi_dict["radio0"]["exist"] = device_exist(item_value)
      wifi_dict["radio0"]["enabled"] = is_wifi_interface_enabled("radio0")
      wifi_dict["radio0"]["capabilities"] = phy_caps_by_path.get(item_value, empty_caps)
    # Parse radio 1
    if item[0] == b'wireless.default_radio1.ssid':
      wifi_dict["radio1"]["ssid"] = item_value
    if item[0] == b'wireless.default_radio1.encryption':
      wifi_dict["radio1"]["encryption"] = _normalize_encryption(item_value)
    if item[0] == b'wireless.default_radio1.key':
      wifi_dict["radio1"]["password"] = item_value
    if item[0] == b'wireless.radio1.band':
      wifi_dict["radio1"]["band"] = item_value
    if item[0] == b'wireless.radio1.htmode':
      wifi_dict["radio1"]["htmode"] = item_value         
    if item[0] == b'wireless.default_radio1.hidden':
      wifi_dict["radio1"]["hidden"] = item_value
    if item[0] == b'wireless.radio1.path':
      if item_value in builtin_wifi_dev:
        wifi_dict["radio1"]["device_type"] = "Builtin"
      elif "pcie" in item_value:
        wifi_dict["radio1"]["device_type"] = "PCIE"
      else:
        wifi_dict["radio1"]["device_type"] = "Unknown"
      wifi_dict["radio1"]["exist"] = device_exist(item_value)
      wifi_dict["radio1"]["enabled"] = is_wifi_interface_enabled("radio1")
      wifi_dict["radio1"]["capabilities"] = phy_caps_by_path.get(item_value, empty_caps)

  result_list = []
  for dev in ("radio0", "radio1"):
    if wifi_dict[dev]["exist"]:
      wifi_dict[dev]["frequency"] = _frequency_from_band_htmode(
        wifi_dict[dev]["band"], wifi_dict[dev]["htmode"]
      )
      result_list.append(wifi_dict[dev])
  return result_list

# For self test
if __name__ == "__main__":
  import pathlib
  current_working_path = pathlib.Path(__file__).parent.parent.resolve()
  fixture_file = current_working_path.joinpath("test", "fixture_wireless.txt")
  f = open(fixture_file, "rb")
  cmd_output = f.read()
  f.close()
  wifi_list = parse_uci_show_wireless(cmd_output)
  print("Get list:", wifi_list)
