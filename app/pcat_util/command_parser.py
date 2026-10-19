#@Author    : qichunren

import pathlib
import re
import subprocess
import os
import json
from pcat_util.openwrt import ubus_call, uci_get, uci_show
# Return list
# eg: [{'exist': True, 'hidden': '1', 'device': 'radio0', 'band': '2g', 'htmode': 'HT20', 'device_type': 'Onboard', 'ssid': 'photonicat-dac1d067', 'encryption': 'psk2', 'password': 'photonicat', 'frequency': '2g'},
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
  val1 = uci_get("wireless." + dev_name + ".disabled")
  val2 = uci_get("wireless.default_" + dev_name + ".disabled")
  
  return (val1 == "0" or val1 == "") and (val2 == "0" or val2 == "")  # empty means not disabled (enabled by default)

def live_ap_ifnames():
  """Netdev names of every AP interface netifd is running, across all radios.

  Resolved from 'network.wireless status', never guessed from the radio
  index: phyN/wlanN numbering is not stable across boots on this hardware
  (USB/PCIe enumeration race), and a radio serving a second SSID has AP
  vifs beyond the first. Returns None when netifd cannot be asked at all.
  """
  status = ubus_call("network.wireless", "status")
  if not status:
    return None
  names = []
  for radio in status.values():
    if not isinstance(radio, dict):
      continue
    for iface in radio.get("interfaces", []):
      name = iface.get("ifname")
      mode = (iface.get("config") or {}).get("mode", "")
      if name and mode == "ap":
        names.append(name)
  return names

def get_wifi_clients_count():
  """Total stations across every live AP, or None when that is unknowable.

  WiFi-upstream mode parks one radio's AP, which used to reduce the count
  to a single index-guessed hostapd name; on a boot where the enumeration
  order differed, the guess missed the live AP and the count read 0 with
  clients connected -- and the idle auto-shutdown acted on it. None is
  deliberately distinct from 0: a probe failure must not look like an
  empty AP to the shutdown countdown.
  """
  ap_names = live_ap_ifnames()
  if ap_names is None:
    return None
  if not ap_names:
    return 0  # no AP is up at all; nobody can be connected
  total = 0
  for name in ap_names:
    result = ubus_call("hostapd." + name, "get_clients")
    if not result or "clients" not in result:
      return None  # an AP we cannot see may hold the only client
    total += len(result["clients"])
  return total

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


PLATFORM_PREFIX = "platform/"

PHY_DIR = "/sys/class/ieee80211"

# A USB *interface* directory: "<bus>-<port chain>:<config>.<interface>", e.g.
# "1-1.3:1.0" sitting under the device dir "1-1.3".
USB_INTERFACE_RE = re.compile(r"^\d+-[\d.]+:\d+\.\d+$")


def canonical_path(path):
  """A radio device path in one spelling, so two of them compare equal.

  UCI stores the PCIe radio relative to /sys/devices ("soc/...pcie/...") and
  the onboard one relative to /sys/devices/platform ("platform/soc/...usb/..."),
  while sysfs itself uses the longer form for both. device_exist() papers over
  that by trying both prefixes; anything that compares paths has to normalise
  instead.
  """
  if path.startswith(PLATFORM_PREFIX):
    return path[len(PLATFORM_PREFIX):]
  return path


def _strip_usb_interface(path):
  """The USB *device* a phy belongs to, given the interface sysfs binds it at.

  A USB Wi-Fi driver binds to an interface, so a phy's `device` link lands on
  ".../1-1/1-1.3/1-1.3:1.0" -- one level below the ".../1-1/1-1.3" that UCI
  and /etc/board.json record. Left in, the two spellings never compare equal:
  the onboard radio's capabilities lookup misses and the settings page offers
  it no bands.

  Only the USB interface shape is peeled, and only when it really is a child
  of its own device. An SDIO function ("mmc2:0001/mmc2:0001:1") looks similar
  and *is* the recorded path, so it must survive untouched.
  """
  parent, _, last = path.rpartition("/")
  if parent and USB_INTERFACE_RE.match(last):
    if last.startswith(parent.rpartition("/")[2] + ":"):
      return parent
  return path


def live_phy_paths():
  """{phy name: canonical device path} for every mac80211 phy the kernel has.

  Ground truth about what is fitted, independent of whatever path
  /etc/config/wireless happens to be carrying -- which is the point: the
  stored path goes stale (see resolve_radio_path).
  """
  paths = {}
  try:
    phys = sorted(os.listdir(PHY_DIR))
  except OSError:
    return paths
  marker = "/sys/devices/"
  for phy in phys:
    if not phy.startswith("phy"):
      continue
    try:
      real = os.path.realpath(os.path.join(PHY_DIR, phy, "device"))
    except OSError:
      continue
    if real.startswith(marker):
      paths[phy] = _strip_usb_interface(canonical_path(real[len(marker):]))
  return paths


def path_class(path):
  """Which of the two physical radio slots a path belongs to."""
  return "pcie" if "pcie" in path else "onboard"


def device_type_for_path(path):
  """The label the settings page and the mini display show for a radio.

  Matched on the *shape* of the path as well as the exact strings in
  builtin_wifi_dev. The onboard module hangs off a USB host controller whose
  instance number is not stable across reboots -- the same card has been seen
  at xhci-hcd.1 and xhci-hcd.4 on consecutive boots -- so an index that list
  does not happen to enumerate must not relabel the onboard radio "Unknown".
  """
  if not path:
    return "Unknown"
  if path in builtin_wifi_dev:
    return "Onboard"
  if "pcie" in path:
    return "PCIE"
  if "xhci" in path or ".usb/" in path or "mmc" in path:
    return "Onboard"
  return "Unknown"

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

def _uci_show_to_dict(cmd_output):
  """`uci show wireless` output as {key: value}, quotes stripped."""
  cfg = {}
  for line in cmd_output.split(b'\n'):
    if not line:
      continue
    # Split on the FIRST '=' only: a value may itself contain '=' (e.g. an
    # SSID or key like a=b), which a bare split() would truncate. Drop any
    # line without a '=' rather than IndexError on malformed input.
    item = line.split(b'=', 1)
    if len(item) != 2:
      continue
    value = item[1]
    if value.startswith(b"'"):
      value = value[1:-1]
    try:
      cfg[item[0].decode('utf8')] = value.decode('utf8')
    except UnicodeDecodeError:
      continue
  return cfg


def radio_names(cfg):
  """Every radio the config knows about, in the order the UI shows them.

  Includes a radio whose `wifi-device` section is *missing* but whose AP iface
  is still present. OpenWrt prunes the device section when it cannot match the
  stored path to a phy -- which happens when the onboard radio's USB host
  controller renumbers, or when the radio is slow to enumerate on a boot where
  its AP is parked for the upstream relay. The orphaned iface is then the only
  record that the customer has that radio at all, and dropping it here is what
  made the onboard WiFi disappear from the settings page.
  """
  names = []
  for key, value in cfg.items():
    if value == "wifi-device" and key.count(".") == 1:
      names.append(key.split(".", 1)[1])
  for key, value in cfg.items():
    if value != "wifi-iface" or key.count(".") != 1:
      continue
    device = cfg.get("wireless.{}.device".format(key.split(".", 1)[1]), "")
    if device and device not in names:
      names.append(device)
  primary = ("radio0", "radio1")
  return sorted(names, key=lambda n: (primary.index(n) if n in primary
                                      else len(primary), n))


def _ap_vif(cfg, radio):
  """The iface section this radio serves clients from.

  Falls back to the conventional `default_<radio>` name so callers can look
  options up unconditionally; the STA relay's own section is never returned.
  """
  default = "default_{}".format(radio)
  if cfg.get("wireless.{}".format(default)) == "wifi-iface":
    return default
  for key, value in cfg.items():
    if value != "wifi-iface" or key.count(".") != 1:
      continue
    section = key.split(".", 1)[1]
    if cfg.get("wireless.{}.device".format(section)) != radio:
      continue
    if cfg.get("wireless.{}.mode".format(section), "ap") != "ap":
      continue
    return section
  return default


def resolve_radio_path(cfg, radio, radios, live_paths):
  """(effective device path, is it fitted) for one radio section.

  The configured path is used whenever it still resolves. When it does not --
  a path left stale by the USB controller renumbering between boots, or no
  path at all because the device section was pruned -- fall back to a live phy
  of the same class that no other radio accounts for.

  Reporting a fitted radio as absent is the expensive direction of this call:
  it takes the radio off the settings page entirely, which reads to the
  customer as their WiFi having vanished. So absence is only concluded when
  there is no unaccounted-for phy it could possibly be.
  """
  configured = cfg.get("wireless.{}.path".format(radio), "")
  if configured and device_exist(configured):
    return configured, True
  claimed = set()
  for other in radios:
    if other == radio:
      continue
    other_path = cfg.get("wireless.{}.path".format(other), "")
    if other_path and device_exist(other_path):
      claimed.add(canonical_path(other_path))
  free = sorted(p for p in live_paths.values() if p not in claimed)
  if not free:
    return configured, False
  if configured:
    same_class = [p for p in free if path_class(p) == path_class(configured)]
    if same_class:
      return same_class[0], True
    return configured, False
  # No path to go on: only claim an unaccounted-for phy when it is the sole
  # candidate, so a second radio section can never adopt the first one's card.
  if len(free) == 1:
    return free[0], True
  return configured, False


def parse_uci_show_wireless(is_mock_data):
  if is_mock_data:
    current_working_path = pathlib.Path(__file__).parent.parent.resolve()
    fixture_file = current_working_path.joinpath("test", "fixture_wireless.txt")
    f = open(fixture_file, "rb")
    cmd_output = f.read()
    f.close()
  else:
    cmd_output = uci_show("wireless")
  # uci_show returns bytes (b"" on error); the mock path also reads bytes.
  # `== ""` compared bytes to str and never matched, so an empty/failed read
  # fell through instead of returning early. `not` handles both b"" and "".
  if not cmd_output:
    return []

  cfg = _uci_show_to_dict(cmd_output)
  phy_caps_by_path = {} if is_mock_data else _read_board_phy_capabilities()
  caps_by_path = {canonical_path(k): v for k, v in phy_caps_by_path.items()}
  live_paths = {} if is_mock_data else live_phy_paths()
  empty_caps = {"is_pcie": False, "bands": {}}

  # While the upstream STA is active that radio's own AP is parked, so
  # present it as relaying to the settings UI and to the screen: not editable,
  # showing the upstream hotspot it is joined to instead.  Report the SSID only
  # while the STA is actually *associated* — when it is enabled but out of range
  # / not yet joined the configured SSID is stale, so surface a standby marker
  # (empty ssid + associated=False) instead of a name that implies a live link.
  sta_radio = None
  sta_ssid = ""
  sta_associated = False
  if not is_mock_data:
    try:
      from pcat_util import wifi_wan
      sta_radio = wifi_wan.active_sta_radio()
    except Exception:
      sta_radio = None
    # get_state() does live ubus/iwinfo probing and is more failure-prone than
    # active_sta_radio()'s uci read; keep its errors from un-marking the relay
    # radio. A failed probe just leaves the radio on standby (no live ssid).
    if sta_radio:
      try:
        state = wifi_wan.get_state()
        sta_associated = bool(state.get("associated"))
        # Only the live-joined SSID is trustworthy; a disassociated STA keeps
        # its last-configured ssid in UCI, which is exactly the stale name we
        # must not present as "connected to".
        if sta_associated:
          sta_ssid = state.get("ssid", "") or ""
      except Exception:
        sta_associated = False

  radios = radio_names(cfg)
  result_list = []
  for dev in radios:
    path, fitted = resolve_radio_path(cfg, dev, radios, live_paths)
    if not fitted:
      continue  # nothing physical behind this section; don't offer a ghost
    vif = _ap_vif(cfg, dev)
    info = {
      'exist': True,
      'hidden': cfg.get("wireless.{}.hidden".format(vif), '0'),
      'device': dev,
      'band': cfg.get("wireless.{}.band".format(dev), ''),
      'htmode': cfg.get("wireless.{}.htmode".format(dev), ''),
      'enabled': is_wifi_interface_enabled(dev),
      'capabilities': caps_by_path.get(canonical_path(path), empty_caps),
      'device_type': device_type_for_path(path),
    }
    ssid = cfg.get("wireless.{}.ssid".format(vif))
    if ssid is not None:
      info['ssid'] = ssid
    encryption = cfg.get("wireless.{}.encryption".format(vif))
    if encryption is not None:
      info['encryption'] = _normalize_encryption(encryption)
    key = cfg.get("wireless.{}.key".format(vif))
    if key is not None:
      info['password'] = key
    info['frequency'] = _frequency_from_band_htmode(
      info['band'], info['htmode']
    )
    if dev == sta_radio:
      info['enabled'] = False
      info['wifi_wan'] = True
      info['wifi_wan_associated'] = sta_associated
      info['wifi_wan_ssid'] = sta_ssid
    result_list.append(info)
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
