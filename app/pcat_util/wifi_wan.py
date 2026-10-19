"""
WiFi-as-WAN support: an upstream STA link used by the "Smart WAN" (智能多网)
network mode and by the "wifi" slot of the WAN priority table (wan_manager).

One radio joins an existing hotspot as a STA client (the "upstream WiFi")
while the other radio keeps serving clients as an AP.  Which radio carries
the STA is selectable (pcat_config wifi_wan_radio: 'onboard' default, or
'pcie'); see set_sta_radio().  Failover is handled purely by route metrics —
in the legacy modes the historic eth0(1) > wan_wifi(5) > cellular(10+)
chain, in priority mode the metrics assigned by wan_manager's table.

UCI layout (mirrors the known-good wireless bridge config):
    wireless.wifinet_sta   wifi-iface   mode=sta, network=wan_wifi
    network.wan_wifi       interface    proto=dhcp,   metric=<v4>
    network.wan_wifi_v6    interface    proto=dhcpv6, device=@wan_wifi, metric=<v6>
    firewall wan zone      network list += wan_wifi, wan_wifi_v6

While the STA is active that radio's own AP (default_radioN) is parked:
AP+STA on one radio is fragile (the AP channel is slaved to the upstream and
flaps when the upstream disappears).  The radio's previous band/htmode/
channel and AP disabled-state are backed up in the user config and restored
when the STA leaves the radio.
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import time
from urllib.parse import urljoin, urlsplit

from pcat_util.openwrt import (
    ubus_call,
    uci_get,
    uci_set,
    uci_commit,
    uci_add_list,
    uci_delete,
    uci_show_dict,
)
from pcat_util import pcat_logger

STA_SECTION = "wifinet_sta"
NET_IFACE = "wan_wifi"
NET_IFACE_V6 = "wan_wifi_v6"


def _metrics():
    """(v4, v6, demoted_v4) route metrics for the upstream. wan_manager keeps
    them in step with the priority table (or the legacy 5/6/50 values). The
    demoted metric is used when the upstream is associated but has no real
    internet (captive portal / dead uplink): egress fails over to the next
    slot while the link stays up so a login page is still reachable."""
    from pcat_util import wan_manager
    return wan_manager.wifi_metrics()

# "eap" = WPA-Enterprise (802.1x). Needs full wpad on the device; gated by
# eap_supported(). PSK variants work on any wpad build.
SUPPORTED_ENCRYPTIONS = ("none", "psk", "psk2", "sae", "eap")
EAP_TYPES = ("peap", "ttls", "tls")

# Connectivity probe (Photonicat-hosted, no third-party dependency).
# HTTPS first: a captive portal cannot present a valid cert for our domain, so
# a clean 200 over TLS proves real internet — it can't be a spoofed login page.
# Only if TLS fails do we fall back to plain HTTP to tell a captive portal
# (intercepts HTTP and answers with its own page/redirect) from a dead uplink
# (no answer at all). https://4.photonicat.com/ip.php is the same lightweight
# IP-echo endpoint used elsewhere (see OEM.md ip_check_urls).
_CONN_CHECK_URL = "http://4.photonicat.com/ip.php"
_CONN_CHECK_URL_HTTPS = "https://4.photonicat.com/ip.php"
_CONN_CACHE_TTL = 45  # seconds; reconcile() refreshes this, get_state() reads it
# Each probe round retries before concluding failure, so a single dropped packet
# on a marginal link doesn't flip the verdict.
_CONN_CHECK_ATTEMPTS = 2
# Flap damping: once we've verified the upstream is online it takes this many
# consecutive bad probe rounds to leave that state. Recovery to online is
# immediate. Without this a lone failed probe demotes the route and bounces all
# egress to cellular for a full reconcile cycle (60s), then back — the exact
# instability this guards against. Genuine portal/offline is still caught, just
# after a brief confirmation window rather than on the first hiccup.
_CONN_FLAP_THRESHOLD = 2
_conn_cache = {"result": "unknown", "time": 0.0, "bad_streak": 0}

# While the upstream is confirmed online, re-verify only this often instead of
# on every 20s reconcile tick. The STA radio is already up, so this is a CPU +
# curl saving, not a radio wake; it widens only the "associated but ISP silently
# dead" detection window (association loss is caught immediately). Portal/offline/
# unknown still probe every tick so recovery and captive-portal login stay prompt.
_ONLINE_PROBE_INTERVAL = 60

# Scan backoff for the reconcile keeper. A full-band WiFi scan is one of the
# most power-hungry radio operations; when no saved network is in range
# (device away from its known hotspots) rescanning every 60s tick just burns
# battery. Consecutive empty/failed scans back the next scan off 60s -> 90s
# (cap). The cap is deliberately low so arriving at a known hotspot rejoins
# within a tick or two rather than leaving egress on cellular for minutes:
# a phone that never rejoins home WiFi is a worse failure than a slightly
# busier radio. Any association, successful join candidate, or user
# (re)configuration resets it so joining stays as responsive as before.
_SCAN_BACKOFF_CAP = 60
# No long tier. There used to be one (300s after 10 fruitless scans) to save the
# radio on a device parked away from all its networks for hours. It cost exactly
# the case this keeper exists for: leave site A, commute, arrive at site B --
# the commute is all fruitless scans, so the device escalated to the 300s tier
# and then sat in it, not scanning, while a saved network was in range the whole
# time. Arriving home to a still-disconnected upstream minutes later is the
# opposite of the phone-like behaviour this module is named for; a phone rescans
# on the order of a minute no matter how long it has been away. Cellular carries
# traffic meanwhile, so the only thing the long tier ever bought was a little
# battery, at the price of the feature working.
_scan_backoff = {"fails": 0, "next_ok": 0.0}

# Roam-while-associated. The keeper's default is stay-if-connected: once joined
# it never scans, so it can't notice a *different* saved network that has come
# into range (the "arrived at a new location, still clinging to the old/weak AP"
# case). To fix that we periodically (not every tick — a scan is power-hungry)
# scan while associated and switch to another saved SSID only if it is
# meaningfully stronger than the current link, with hysteresis so we don't
# ping-pong between two comparable APs. Same-SSID roaming (a closer BSS of the
# network we're already on) is left to the driver; we only jump between distinct
# saved SSIDs, which the driver can't do on its own.
_ROAM_SCAN_INTERVAL = 60      # seconds between roam scans while associated
_ROAM_SWITCH_MARGIN_DBM = 12  # a rival saved SSID must beat the current link by
                              # this much (dBm) before we switch to it
_roam_scan = {"next_ok": 0.0}

# The relay must never run on a single live radio — it would park the device's
# only AP and lock out whoever enabled it over WiFi. But one visible radio
# right after boot usually means the drivers haven't registered both phys yet
# (this process starts before they settle), so the keeper only parks an
# enabled STA after the deficit has held for this many consecutive ~20s
# ticks. A real removal (PCIe card pulled) still auto-parks within a minute;
# a slow boot just delays the relay instead of silently disabling it.
_RADIO_DEFICIT_PARK_TICKS = 3
# "parked" records that it was this keeper (not the user) that disabled the
# STA for the deficit, so the return of the second radio can re-engage it
# directly rather than via the scan path.
_radio_deficit = {"ticks": 0, "parked": False}

# STA-vif watchdog. UCI can say the STA is enabled while its netdev simply
# does not exist: observed on the onboard AIC8800 (USB) when a wireless
# restart lands during boot bring-up — netifd re-sets-up the radio without
# preparing the STA interface, reports the radio "up", and never retries.
# Scanning cannot dig us out of that hole (there is no STA netdev to scan
# from, and the fallback AP-mode scan on the other radio misses SSIDs), so
# the keeper used to sit in no_saved_in_range/scan_backoff until the user
# clicked reconnect. What the manual reconnect actually fixes is the reload:
# `wifi reload` + ifup recreates the vif and wpa_supplicant joins on its own.
# The watchdog does that re-assert automatically — only after the absence
# has persisted past a grace window (boot/reload transients settle first,
# reconcile ticks every ~20s), and with exponential backoff between attempts
# so a truly wedged driver can't turn this into a reload storm.
_VIF_MISSING_GRACE = 45          # seconds the netdev must stay missing (~2-3 ticks)
_VIF_RESCUE_BACKOFF_START = 60   # seconds until a failed rescue is retried
_VIF_RESCUE_BACKOFF_CAP = 600
_vif_rescue = {"missing_since": 0.0, "fails": 0, "next_ok": 0.0}


def _scan_backoff_hit():
    """Register a fruitless scan; returns nothing.

    The delay never exceeds _SCAN_BACKOFF_CAP (60s), however long the device
    has been away from every saved network -- see the note there. This is a
    floor on responsiveness, not a growth curve: arriving anywhere known must
    rejoin within about a tick.
    """
    _scan_backoff["fails"] += 1
    delay = min(_SCAN_BACKOFF_CAP, 60 * (2 ** min(_scan_backoff["fails"], 4)))
    _scan_backoff["next_ok"] = time.time() + delay


def _scan_backoff_reset():
    _scan_backoff["fails"] = 0
    _scan_backoff["next_ok"] = 0.0

# wpad/wpa_supplicant binaries to probe for EAP method support.
_WPA_SUPPLICANT_PATHS = ("/usr/sbin/wpa_supplicant", "/sbin/wpa_supplicant",
                         "/usr/sbin/wpad", "/sbin/wpad")
_eap_supported_cache = None

# htmode/channel used while the radio operates as a STA on a new band; the
# original AP values are restored from the backup when leaving smart_wan.
_BAND_DEFAULTS = {
    "2g": {"htmode": "HE20", "channel": "1"},
    "5g": {"htmode": "HE80", "channel": "36"},
}

_RADIO_BACKUP_KEY = "wifi_wan_radio_backup"


def _radio_by_class(cls):
    """UCI radio for a physical class ('onboard'/'pcie'), matched by device
    path (radioN numbering is not guaranteed stable). Reads the cached wireless
    snapshot rather than forking one 'uci get' per radio -- the radio device
    path is static hardware topology and this runs on hot status/poll paths."""
    cfg = uci_show_dict("wireless")
    for radio in ("radio0", "radio1"):
        path = cfg.get("wireless.{}.path".format(radio), "")
        if not path:
            continue
        if ("pcie" in path) == (cls == "pcie"):
            return radio
    return None


def get_sta_radio():
    """The radio that carries the upstream STA link. User-selectable
    (onboard by default); falls back to the onboard radio, then radio0."""
    import pcat_config
    return (_radio_by_class(pcat_config.get_wifi_wan_radio())
            or _radio_by_class("onboard") or "radio0")


# Factory-fresh AP sections carry these SSIDs with no encryption; they must
# not go on air when a freed radio's hotspot is switched back on.
_PLACEHOLDER_SSIDS = ("", "OpenWrt", "LEDE")


def _ap_ssid_from(radio, src_ssid):
    """The firmware's own AP name for `radio`, derived from the other radio's
    SSID: <device> on the onboard radio, <device>-5g on the PCIe card. The
    source's own suffix is stripped first, so copying from a '-5g' neighbour
    can never stack a second one (issue #115)."""
    from pcat_util import wifi_radio
    return wifi_radio.ap_ssid_for(uci_show_dict("wireless", max_age=0), radio,
                                  wifi_radio.ssid_stem(src_ssid))


def _repair_stacked_ssid(radio):
    """Collapse a doubled band suffix on this radio's AP so leaving the relay
    puts the firmware's own name back on air instead of whatever an earlier
    mis-branding left behind. Returns True if the section changed."""
    from pcat_util import wifi_radio
    renames = wifi_radio.stacked_suffix_renames(
        uci_show_dict("wireless", max_age=0), [radio])
    for vif, old, new in renames:
        uci_set("wireless.{}.ssid={}".format(vif, new))
        pcat_logger.logger.info(
            "wifi_wan: repaired {} AP name '{}' -> '{}'".format(vif, old, new))
    return bool(renames)


def _ensure_ap_configured(radio):
    """Seed an unconfigured AP section from the other radio's AP so a freed
    radio recovers the device's photonicat2-<serial> style identity (same
    password/security, band suffix matching this radio) instead of
    broadcasting an open 'OpenWrt' network. A user-configured SSID is left
    untouched."""
    other = "radio1" if radio == "radio0" else "radio0"
    ssid = uci_get("wireless.default_{}.ssid".format(radio))
    if ssid not in _PLACEHOLDER_SSIDS:
        return False
    src_ssid = uci_get("wireless.default_{}.ssid".format(other))
    if src_ssid in _PLACEHOLDER_SSIDS:
        return False  # nothing sane to copy from
    new_ssid = _ap_ssid_from(radio, src_ssid)
    uci_set("wireless.default_{}.ssid={}".format(radio, new_ssid))
    src_enc = uci_get("wireless.default_{}.encryption".format(other))
    src_key = uci_get("wireless.default_{}.key".format(other))
    if src_enc not in ("", "none") and src_key:
        uci_set("wireless.default_{}.encryption={}".format(radio, src_enc))
        uci_set("wireless.default_{}.key={}".format(radio, src_key))
        ieee80211w = uci_get("wireless.default_{}.ieee80211w".format(other))
        if ieee80211w:
            uci_set("wireless.default_{}.ieee80211w={}".format(
                radio, ieee80211w))
    pcat_logger.logger.info(
        "wifi_wan: seeded {} AP from {} (ssid '{}')".format(
            radio, other, new_ssid))
    return True


def set_sta_radio(cls):
    """Move the upstream STA to the other physical radio ('onboard'/'pcie').
    The freed radio gets its band restored from backup and its AP switched
    back on (the radios swap roles: one relays upstream, the other serves
    clients); the new radio is retuned and its AP parked. Returns True when
    the radio actually moved."""
    import pcat_config
    if cls not in ("onboard", "pcie"):
        return False
    if cls == pcat_config.get_wifi_wan_radio():
        return False
    # Require live hardware, not just a UCI section: a removed PCIe card
    # leaves wireless.radio1 behind, and moving the upstream onto it would
    # take WiFi-as-WAN down with no way back except re-selecting the other
    # radio. _radio_by_class() itself stays section-based on purpose — it is
    # what pins an already-configured STA to its own radio, and making it
    # hardware-aware would silently retarget that STA at the onboard radio
    # and park the AP that is still serving clients.
    target = _radio_by_class(cls)
    if target is None or _phy_for_radio(target) is None:
        pcat_logger.logger.warning("wifi_wan: no live {} radio present".format(cls))
        return False

    was_enabled = is_sta_enabled()
    old_radio = (uci_get("wireless.{}.device".format(STA_SECTION))
                 or get_sta_radio())
    if was_enabled:
        # Restores the old radio's band/channel *and* its original AP
        # identity from the backup taken when it became the upstream.
        _restore_radio_state()
        # Swap roles: the freed radio goes back to serving clients even if
        # its AP happened to be off before it became the upstream. Belt and
        # braces if the backup had no AP snapshot to restore from -- this
        # radio must never go on air as an open factory 'OpenWrt'.
        _ensure_ap_configured(old_radio)
        uci_set("wireless.{}.disabled=0".format(old_radio))
        uci_set("wireless.default_{}.disabled=0".format(old_radio))
    pcat_config.set_wifi_wan_radio(cls)
    radio = get_sta_radio()

    if uci_get("wireless.{}".format(STA_SECTION)) == "wifi-iface":
        uci_set("wireless.{}.device={}".format(STA_SECTION, radio))
    if was_enabled:
        _backup_radio_state(radio)
        ssid = uci_get("wireless.{}.ssid".format(STA_SECTION))
        saved = next((n for n in pcat_config.get_wifi_wan_networks()
                      if n.get("ssid") == ssid), None)
        if saved:
            _tune_radio_for_band(radio, saved.get("band", ""), saved.get("channel"))
        uci_set("wireless.{}.disabled=0".format(radio))
        uci_set("wireless.default_{}.disabled=1".format(radio))
    uci_commit("wireless")
    if was_enabled:
        _reload_wifi()
        _ifup(NET_IFACE)
    pcat_logger.logger.info("wifi_wan: upstream STA moved to {} radio ({})".format(cls, radio))
    return True


def is_available():
    """Smart WAN needs two radios: the onboard STA uplink plus another
    radio (PCIe) that keeps serving clients as an AP."""
    try:
        phys = [p for p in os.listdir("/sys/class/ieee80211") if p.startswith("phy")]
        return len(phys) >= 2
    except OSError:
        return False


def _wireless_cfg():
    """Cached snapshot of the whole wireless UCI config. These status helpers
    run on every dashboard/status-bar refresh; per-key 'uci get' forked a
    process per call, this costs one 'uci show' fork per cache window."""
    return uci_show_dict("wireless")


def is_configured():
    cfg = _wireless_cfg()
    return (cfg.get("wireless.{}".format(STA_SECTION), "") == "wifi-iface"
            and cfg.get("wireless.{}.ssid".format(STA_SECTION), "") != "")


def is_sta_enabled():
    return is_configured() and _wireless_cfg().get(
        "wireless.{}.disabled".format(STA_SECTION), "") != "1"


def is_active():
    """True when the upstream WiFi STA is live — i.e. the priority table's
    wifi slot is enabled (priority mode is the upstream-WiFi "Smart WAN"
    replacement). While active the STA radio's own AP is parked, so callers
    should present that radio as disabled in the UI."""
    try:
        import pcat_config
        if pcat_config.get_network_mode() != "priority":
            return False
    except Exception:
        return False
    return is_sta_enabled()


def active_sta_radio():
    """The radio currently carrying the Smart WAN STA uplink, or None when
    Smart WAN is not active. The radio's AP is parked while this is set."""
    if not is_active():
        return None
    return _wireless_cfg().get(
        "wireless.{}.device".format(STA_SECTION), "") or get_sta_radio()


# ── scanning ──────────────────────────────────────────────────────────────────

def _radio_ifnames(radio):
    """Live netdev names of a radio from 'network.wireless status'."""
    status = ubus_call("network.wireless", "status") or {}
    names = []
    for iface in (status.get(radio) or {}).get("interfaces", []):
        name = iface.get("ifname")
        if name:
            names.append((name, (iface.get("config") or {}).get("mode", "")))
    return names


def _sta_ifname(radio):
    for name, mode in _radio_ifnames(radio):
        if mode == "sta":
            return name
    return None


def _phy_for_radio(radio):
    """Kernel phy (phy0/phy1) for a UCI radio, matched via its device path.
    Uses the cached wireless snapshot (static path, hot path) instead of a
    per-call 'uci get' fork."""
    radio_path = uci_show_dict("wireless").get("wireless.{}.path".format(radio), "")
    if not radio_path:
        return None
    try:
        phys = sorted(os.listdir("/sys/class/ieee80211"))
    except OSError:
        return None
    for phy in phys:
        try:
            dev = os.readlink("/sys/class/ieee80211/{}".format(phy))
        except OSError:
            continue
        if radio_path in dev:
            return phy
    return None


def _scan_ifname(radio):
    """Best netdev to scan from: the active STA, any netifd-managed iface on
    this radio, a lingering kernel netdev on this radio's phy, or — when the
    STA radio has no netdev at all (AP parked, no STA configured yet) — any
    other radio's netdev.  A discovery scan covers all bands from either
    radio, and the join itself always targets the STA radio by UCI name.
    Note: phyN numbering is NOT stable across boots (USB/PCIe enumeration
    race), so phys are matched by device path, never by index."""
    names = _radio_ifnames(radio)
    for name, mode in names:
        if mode == "sta":
            return name
    if names:
        return names[0][0]
    devices = (ubus_call("iwinfo", "devices") or {}).get("devices", [])
    phy = _phy_for_radio(radio)
    if phy:
        for dev in devices:
            if dev == phy or dev.startswith(phy + "-"):
                return dev
    return devices[0] if devices else None


def _parse_encryption(enc):
    """Map an iwinfo scan encryption block to a UCI encryption value."""
    if not enc or not enc.get("enabled"):
        return "none"
    auth = enc.get("authentication") or []
    wpa = enc.get("wpa") or []
    if "802.1x" in auth or "8021x" in auth:
        return "eap"
    if "sae" in auth:
        # WPA2/WPA3 mixed -> psk2 for broadest STA compatibility
        return "psk2" if "psk" in auth else "sae"
    if "psk" in auth:
        return "psk2" if (2 in wpa or 3 in wpa) else "psk"
    return "wep" if not wpa else "psk2"


def scan(radio=None):
    """
    Scan for nearby APs from the STA radio.
    Returns (networks, error): networks sorted by signal, deduped per
    (ssid, band) keeping the strongest BSS; error is None or a reason string.
    """
    radio = radio or get_sta_radio()
    ifname = _scan_ifname(radio)
    if not ifname:
        return [], "radio_disabled"
    results = _ubus_scan(ifname)
    if not results:
        # scans on a busy radio occasionally come back empty; retry once
        results = _ubus_scan(ifname)

    # Joining one of the device's own APs would create a loop
    own_ssids = set()
    for own in ("default_radio0", "default_radio1"):
        own_ssid = uci_get("wireless.{}.ssid".format(own))
        if own_ssid:
            own_ssids.add(own_ssid)

    best = {}
    for r in results:
        ssid = r.get("ssid", "")
        if not ssid or ssid in own_ssids:
            continue  # hidden networks are joined manually; own APs excluded
        band = {2: "2g", 5: "5g", 6: "6g"}.get(r.get("band"), "")
        encryption = _parse_encryption(r.get("encryption"))
        net = {
            "ssid": ssid,
            "bssid": r.get("bssid", ""),
            "band": band,
            "channel": r.get("channel", 0),
            "signal": r.get("signal", -100),
            "quality": r.get("quality", 0),
            "quality_max": r.get("quality_max", 70),
            "encryption": encryption,
            "supported": encryption in SUPPORTED_ENCRYPTIONS and band in _BAND_DEFAULTS,
        }
        key = (ssid, band)
        if key not in best or net["signal"] > best[key]["signal"]:
            best[key] = net
    networks = sorted(best.values(), key=lambda n: n["signal"], reverse=True)
    return networks, None


def _ubus_scan(ifname):
    # Subprocess on purpose: a scan blocks 3-6s and must not hold the shared
    # native-ubus lock; it also needs a longer timeout than ubus_call's 5s.
    try:
        out = subprocess.check_output(
            ["ubus", "call", "iwinfo", "scan", json.dumps({"device": ifname})],
            timeout=20, text=True)
        return (json.loads(out) if out.strip() else {}).get("results", [])
    except Exception as e:
        pcat_logger.logger.warning("wifi_wan: scan on {} failed: {}".format(ifname, e))
        return []


# ── radio backup / restore ────────────────────────────────────────────────────

# AP identity options carried through the relay. The radio's own AP section is
# parked (and on the onboard radio torn down and regenerated from board
# defaults) while it serves as the STA, so the customer's SSID/password are
# snapshotted here and written back verbatim on exit -- otherwise the AP comes
# back as a factory-stock open 'OpenWrt' (issue #112).
_AP_IDENTITY_OPTS = ("ssid", "encryption", "key", "hidden", "ieee80211w")


def _backup_radio_state(radio):
    """Snapshot AP band/htmode/channel and the AP's identity (SSID/security)
    before the STA takes the radio over."""
    import pcat_config
    if pcat_config.get_user_config_key(_RADIO_BACKUP_KEY):
        return  # already relaying; keep the original AP snapshot
    backup = {
        "radio": radio,
        "band": uci_get("wireless.{}.band".format(radio)),
        "htmode": uci_get("wireless.{}.htmode".format(radio)),
        "channel": uci_get("wireless.{}.channel".format(radio)),
        "ap_disabled": uci_get("wireless.default_{}.disabled".format(radio)),
    }
    # Never snapshot a placeholder over a real identity: if the AP section has
    # already been regenerated to factory 'OpenWrt' by the time we get here,
    # recording it would make the restore a no-op that cements the bug.
    ap_ssid = uci_get("wireless.default_{}.ssid".format(radio))
    if ap_ssid not in _PLACEHOLDER_SSIDS:
        backup["ap"] = {
            opt: uci_get("wireless.default_{}.{}".format(radio, opt))
            for opt in _AP_IDENTITY_OPTS
        }
    pcat_config.set_user_config_key(_RADIO_BACKUP_KEY, backup)
    # Log without the AP key: this line goes to the system log.
    pcat_logger.logger.info("wifi_wan: saved radio backup {}".format(
        dict(backup, ap=_redact_ap(backup.get("ap")))))


def _redact_ap(ap):
    """AP snapshot with the passphrase masked, for logging."""
    if not ap:
        return ap
    return dict(ap, key="***" if ap.get("key") else "")


def saved_ap_identity(radio):
    """The AP identity snapshotted before `radio` became the upstream relay,
    or None if there isn't one for this radio.

    Exposed for the boot reconcile. When the firmware regenerates a parked AP
    section as factory 'OpenWrt' -- which is what happens to a radio whose
    device section gets pruned while its AP is parked -- this snapshot is the
    only surviving record of the customer's own SSID and passphrase, and
    restoring from it is the difference between the hotspot coming back and
    the customer re-pairing every device in the house.
    """
    import pcat_config
    backup = pcat_config.get_user_config_key(_RADIO_BACKUP_KEY)
    if not isinstance(backup, dict) or backup.get("radio") != radio:
        return None
    ap = backup.get("ap")
    if not isinstance(ap, dict) or not ap.get("ssid"):
        return None
    return dict(ap)


def saved_radio_state(radio):
    """{band, htmode, channel} snapshotted before `radio` became the upstream
    relay, or None. Only the keys that were actually recorded are returned.

    Companion to saved_ap_identity() for the same reason: a regenerated radio
    section comes back on the firmware's default band, and an onboard module
    that only does 2.4 GHz will not come up at all on the 5 GHz default -- the
    radio would be "restored" and still missing.
    """
    import pcat_config
    backup = pcat_config.get_user_config_key(_RADIO_BACKUP_KEY)
    if not isinstance(backup, dict) or backup.get("radio") != radio:
        return None
    state = {key: backup.get(key) for key in ("band", "htmode", "channel")
             if backup.get(key)}
    return state or None


def _restore_ap_identity(radio, backup):
    """Write the customer's original SSID/security back onto the freed radio's
    AP section. Returns True if anything changed.

    Only fills in what the live section lost: a user who reconfigured the
    hotspot while the relay was running keeps their new settings (the live
    section is then intact and is left alone)."""
    ap = (backup or {}).get("ap")
    if not ap or not ap.get("ssid"):
        return False
    live_ssid = uci_get("wireless.default_{}.ssid".format(radio))
    live_enc = uci_get("wireless.default_{}.encryption".format(radio))
    live_key = uci_get("wireless.default_{}.key".format(radio))
    # A regenerated section shows up two ways: the name reverts to a factory
    # placeholder, OR the name survives but the security is gone -- the latter
    # would otherwise put the customer's hotspot name on air as an OPEN
    # network, which is worse than the wrong name. Only treat the live section
    # as the user's own when it kept both its name and its security.
    lost_name = live_ssid in _PLACEHOLDER_SSIDS
    lost_security = (ap.get("encryption") not in ("", "none")
                     and (live_enc in ("", "none") or not live_key))
    if not lost_name and not lost_security:
        return False  # intact, user-visible AP is already there
    # The section itself can be gone entirely, not merely reset.
    if uci_get("wireless.default_{}".format(radio)) != "wifi-iface":
        uci_set("wireless.default_{}=wifi-iface".format(radio))
        uci_set("wireless.default_{}.device={}".format(radio, radio))
        uci_set("wireless.default_{}.network=lan".format(radio))
        uci_set("wireless.default_{}.mode=ap".format(radio))
    for opt in _AP_IDENTITY_OPTS:
        val = ap.get(opt)
        # A hotspot renamed while the relay ran keeps the new name: only the
        # security is being repaired here, so don't roll the name back.
        if opt == "ssid" and not lost_name:
            continue
        if val:
            uci_set("wireless.default_{}.{}={}".format(radio, opt, val))
        else:
            uci_delete("wireless.default_{}.{}".format(radio, opt))
    pcat_logger.logger.info(
        "wifi_wan: restored {} AP {} (ssid '{}')".format(
            radio, "identity" if lost_name else "security",
            uci_get("wireless.default_{}.ssid".format(radio))))
    return True


def _unpark_ap_without_backup(radio):
    """Last-resort un-park for a radio whose backup was lost (config reset,
    upgrade, an interrupted write). Without this the AP stays parked forever:
    _restore_radio_state() has nothing to restore from, and the settings-page
    toggle can't help either -- enable_wifi_interface() no-ops because the
    radio already reads as disabled. That strands the user with no hotspot and
    no way back through the UI. Returns True if anything changed."""
    if uci_get("wireless.default_{}.disabled".format(radio)) != "1":
        return False
    # Recover an identity first so un-parking can't put a factory-stock open
    # 'OpenWrt' on air; if there is nothing to copy from, leave the AP parked
    # rather than broadcast one.
    if (uci_get("wireless.default_{}.ssid".format(radio)) in _PLACEHOLDER_SSIDS
            and not _ensure_ap_configured(radio)):
        pcat_logger.logger.warning(
            "wifi_wan: {} AP parked with no backup and no identity to "
            "recover; leaving it off rather than broadcasting 'OpenWrt'".format(radio))
        return False
    uci_set("wireless.{}.disabled=0".format(radio))
    uci_delete("wireless.default_{}.disabled".format(radio))
    pcat_logger.logger.warning(
        "wifi_wan: un-parked {} AP with no backup present".format(radio))
    return True


def _restore_radio_state():
    """Put the AP back on its original band/channel. Returns True if changed."""
    import pcat_config
    backup = pcat_config.get_user_config_key(_RADIO_BACKUP_KEY)
    if not backup:
        # The relay is being torn down but the snapshot is gone. Don't leave a
        # parked AP behind -- that is a hotspot the user cannot switch back on
        # -- and don't un-park it under a mis-branded name either.
        radio = get_sta_radio()
        repaired = _repair_stacked_ssid(radio)
        return _unpark_ap_without_backup(radio) or repaired
    radio = backup.get("radio", "radio0")
    _repair_stacked_ssid(radio)
    for key in ("band", "htmode", "channel"):
        val = backup.get(key)
        if val:
            uci_set("wireless.{}.{}={}".format(radio, key, val))
    # Put the customer's hotspot name/password back before un-parking the AP,
    # so it never goes on air as a factory-stock open 'OpenWrt' (issue #112).
    if not _restore_ap_identity(radio, backup):
        # No usable snapshot (upgraded from a build that predates it, or the
        # AP was already a placeholder when the relay started): fall back to
        # deriving the identity from the other radio's AP.
        _ensure_ap_configured(radio)
    if backup.get("ap_disabled") == "1":
        uci_set("wireless.default_{}.disabled=1".format(radio))
    else:
        uci_delete("wireless.default_{}.disabled".format(radio))
    pcat_config.set_user_config_key(_RADIO_BACKUP_KEY, None)
    pcat_logger.logger.info("wifi_wan: restored radio state from backup")
    return True


# ── mwan3 ─────────────────────────────────────────────────────────────────────
# Route selection is pure metric routing; mwan3 (whose stock config would
# blackhole all egress when its only tracked interface dies) is stopped and
# disabled outright by wan_manager.disable_mwan3().

def _disable_mwan3():
    from pcat_util import wan_manager
    wan_manager.disable_mwan3()


# ── UCI plumbing (network interface + firewall zone) ─────────────────────────

def _ensure_network_ifaces():
    metric_v4, metric_v6, _demoted = _metrics()
    changed = False
    if uci_get("network.{}".format(NET_IFACE)) != "interface":
        uci_set("network.{}=interface".format(NET_IFACE))
        changed = True
    # norelease: a netifd re-prepare of the STA SIGTERMs udhcpc, which
    # RELEASEs the lease before the fresh client DISCOVERs — the upstream
    # server then hands out a different address each bounce and the
    # router's WAN IP wanders. Keeping the lease bound gets the same
    # address re-offered.
    for key, val in (("proto", "dhcp"), ("metric", str(metric_v4)),
                     ("norelease", "1")):
        if uci_get("network.{}.{}".format(NET_IFACE, key)) != val:
            uci_set("network.{}.{}={}".format(NET_IFACE, key, val))
            changed = True
    if uci_get("network.{}".format(NET_IFACE_V6)) != "interface":
        uci_set("network.{}=interface".format(NET_IFACE_V6))
        changed = True
    for key, val in (("proto", "dhcpv6"), ("device", "@" + NET_IFACE),
                     ("metric", str(metric_v6)), ("reqaddress", "try"),
                     ("reqprefix", "auto"), ("sourcefilter", "0")):
        if uci_get("network.{}.{}".format(NET_IFACE_V6, key)) != val:
            uci_set("network.{}.{}={}".format(NET_IFACE_V6, key, val))
            changed = True
    if changed:
        uci_commit("network")
    return changed


def _wan_zone_index():
    for i in range(20):
        name = uci_get("firewall.@zone[{}].name".format(i))
        if not name:
            return None
        if name == "wan":
            return i
    return None


def _ensure_firewall():
    idx = _wan_zone_index()
    if idx is None:
        pcat_logger.logger.error("wifi_wan: firewall wan zone not found")
        return False
    networks = uci_get("firewall.@zone[{}].network".format(idx)).split()
    changed = False
    for net in (NET_IFACE, NET_IFACE_V6):
        if net not in networks:
            uci_add_list("firewall.@zone[{}].network={}".format(idx, net))
            changed = True
    if changed:
        uci_commit("firewall")
        try:
            subprocess.run(["/etc/init.d/firewall", "reload"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except Exception as e:
            pcat_logger.logger.warning("wifi_wan: firewall reload failed: {}".format(e))
    return True


def _reload_wifi():
    try:
        subprocess.run(["/sbin/wifi", "reload"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception as e:
        pcat_logger.logger.warning("wifi_wan: wifi reload failed: {}".format(e))


def _ifup(iface):
    try:
        subprocess.run(["ifup", iface], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=10)
    except Exception:
        pass


def _ifdown(iface):
    try:
        subprocess.run(["ifdown", iface], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=10)
    except Exception:
        pass


def _commit_wireless_verified():
    """Commit the wireless config and verify the STA section actually reached
    the persistent file. A commit can die quietly (subprocess timeout under
    the join's own wifi-reload CPU spike, transient uci error); the change
    then lives only in /tmp/.uci staging — uci show and netifd both honor it,
    so everything works until a reboot wipes /tmp and the upstream config
    evaporates ("have to reconnect after every restart"). Retry once and log
    loudly if the section still isn't on flash."""
    for attempt in (1, 2):
        uci_commit("wireless")
        try:
            with open("/etc/config/wireless") as f:
                if "'{}'".format(STA_SECTION) in f.read():
                    return True
        except OSError:
            pass
        pcat_logger.logger.error(
            "wifi_wan: wireless commit did not reach /etc/config/wireless "
            "(attempt {})".format(attempt))
    return False


def _tune_radio_for_band(radio, band, channel=None):
    """Point the STA radio at the upstream's band (the STA scans the whole
    band to find the AP; channel is just a starting hint)."""
    if band in _BAND_DEFAULTS:
        uci_set("wireless.{}.band={}".format(radio, band))
        uci_set("wireless.{}.htmode={}".format(radio, _BAND_DEFAULTS[band]["htmode"]))
        uci_set("wireless.{}.channel={}".format(
            radio, channel or _BAND_DEFAULTS[band]["channel"]))


# ── random MAC (stable per SSID, iPhone "Private Wi-Fi Address" style) ─────────

def stable_mac_for_ssid(ssid):
    """Deterministic locally-administered MAC derived from the SSID. Stable
    across rejoins so captive-portal logins and DHCP leases survive reconnects
    (a fresh-random MAC every join would force re-login each time)."""
    digest = hashlib.sha256(("pcat-wifi-wan:" + ssid).encode("utf-8")).digest()
    octets = list(digest[:6])
    octets[0] = (octets[0] | 0x02) & 0xFE  # locally administered, unicast
    return ":".join("{:02x}".format(b) for b in octets)


_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def valid_custom_mac(mac):
    """Normalize a user-supplied STA MAC, or '' if it is unusable.

    Accepts aa:bb / aa-bb separators and any case; rejects multicast
    (group-bit) and the all-zero/broadcast addresses, which drivers refuse
    or hostapd treats specially. Deliberately allows globally-administered
    addresses: the point of a custom MAC is joining a network that only
    admits a specific real device's address."""
    mac = (mac or "").strip().lower().replace("-", ":")
    if not _MAC_RE.match(mac):
        return ""
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return ""
    if int(mac[:2], 16) & 0x01:
        return ""
    return mac


def pinned_sta_mac(radio):
    """The default (non-random) STA MAC: the radio's permanent hardware MAC
    with the locally-administered bit set.

    Leaving `macaddr` unset does NOT mean "hardware MAC": with more than one
    vif on the phy (the relay parks an AP section on the same radio),
    mac80211 hands out order-DERIVED addresses, and the order shifts as the
    AP is parked/unparked — so every reconnect could show a different MAC and
    pull a fresh DHCP lease (observed on-device as the router's IP wandering
    per roam). Pinning must therefore be explicit. The local bit keeps the
    pin distinct from the base MAC so an AP vif on the same phy (which gets
    the base) can never collide with it."""
    phy = _phy_for_radio(radio)
    if phy is None:
        return None
    try:
        with open("/sys/class/ieee80211/{}/macaddress".format(phy)) as f:
            octets = f.read().strip().split(":")
        octets[0] = "{:02x}".format((int(octets[0], 16) | 0x02) & 0xFE)
        return ":".join(octets)
    except (OSError, ValueError, IndexError):
        return None


# ── EAP (802.1x / WPA-Enterprise) capability ──────────────────────────────────

def eap_supported():
    """True when the on-device wpa_supplicant/wpad can do EAP. wpad-mini and
    wpad-basic-* cannot; only the full builds embed the EAP methods. Detected by
    probing the binary for an EAP method string. Cached for the process."""
    global _eap_supported_cache
    if _eap_supported_cache is not None:
        return _eap_supported_cache
    result = False
    for path in _WPA_SUPPLICANT_PATHS:
        if not os.path.exists(path):
            continue
        try:
            rc = subprocess.run(["grep", "-qa", "EAP-PEAP", path],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5).returncode
            if rc == 0:
                result = True
                break
        except Exception:
            continue
    _eap_supported_cache = result
    pcat_logger.logger.info("wifi_wan: eap_supported={}".format(result))
    return result


# ── connectivity probe / captive-portal detection ─────────────────────────────

# Transparent-proxy escape (OpenClash / passwall / ssr-plus and friends).
# Those tools poison the system resolver (fake-ip answers in 198.18.0.0/15)
# and redirect ALL locally-generated TCP to a listener on loopback. A probe
# socket bound to the WAN netdev can never complete that hairpin (loopback is
# unreachable through a device-bound socket), so every bound probe times out
# and the upstream is reported offline — and demoted — while the user's
# traffic, which does flow through the proxy, works fine. Two facts, verified
# on hardware against OpenClash's live nft ruleset, give a clean escape:
#   * the proxy's own loop-avoidance rule (`meta skgid 65534 return`) exempts
#     sockets owned by gid 65534 ("nobody") from the redirect, and
#   * curl can resolve via DoH at a FIXED-IP resolver (--doh-url), which
#     sidesteps the poisoned resolver without needing a working UDP :53
#     (that is also hijacked). The DoH transfer inherits --interface and the
#     gid, so the whole probe still tests the one netdev it was asked about.
_PROXY_BYPASS_GID = 65534
_DOH_URLS = ("https://223.5.5.5/dns-query", "https://1.1.1.1/dns-query")
# Default fake-ip pool of clash/mihomo (198.18.0.1/16) sits in this /15.
_FAKEIP_NET = ipaddress.ip_network("198.18.0.0/15")
_CONN_CHECK_HOST = "4.photonicat.com"


def _fakeip_poisoned():
    """True when the system resolver answers our probe host with a fake-ip —
    the positive fingerprint of a transparent proxy in fake-ip mode. Cheap:
    the answer comes from local dnsmasq/clash, no external round trip."""
    try:
        addr = socket.getaddrinfo(_CONN_CHECK_HOST, 80, socket.AF_INET,
                                  socket.SOCK_STREAM)[0][4][0]
        return ipaddress.ip_address(addr) in _FAKEIP_NET
    except Exception:
        return False


def _curl_probe(url, l3dev, doh_url=None, bypass_gid=None):
    """Return (http_code, body, redirect_url) curl gets for `url` over the
    given netdev. Redirects are NOT followed (no -L), so a captive portal's
    30x comes back as a small redirect response rather than its full login
    page; its Location target is surfaced as redirect_url ('' when none) so
    the portal-access pin knows where the login page lives. ('000', '', '')
    on any failure (timeout, no route, TLS reject). `doh_url`/`bypass_gid`
    select the transparent-proxy-escaping form (see block comment above)."""
    cmd = ["curl", "-s", "-w", "\n%{http_code}\t%{redirect_url}",
           "--max-filesize", "65536",
           "--interface", l3dev, "--max-time", "5"]
    if doh_url:
        cmd += ["--doh-url", doh_url]
    cmd.append(url)
    run_kw = dict(capture_output=True, text=True, timeout=8)
    if bypass_gid is not None:
        # Only the child curl gets the gid; needs root, which we run as.
        run_kw["group"] = bypass_gid
    try:
        # Default curl writes the body to stdout; -w appends the status code
        # (and redirect target) on its own line after it, so we can split the
        # parts apart. A body can contain tabs, the -w line's layout is ours.
        out = subprocess.run(cmd, **run_kw).stdout
        nl = out.rfind("\n")
        if nl == -1:
            return "000", "", ""
        code, _tab, redirect = out[nl + 1:].strip().partition("\t")
        return code.strip(), out[:nl].strip(), redirect.strip()
    except Exception:
        return "000", "", ""


def _looks_like_ip_echo(body):
    """True when `body` is exactly an IP address — what our /ip.php endpoint
    returns. A captive-portal login page never is."""
    body = (body or "").strip()
    if not body or len(body) > 64:
        return False
    try:
        ipaddress.ip_address(body)
        return True
    except ValueError:
        return False


# Where the last-seen captive portal points its victims. Captured from the
# probe's redirect (or, failing that, a meta-refresh/JS redirect in the body),
# consumed by the portal-access pin below and surfaced to the UI as a tappable
# login link. Cleared whenever the verdict leaves 'portal' or the STA rejoins.
_portal_seen = {"url": ""}

# Portals redirect to hostnames only the hotspot's own DNS knows; anything a
# Location header can carry lands here, so accept only a syntactically plain
# hostname before it may be written into a dnsmasq config line.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

_META_REFRESH_RE = re.compile(
    r"http-equiv\s*=\s*[\"']?refresh[\"']?[^>]*?"
    r"content\s*=\s*[\"'][^\"']*?url\s*=\s*([^\"'\s>]+)",
    re.IGNORECASE | re.DOTALL)
_JS_REDIRECT_RE = re.compile(
    r"(?:window\.location(?:\.href)?|location\.href)\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE)


def _url_host(url):
    """Lowercased hostname of an http(s) URL, '' for anything else."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return ""
        return (parts.hostname or "").strip(".").lower()
    except ValueError:
        return ""


def _portal_url_from_body(body):
    """Best-effort login URL from a portal that answers 200 in place instead
    of redirecting: meta-refresh first, then the common JS location assignment.
    Returns an absolute URL or ''."""
    for pat in (_META_REFRESH_RE, _JS_REDIRECT_RE):
        m = pat.search(body or "")
        if m:
            return urljoin(_CONN_CHECK_URL, m.group(1).strip())
    return ""


def _note_portal_url(url, record=None, dev=""):
    """Record where the captive portal sends clients. First capture of an
    episode wins (later probe rounds may answer 200-in-place with no target);
    the record is cleared when the verdict leaves 'portal'. Our own check host
    is never a portal, so a redirect staying on it is ignored.

    `record` selects which {'url': ...} slot the capture lands in. It defaults
    to the STA's own _portal_seen, but wan_manager probes the wired/cellular
    slots through this same code and their login page belongs to *that* slot,
    not to the upstream WiFi episode (whose keeper clears _portal_seen on
    every non-portal verdict, so a shared record would lose it anyway)."""
    if record is None:
        record = _portal_seen
    url = (url or "").strip()
    if not url or record["url"]:
        return
    host = _url_host(url)
    if not host or host == _CONN_CHECK_HOST:
        return
    record["url"] = url
    pcat_logger.logger.info("wifi_wan: captive portal on {} points at {}".format(
        dev or "upstream", url))


def _probe_once(l3dev, doh_url=None, bypass_gid=None, record=None):
    """One probe round. Returns 'online', 'portal', or 'offline'."""
    # A valid TLS 200 can't be a spoofed portal page => really online.
    code, _body, _redir = _curl_probe(_CONN_CHECK_URL_HTTPS, l3dev, doh_url,
                                      bypass_gid)
    if code == "200":
        return "online"
    # No TLS success: associated but TLS was blocked or hiccuped. Fall back to
    # plain HTTP and use the BODY to tell three cases apart:
    code, body, redirect = _curl_probe(_CONN_CHECK_URL, l3dev, doh_url,
                                       bypass_gid)
    if not code or code == "000":
        return "offline"                       # dead uplink: nothing answers
    if code == "200" and _looks_like_ip_echo(body):
        # Our own IP echo came back over HTTP => the uplink really reaches us and
        # only TLS glitched. Treat as online, not as a portal (this misclassif-
        # ication was a big source of needless failover to cellular).
        return "online"
    # A redirect, or a 200 whose body is someone else's page => captive portal.
    _note_portal_url(redirect or _portal_url_from_body(body), record, l3dev)
    return "portal"


def _probe_connectivity(l3dev, record=None):
    """Probe the upstream for real internet, retrying transient failures so a
    single dropped packet doesn't flip the verdict. Returns 'online' (verified),
    'portal' (associated but intercepted => captive portal), or 'offline' (no
    usable internet). Bound to the wan_wifi netdev so it tests the upstream
    specifically, not whatever the default route happens to be. Biased toward
    'online': any attempt that confirms internet wins immediately.

    When a transparent proxy is hijacking router-self traffic the plain bound
    probe is structurally dead (see _PROXY_BYPASS_GID block comment), so:
    fake-ip poisoning detected => go straight to the hardened form (the plain
    one would just burn its full timeout every round); otherwise probe plainly
    and use one hardened round as arbitration before calling the link bad, so
    a proxy in a non-fake-ip mode can't fail a healthy uplink either. The
    hardened round rotates DoH resolvers so one blocked resolver can't wedge
    the verdict.

    `record` is passed through to _note_portal_url: a caller probing some other
    slot's netdev hands in its own {'url': ''} so the login page it finds is
    filed against that slot instead of the STA's episode."""
    if not l3dev:
        return "offline"
    result = "offline"
    if _fakeip_poisoned():
        # Plain rounds would each just burn their full timeout — skip them and
        # spend the whole attempt budget on the hardened form.
        hardened_rounds = _CONN_CHECK_ATTEMPTS
    else:
        for _ in range(_CONN_CHECK_ATTEMPTS):
            result = _probe_once(l3dev, record=record)
            if result == "online":
                return "online"
        # Single arbitration round; a longer hardened retry here would double
        # the worst-case probe time of a genuinely dead uplink, and sustained-
        # failure confirmation is already the flap threshold's job.
        hardened_rounds = 1
    for attempt in range(hardened_rounds):
        hardened = _probe_once(l3dev,
                               doh_url=_DOH_URLS[attempt % len(_DOH_URLS)],
                               bypass_gid=_PROXY_BYPASS_GID,
                               record=record)
        if hardened == "online":
            return "online"
        if result == "offline":
            # 'portal' from either form beats a bare timeout for diagnostics.
            result = hardened
    return result


def connectivity_state():
    """Cached connectivity result for the upstream ('online'/'portal'/'offline'/
    'unknown'). reconcile() refreshes it on its timer; the dashboard reads it
    cheaply without firing a probe on every poll."""
    return _conn_cache["result"]


def portal_url():
    """Login page the current captive-portal episode redirects to, '' when
    none was captured (or the verdict has moved on)."""
    return _portal_seen["url"]


def _refresh_connectivity(l3dev):
    """Probe and update the cached verdict with flap damping. A single bad probe
    round won't drop us out of 'online' — it takes _CONN_FLAP_THRESHOLD
    consecutive bad rounds, so a brief glitch can't bounce egress between WiFi
    and cellular. Recovery to 'online' is immediate. The damped verdict is what
    reconcile() acts on (metric demotion / login hook), so the route stays put
    through transient noise."""
    probed = _probe_connectivity(l3dev)
    prev = _conn_cache["result"]

    if probed == "online":
        _conn_cache["bad_streak"] = 0
        result = "online"
    else:
        _conn_cache["bad_streak"] += 1
        # Hold the last good verdict until the failures are sustained. The
        # online->bad transition is damped (that's the costly one that
        # triggers failover), and so is unknown->offline: the first round
        # after boot or a fresh join races DHCP/DNS/clock settling, and a
        # single timed-out probe used to demote a perfectly healthy upstream
        # under cellular — egress flipped to 5G right after startup and back
        # again a tick later, even though WiFi outranks cellular in the
        # priority table. A 'portal' verdict passes through undamped either
        # way: it is a positive HTTP fingerprint (a real answer came back),
        # not a timeout, so it needs no confirmation.
        if prev == "online" and _conn_cache["bad_streak"] < _CONN_FLAP_THRESHOLD:
            result = "online"
        elif prev == "unknown" and probed == "offline" \
                and _conn_cache["bad_streak"] < _CONN_FLAP_THRESHOLD:
            result = "unknown"
        else:
            result = probed

    if result != "portal":
        # The captured login URL belongs to the portal episode; once the
        # verdict moves on (logged in, or the uplink died) it is stale.
        _portal_seen["url"] = ""
    _conn_cache["result"] = result
    _conn_cache["time"] = time.time()
    return result


# ── captive-portal login hook ─────────────────────────────────────────────────

def _run_login_cmd(login_cmd, l3dev):
    """Run a user-supplied per-network auto-login command (Travelmate-style).
    The interface and gateway are exported so the script can target the
    upstream. Best-effort, time-boxed; never raises."""
    if not login_cmd:
        return
    gateway = ""
    ifstatus = ubus_call("network.interface.{}".format(NET_IFACE), "status") or {}
    for route in ifstatus.get("route") or []:
        if route.get("target") in ("0.0.0.0", "") and route.get("nexthop"):
            gateway = route.get("nexthop")
            break
    env = dict(os.environ, WIFI_WAN_IFACE=l3dev or "", WIFI_WAN_GATEWAY=gateway,
               WIFI_WAN_PORTAL_URL=_portal_seen["url"])
    pcat_logger.logger.info("wifi_wan: running captive-portal login hook")
    try:
        subprocess.run(["sh", "-c", login_cmd], env=env, timeout=30,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        pcat_logger.logger.warning("wifi_wan: login hook failed: {}".format(e))


# ── captive-portal access pin ─────────────────────────────────────────────────
# When the upstream is portal-locked, egress is demoted to the next uplink
# (usually cellular) — but the login page must STILL be reachable through the
# WiFi, and with the demotion alone it is not, for two stacked reasons:
#   * DNS: the portal hostname (e.g. "gcppty.gw") exists only in the hotspot's
#     own resolver. wan_manager sets dnsmasq all_servers, so the cellular
#     carrier's instant NXDOMAIN wins the parallel race; and even when the
#     hotspot's answer wins, OpenWrt's default rebind protection drops it for
#     resolving to a private address. Clients see "DNS failure" on the login
#     URL every time.
#   * Routing: with the default route on cellular, only on-link (same-subnet)
#     portal addresses are still reached via the WiFi; anything else — an
#     off-subnet portal or a cloud login host that must be visited FROM the
#     hotspot's network — leaks out over cellular and the login never lands.
# The pin fixes both for exactly the portal episode: a dnsmasq drop-in scopes
# the portal hostname(s) to the hotspot's DHCP resolvers and exempts them from
# rebind protection, and /32 host routes steer the portal chain (plus any
# off-subnet resolver) through the WiFi. Everything lives in /tmp or the
# routing table: a reboot clears it for free, and _clear_portal_access() tears
# it down on login success, roam/rejoin, forget, or disassociation.

_PORTAL_PIN_CONF = "pcat_portal_pin.conf"
# Follow at most this many redirect hops when pinning the login chain (portal
# gateways commonly bounce once more to a cloud login host).
_PORTAL_MAX_HOPS = 3
# url: portal URL the current pin was built for (skip the chain re-walk while
# it is unchanged); routes: /32s we installed, re-asserted each tick because
# an ifup/DHCP renew silently flushes them; conf_path: our dnsmasq drop-in.
_portal_pin = {"url": "", "routes": set(), "conf_path": ""}


def _dnsmasq_confdir():
    """dnsmasq's drop-in config dir (OpenWrt default /tmp/dnsmasq.d; honours a
    user-configured confdir)."""
    return uci_get("dhcp.@dnsmasq[0].confdir") or "/tmp/dnsmasq.d"


def _restart_dnsmasq():
    """dnsmasq re-reads conf-dir files only on restart (reload just rewrites
    the UCI-generated config), so the pin needs a real restart. Sub-second on
    the target; clients retry DNS transparently."""
    try:
        subprocess.run(["/etc/init.d/dnsmasq", "restart"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=30)
    except Exception as e:
        pcat_logger.logger.warning("wifi_wan: dnsmasq restart failed: {}".format(e))


def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _pinnable_ip(ip):
    """True for an IPv4 address it makes sense to route/resolve toward
    (private or public unicast). Refuses loopback/multicast/reserved and
    anything unparsable, since these strings end up in `ip route` argv and
    dnsmasq config lines."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (a.version == 4
            and not (a.is_loopback or a.is_multicast or a.is_unspecified
                     or a.is_reserved or a.is_link_local))


def _wan_wifi_ipv4():
    """(gateway, dns_servers, subnet) of the upstream WiFi's IPv4 lease —
    everything the pin needs to know about the hotspot's network. Fields
    degrade to ''/[]/None when absent."""
    st = ubus_call("network.interface.{}".format(NET_IFACE), "status") or {}
    gateway = ""
    for route in st.get("route") or []:
        if route.get("target") in ("0.0.0.0", "") and route.get("nexthop"):
            gateway = route["nexthop"]
            break
    dns = [d for d in st.get("dns-server") or [] if _pinnable_ip(d)]
    subnet = None
    for addr in st.get("ipv4-address") or []:
        try:
            subnet = ipaddress.ip_network(
                "{}/{}".format(addr.get("address"), addr.get("mask")),
                strict=False)
        except (TypeError, ValueError):
            pass
        break
    return gateway, dns, subnet


def _pin_route(ip, gateway, l3dev, subnet):
    """Steer one address through the WiFi with a /32 host route (most-specific
    wins over any default route, so the demoted metric doesn't matter). On-link
    addresses are skipped — the connected route already reaches them."""
    if not _pinnable_ip(ip) or not l3dev or not gateway:
        return
    try:
        if subnet is not None and ipaddress.ip_address(ip) in subnet:
            return
    except ValueError:
        return
    try:
        r = subprocess.run(
            ["ip", "route", "replace", "{}/32".format(ip),
             "via", gateway, "dev", l3dev],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if r.returncode == 0:
            _portal_pin["routes"].add(ip)
    except Exception:
        pass


def _build_dns_query(host, txid):
    """Minimal RD A/IN query for `host`."""
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b""
    for label in host.strip(".").split("."):
        raw = label.encode("ascii")
        if not raw or len(raw) > 63:
            raise ValueError("bad label")
        qname += bytes([len(raw)]) + raw
    return header + qname + b"\x00" + struct.pack(">HH", 1, 1)


def _parse_dns_a(data, txid):
    """A records from a DNS response (compression-aware, CNAMEs skipped —
    their terminal A records appear in the same answer section)."""
    if len(data) < 12:
        return []
    rid, flags, qdcount, ancount, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != txid or not flags & 0x8000:
        return []

    def skip_name(off):
        while off < len(data):
            length = data[off]
            if length == 0:
                return off + 1
            if length & 0xC0 == 0xC0:
                return off + 2
            off += 1 + length
        return off

    off = 12
    for _ in range(qdcount):
        off = skip_name(off) + 4
    addrs = []
    for _ in range(ancount):
        off = skip_name(off)
        if off + 10 > len(data):
            break
        rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if rtype == 1 and rclass == 1 and rdlen == 4 and off + 4 <= len(data):
            addrs.append(".".join(str(b) for b in data[off:off + 4]))
        off += rdlen
    return addrs


def _dns_query_a(host, server, l3dev, timeout=2):
    """Resolve `host` by asking `server` directly over UDP bound to the WiFi
    netdev — sidesteps the local dnsmasq entirely (whose all_servers race and
    rebind filter are the very failure being fixed) and guarantees the query
    travels the hotspot's uplink. Returns a list of IPv4 strings, [] on any
    failure."""
    txid = int.from_bytes(os.urandom(2), "big")
    try:
        query = _build_dns_query(host, txid)
    except (ValueError, UnicodeError):
        return []
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if l3dev:
            sock.setsockopt(socket.SOL_SOCKET,
                            getattr(socket, "SO_BINDTODEVICE", 25),
                            (l3dev + "\0").encode())
        sock.settimeout(timeout)
        sock.sendto(query, (server, 53))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data, addr = sock.recvfrom(4096)
            if addr[0] == server:
                return _parse_dns_a(data, txid)
        return []
    except Exception:
        return []
    finally:
        if sock is not None:
            sock.close()


def _next_hop(url, host, addr, l3dev):
    """Location target of the next redirect in the portal's login chain, ''
    when it answers in place. --resolve pins the name to the address the
    hotspot's own DNS gave so the request can't depend on the system resolver;
    -k because only the Location header is read here, nothing is submitted."""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return ""
    cmd = ["curl", "-s", "-o", "/dev/null", "-k", "--max-time", "5",
           "--interface", l3dev, "-w", "%{redirect_url}"]
    if addr and not _is_ip_literal(host):
        cmd += ["--resolve", "{}:{}:{}".format(host, port, addr)]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=8).stdout.strip()
    except Exception:
        return ""
    return out if _url_host(out) else ""


def _portal_conf_text(hosts, dns_servers):
    """dnsmasq drop-in scoping each portal hostname to the hotspot's own
    resolvers (a domain-scoped server= overrides all_servers' parallel race)
    and exempting it from rebind protection (portal names resolve to private
    addresses by design)."""
    lines = []
    for host in hosts:
        for dns in dns_servers:
            lines.append("server=/{}/{}".format(host, dns))
        lines.append("rebind-domain-ok=/{}/".format(host))
    return "\n".join(lines) + "\n"


def _write_portal_dns_pin(hosts, dns_servers):
    """Install (or refresh) the dnsmasq drop-in. Restarts dnsmasq only when
    the content actually changed, so the per-tick re-assert is free. Hostnames
    and resolver addresses are validated before they may become config lines —
    a hostile AP controls the Location header this data came from."""
    hosts = [h for h in hosts if _HOSTNAME_RE.match(h)]
    dns_servers = [d for d in dns_servers if _pinnable_ip(d)]
    if not hosts or not dns_servers:
        return False
    path = os.path.join(_dnsmasq_confdir(), _PORTAL_PIN_CONF)
    text = _portal_conf_text(hosts, dns_servers)
    try:
        with open(path) as f:
            if f.read() == text:
                _portal_pin["conf_path"] = path
                return False
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    except OSError as e:
        pcat_logger.logger.warning(
            "wifi_wan: portal DNS pin write failed: {}".format(e))
        return False
    _portal_pin["conf_path"] = path
    _restart_dnsmasq()
    pcat_logger.logger.info(
        "wifi_wan: portal DNS pinned: {} -> {}".format(
            ", ".join(hosts), ", ".join(dns_servers)))
    return True


def _ensure_portal_access(l3dev):
    """Make the captured portal login URL reachable for LAN clients while
    egress rides the next uplink. Walks the login redirect chain (bounded),
    resolving each hostname against the hotspot's DHCP resolvers, then pins
    DNS (dnsmasq drop-in) and routing (/32s via the WiFi gateway) for every
    hop. Idempotent per tick; the full walk runs only when the portal URL
    changes. Never raises — a failed pin just leaves today's (broken) status
    quo, which reconcile retries next tick."""
    url = _portal_seen["url"]
    if not url or not l3dev:
        return
    try:
        gateway, dns, subnet = _wan_wifi_ipv4()
        if _portal_pin["url"] == url:
            for ip in list(_portal_pin["routes"]):
                _pin_route(ip, gateway, l3dev, subnet)
            return
        # The hotspot's resolver itself must be reachable through the WiFi:
        # dnsmasq forwards router-side (unbound socket -> default route), so
        # an off-subnet resolver would otherwise be queried via cellular.
        for d in dns:
            _pin_route(d, gateway, l3dev, subnet)
        hosts = {}  # host -> [addrs], insertion-ordered along the chain
        cur = url
        for _ in range(_PORTAL_MAX_HOPS):
            host = _url_host(cur)
            if not host or host in hosts or host == _CONN_CHECK_HOST:
                break
            if _is_ip_literal(host):
                if not _pinnable_ip(host):
                    break
                hosts[host] = [host]
            else:
                if not dns or not _HOSTNAME_RE.match(host):
                    break
                addrs = []
                for d in dns:
                    addrs = _dns_query_a(host, d, l3dev)
                    if addrs:
                        break
                hosts[host] = addrs[:4]
                if not addrs:
                    break  # unresolvable: nothing to pin, cannot follow either
            nxt = _next_hop(cur, host, hosts[host][0] if hosts[host] else "",
                            l3dev)
            if not nxt:
                break
            cur = nxt
        named = [h for h in hosts if not _is_ip_literal(h)]
        if named:
            _write_portal_dns_pin(named, dns)
        for addrs in hosts.values():
            for a in addrs:
                _pin_route(a, gateway, l3dev, subnet)
        if any(addrs for addrs in hosts.values()):
            # Only a walk that resolved something is worth caching; otherwise
            # retry the whole thing next tick (hotspot DNS may still be
            # settling right after the join).
            _portal_pin["url"] = url
            pcat_logger.logger.info(
                "wifi_wan: portal access pinned via {}: {}".format(
                    l3dev,
                    "; ".join("{} [{}]".format(h, ", ".join(a))
                              for h, a in hosts.items())))
    except Exception as e:
        pcat_logger.logger.warning(
            "wifi_wan: portal access pin failed: {}".format(e))


def _clear_portal_access(reason=""):
    """Tear the pin down: remove the dnsmasq drop-in (restarting dnsmasq only
    if one was actually present — also catches a file left by a previous app
    run) and delete our /32s. Cheap no-op when nothing is pinned, so callers
    sprinkle it freely on every exit from the portal state."""
    cleared = False
    path = _portal_pin["conf_path"] or os.path.join(_dnsmasq_confdir(),
                                                    _PORTAL_PIN_CONF)
    try:
        if os.path.exists(path):
            os.remove(path)
            cleared = True
            _restart_dnsmasq()
    except OSError:
        pass
    for ip in list(_portal_pin["routes"]):
        try:
            subprocess.run(["ip", "route", "del", "{}/32".format(ip)],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
        cleared = True
    _portal_pin["routes"].clear()
    _portal_pin.update({"url": "", "conf_path": ""})
    if cleared:
        pcat_logger.logger.info("wifi_wan: portal access pin cleared{}".format(
            " ({})".format(reason) if reason else ""))


# ── route-metric demotion (failover away from a portal-locked uplink) ──────────

def _set_wifi_metric(metric):
    """Set the wan_wifi route metric for BOTH families and re-apply. Lower =
    preferred.

    The upstream is two UCI interfaces -- wan_wifi (IPv4) and wan_wifi_v6 --
    and the kernel selects a default route per family independently. Demoting
    only wan_wifi (as this used to) failed v4 over to the next uplink but left
    the v6 default route on wan_wifi_v6, still beating cellular's v6, so all
    IPv6 traffic kept flowing to a dead or portal-locked upstream. Move both.

    `metric` is the v4 value; the wifi slot's v6 metric always sits exactly one
    above its v4 (wan_manager.slot_metrics builds it as v4+1 for this
    single-member slot, and the legacy 5/6 pair matches), so the demoted pair
    is 1020/1021 and the preferred pair 20/21. Independent change checks mean a
    device left half-demoted by the old code (v4=1020, v6 stuck at 21) is
    corrected on the next call even if its v4 already matches."""
    metric_v4 = int(metric)
    metric_v6 = metric_v4 + 1
    changed = False
    if uci_get("network.{}.metric".format(NET_IFACE)) != str(metric_v4):
        uci_set("network.{}.metric={}".format(NET_IFACE, metric_v4))
        changed = True
    if uci_get("network.{}.metric".format(NET_IFACE_V6)) != str(metric_v6):
        uci_set("network.{}.metric={}".format(NET_IFACE_V6, metric_v6))
        changed = True
    if not changed:
        return False
    uci_commit("network")
    _ifup(NET_IFACE)
    _ifup(NET_IFACE_V6)
    pcat_logger.logger.info(
        "wifi_wan: wan_wifi metric -> {} (v6 {})".format(metric_v4, metric_v6))
    return True


def promote_if_connected():
    """Restore the preferred (non-demoted) wan_wifi metric when the STA is up
    with an address. Called by wan_manager when a higher-priority uplink dies
    so a still-connected WiFi is not left demoted under cellular.

    Returns True when the metric was restored (or already preferred with a
    live address). Does not run a connectivity probe — the next wifi_wan
    reconcile will re-demote if the uplink has no real internet."""
    if not is_sta_enabled():
        return False
    ifstatus = ubus_call("network.interface.{}".format(NET_IFACE), "status") or {}
    if not ifstatus.get("up"):
        return False
    has_addr = bool(ifstatus.get("ipv4-address")) or bool(
        (ubus_call("network.interface.{}".format(NET_IFACE_V6), "status") or {})
        .get("ipv6-address"))
    if not has_addr:
        return False
    metric_v4, _v6, _demoted = _metrics()
    current = uci_get("network.{}.metric".format(NET_IFACE))
    if current == str(metric_v4):
        return True  # already preferred and connected
    _set_wifi_metric(metric_v4)
    # Clear flap state so a single post-handoff glitch doesn't demote again
    # before the link has a chance to carry traffic.
    _conn_cache["bad_streak"] = 0
    _conn_cache["result"] = "online"
    _conn_cache["time"] = time.time()
    return True


def nudge_rejoin():
    """Clear scan backoff so the next reconcile() can rejoin a saved network
    immediately (used when eth dies and the STA is disassociated)."""
    _scan_backoff_reset()
    # Allow an immediate roam/rejoin scan rather than waiting out the interval.
    _roam_scan["next_ok"] = 0.0


# ── high-level operations ─────────────────────────────────────────────────────

def apply_config(ssid, key, encryption, band="", channel=None,
                 random_mac=False, custom_mac="", eap=None):
    """
    Configure and start the upstream STA: writes wireless/network/firewall
    UCI, parks the onboard AP, reloads wifi and brings wan_wifi up.

    random_mac: use a stable-per-SSID locally-administered MAC for the STA.
    custom_mac: a user-supplied MAC (already validated); wins over random_mac.
    eap: when encryption == 'eap', a dict with EAP params:
         {type, identity, password, anonymous_identity, ca_cert}.
    """
    radio = get_sta_radio()
    pcat_logger.logger.info(
        "wifi_wan: joining '{}' ({} {}) on {}".format(ssid, encryption, band or "?", radio))

    _scan_backoff_reset()  # explicit (re)configuration: resume prompt scanning
    _vif_rescue_reset()    # this join reloads wifi itself; fresh grace window
    # A connectivity verdict measured on the previous association is stale for
    # this one: reset to 'unknown' so the fresh link gets the damped
    # unknown->offline grace (one probe round) instead of being demoted by a
    # first probe that races its own DHCP. The portal pin (and the captured
    # login URL) belong to the previous hotspot for the same reason.
    _conn_cache.update({"result": "unknown", "bad_streak": 0, "time": 0.0})
    _portal_seen["url"] = ""
    _clear_portal_access("new join")
    # Hold off the next roam scan by one full interval: a just-joined link
    # should settle before we consider jumping off it again (prevents an
    # immediate re-scan/switch loop between two comparable saved networks).
    _roam_scan["next_ok"] = time.time() + _ROAM_SCAN_INTERVAL
    _ensure_network_ifaces()
    _ensure_firewall()
    _disable_mwan3()
    _backup_radio_state(radio)

    _tune_radio_for_band(radio, band, channel)
    uci_set("wireless.{}.disabled=0".format(radio))

    if uci_get("wireless.{}".format(STA_SECTION)) != "wifi-iface":
        uci_set("wireless.{}=wifi-iface".format(STA_SECTION))
    uci_set("wireless.{}.device={}".format(STA_SECTION, radio))
    uci_set("wireless.{}.mode=sta".format(STA_SECTION))
    uci_set("wireless.{}.network={}".format(STA_SECTION, NET_IFACE))
    uci_set("wireless.{}.ssid={}".format(STA_SECTION, ssid))
    _write_encryption(ssid, key, encryption, eap)

    # Custom (user-supplied, e.g. an address the upstream admits) wins over
    # the private stable-per-SSID MAC, which wins over the pinned per-radio
    # default. Never leave macaddr unset: mac80211's derived addressing makes
    # an unset MAC drift with vif order (see pinned_sta_mac), churning DHCP
    # leases.
    custom = valid_custom_mac(custom_mac)
    if custom:
        uci_set("wireless.{}.macaddr={}".format(STA_SECTION, custom))
    elif random_mac:
        uci_set("wireless.{}.macaddr={}".format(STA_SECTION, stable_mac_for_ssid(ssid)))
    else:
        pinned = pinned_sta_mac(radio)
        if pinned:
            uci_set("wireless.{}.macaddr={}".format(STA_SECTION, pinned))
        else:
            uci_delete("wireless.{}.macaddr".format(STA_SECTION))

    uci_set("wireless.{}.disabled=0".format(STA_SECTION))
    # STA-only while relaying: park this radio's own AP (restored on exit)
    uci_set("wireless.default_{}.disabled=1".format(radio))

    _commit_wireless_verified()
    # Remember which upstream we joined last (survives reboots): the
    # config-rebuild path in reconcile() uses it to restore the right
    # network when the STA section is missing at boot.
    import pcat_config
    pcat_config.set_user_config_key("wifi_wan_last_ssid", ssid)
    _reload_wifi()
    _ifup(NET_IFACE)
    # The driver does not honor the configured MAC at vif creation; give the
    # netdev a few seconds to appear, then set it over rtnetlink.
    _enforce_sta_netdev_mac(radio, attempts=6)
    return True


def _write_encryption(ssid, key, encryption, eap):
    """Write the encryption/auth options for the STA section. Clears stale
    EAP/PSK options so switching a saved network between PSK and EAP is clean."""
    # Clear options that may linger from a previous encryption type.
    for opt in ("key", "eap_type", "auth", "identity", "password",
                "anonymous_identity", "ca_cert"):
        uci_delete("wireless.{}.{}".format(STA_SECTION, opt))

    if encryption == "none":
        uci_set("wireless.{}.encryption=none".format(STA_SECTION))
        return
    if encryption == "eap":
        eap = eap or {}
        eap_type = eap.get("type", "peap")
        if eap_type not in EAP_TYPES:
            eap_type = "peap"
        uci_set("wireless.{}.encryption=wpa2".format(STA_SECTION))
        uci_set("wireless.{}.eap_type={}".format(STA_SECTION, eap_type))
        if eap_type in ("peap", "ttls"):
            uci_set("wireless.{}.auth=EAP-MSCHAPV2".format(STA_SECTION))
            uci_set("wireless.{}.identity={}".format(STA_SECTION, eap.get("identity", "")))
            uci_set("wireless.{}.password={}".format(STA_SECTION, key))
            anon = eap.get("anonymous_identity", "")
            if anon:
                uci_set("wireless.{}.anonymous_identity={}".format(STA_SECTION, anon))
        ca_cert = eap.get("ca_cert", "")
        if ca_cert:
            uci_set("wireless.{}.ca_cert={}".format(STA_SECTION, ca_cert))
        return
    # PSK variants
    uci_set("wireless.{}.encryption={}".format(STA_SECTION, encryption))
    uci_set("wireless.{}.key={}".format(STA_SECTION, key))


def set_sta_enabled(enabled):
    """
    Toggle the relay STA on network-mode changes (smart_wan <-> others).
    No-op when no upstream has been configured yet. Returns True if the
    runtime state changed.
    """
    if not is_configured():
        return False
    radio = uci_get("wireless.{}.device".format(STA_SECTION)) or get_sta_radio()
    was_enabled = uci_get("wireless.{}.disabled".format(STA_SECTION)) != "1"
    if enabled:
        changed = not was_enabled
        _scan_backoff_reset()  # STA (re)enabled: resume prompt scanning
        _vif_rescue_reset()
        _ensure_network_ifaces()
        _ensure_firewall()
        _disable_mwan3()
        _backup_radio_state(radio)
        # Retune the radio to the remembered upstream's band (the exit path
        # restores the AP band, so a re-entered STA may be on the wrong band)
        import pcat_config
        ssid = uci_get("wireless.{}.ssid".format(STA_SECTION))
        saved = next((n for n in pcat_config.get_wifi_wan_networks()
                      if n.get("ssid") == ssid), None)
        if saved:
            _tune_radio_for_band(radio, saved.get("band", ""), saved.get("channel"))
        uci_set("wireless.{}.disabled=0".format(STA_SECTION))
        uci_set("wireless.{}.disabled=0".format(radio))
        uci_set("wireless.default_{}.disabled=1".format(radio))
    else:
        changed = was_enabled
        _portal_seen["url"] = ""
        _clear_portal_access("sta disabled")
        uci_set("wireless.{}.disabled=1".format(STA_SECTION))
        if _restore_radio_state():
            changed = True
    uci_commit("wireless")
    if changed:
        pcat_logger.logger.info("wifi_wan: STA {}".format("enabled" if enabled else "disabled"))
        _reload_wifi()
        if enabled:
            _ifup(NET_IFACE)
        else:
            _ifdown(NET_IFACE)
    return changed


def remove_sta():
    """Forget the current upstream: drop the STA section and restore the AP."""
    changed = False
    _portal_seen["url"] = ""
    _clear_portal_access("forget")
    if uci_get("wireless.{}".format(STA_SECTION)):
        _ifdown(NET_IFACE)
        uci_delete("wireless.{}".format(STA_SECTION))
        changed = True
    if _restore_radio_state():
        changed = True
    if changed:
        uci_commit("wireless")
        _reload_wifi()
        pcat_logger.logger.info("wifi_wan: removed STA config")
    return changed


def _iw_link_signal(dev):
    """Real STA signal in dBm from `iw dev <dev> link`. Some onboard drivers
    (e.g. AIC8800) report 0 dBm through iwinfo while iw shows the true value."""
    try:
        out = subprocess.run(["iw", "dev", dev, "link"],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("signal:"):
            # e.g. "signal: -45 dBm"
            try:
                return int(line.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                return None
    return None


def signal_dbm_to_percent(dbm):
    """Map a Wi-Fi RSSI (dBm) to a 0-100% bar level. -50 dBm or better -> 100%,
    -100 dBm or worse -> 0% (the convention iwinfo/NetworkManager use)."""
    if dbm is None:
        return None
    if dbm >= -50:
        return 100
    if dbm <= -100:
        return 0
    return int(2 * (dbm + 100))


def get_state():
    """Config + live connection state of the upstream STA link."""
    state = {
        "configured": is_configured(),
        "sta_enabled": False,
        "ssid": "",
        "encryption": "none",
        "random_mac": False,
        "custom_mac": "",
        "band": "",
        "associated": False,
        "connected": False,
        "internet": None,
        "captive_portal": False,
        "portal_url": "",
        "ip": "",
        "ip6": "",
        "signal": None,
        "signal_percent": None,
        "bitrate": None,
        "uptime": 0,
    }
    if not state["configured"]:
        return state
    cfg = _wireless_cfg()
    sta = "wireless.{}".format(STA_SECTION)
    radio = cfg.get(sta + ".device", "") or get_sta_radio()
    state["ssid"] = cfg.get(sta + ".ssid", "")
    # EAP is stored as encryption=wpa2 + eap_type; report it back as "eap".
    enc = cfg.get(sta + ".encryption", "") or "none"
    state["encryption"] = "eap" if cfg.get(sta + ".eap_type", "") else enc
    # A set macaddr no longer implies "private MAC" — the non-random default
    # is now a pinned per-radio address (see pinned_sta_mac). Random mode is
    # recognizable as exactly the per-SSID derived value; anything that is
    # neither the derived nor the pinned address is a custom MAC and gets
    # reported as such (never guessed at).
    macaddr = cfg.get(sta + ".macaddr", "")
    state["random_mac"] = (
        macaddr == stable_mac_for_ssid(state["ssid"])
        if state["ssid"] else False)
    if macaddr and not state["random_mac"] and macaddr != pinned_sta_mac(radio):
        state["custom_mac"] = macaddr
    state["band"] = cfg.get("wireless.{}.band".format(radio), "")
    state["sta_enabled"] = cfg.get(sta + ".disabled", "") != "1"
    if not state["sta_enabled"]:
        return state

    ifstatus = ubus_call("network.interface.{}".format(NET_IFACE), "status") or {}
    up = bool(ifstatus.get("up"))
    state["uptime"] = ifstatus.get("uptime", 0)
    for addr in ifstatus.get("ipv4-address") or []:
        state["ip"] = addr.get("address", "")
        break
    ifstatus_v6 = ubus_call("network.interface.{}".format(NET_IFACE_V6), "status") or {}
    for addr in ifstatus_v6.get("ipv6-address") or []:
        state["ip6"] = addr.get("address", "")
        break

    l3dev = ifstatus.get("l3_device") or ifstatus.get("device") or _sta_ifname(radio)
    if l3dev:
        info = ubus_call("iwinfo", "info", {"device": l3dev}) or {}
        if info.get("ssid"):
            state["associated"] = True
            # some drivers report 0 dBm for the STA link via iwinfo; fall back
            # to `iw dev link` which reports the real value on those drivers.
            signal = info.get("signal") or _iw_link_signal(l3dev)
            state["signal"] = signal if signal else None
            state["signal_percent"] = signal_dbm_to_percent(state["signal"])
            state["bitrate"] = info.get("bitrate")
    # Association is a precondition, not an extra: netifd keeps wan_wifi "up"
    # with its address on an unexpired DHCP lease for a while after the AP goes
    # out of range, so up+IP alone reports a link that no longer exists. Walking
    # out of range then read as "connected <last ssid> -54 dBm" (the last values
    # sampled while still in range) instead of standby.
    state["connected"] = (state["associated"]
                          and up and (state["ip"] != "" or state["ip6"] != ""))
    # Connectivity is refreshed by reconcile()'s timer; surface the cached
    # verdict so the dashboard can flag a captive portal without probing here.
    if state["associated"]:
        conn = connectivity_state()
        if conn != "unknown":
            state["internet"] = conn == "online"
            state["captive_portal"] = conn == "portal"
            if state["captive_portal"]:
                # The probe's captured login URL: with the access pin holding
                # DNS + routing open through the WiFi, this link actually
                # works from a LAN client, so the UI offers it as a shortcut.
                state["portal_url"] = _portal_seen["url"]
    return state


# ── iPhone-like keeper ────────────────────────────────────────────────────────
# Periodic reconcile so the upstream behaves like a phone joining known WiFi:
# whenever Smart WAN is selected and a saved network is in range, keep the STA
# associated to it.  While associated to any saved network we never switch
# (stay-if-connected); we only (re)join when disassociated.  When no saved
# network is reachable we leave the radio idle and the metric/route layer +
# pcat-manager correctly fall back to cellular.

def _strongest_by_ssid(networks):
    """{ssid: strongest scan entry}. scan() returns one entry per (ssid, band),
    so a dual-band SSID appears twice; keep the stronger one, otherwise the
    join targets whatever band happened to come last — often the weak one that
    barely reaches the current room."""
    in_range = {}
    for n in networks:  # scan() sorts strongest-first
        in_range.setdefault(n["ssid"], n)
    return in_range


def _is_associated(radio):
    """True when the STA radio is currently joined to an AP."""
    l3dev = _sta_ifname(radio)
    if not l3dev:
        return False
    info = ubus_call("iwinfo", "info", {"device": l3dev}) or {}
    return bool(info.get("ssid"))


def _sta_vif_present(radio):
    """True when the STA's netdev actually exists. netifd's wireless status
    may either drop the interface entirely or keep reporting a stale ifname
    whose netdev is gone; both count as absent (a stale name would also send
    every scan below to a dead netdev, coming back empty forever)."""
    ifname = _sta_ifname(radio)
    return bool(ifname) and os.path.exists("/sys/class/net/{}".format(ifname))


def _vif_rescue_reset():
    _vif_rescue["missing_since"] = 0.0
    _vif_rescue["fails"] = 0
    _vif_rescue["next_ok"] = 0.0


def _rescue_missing_vif(radio):
    """One keeper tick in the 'STA enabled but no netdev' state: wait out the
    grace window, then re-assert the wireless config the way the manual
    reconnect does (see the vif-watchdog note above). Returns the reconcile
    status string."""
    now = time.time()
    if not _vif_rescue["missing_since"]:
        _vif_rescue["missing_since"] = now
    if now - _vif_rescue["missing_since"] < _VIF_MISSING_GRACE:
        return "sta_vif_missing"
    if now < _vif_rescue["next_ok"]:
        return "sta_vif_rescue_wait"
    delay = min(_VIF_RESCUE_BACKOFF_CAP,
                _VIF_RESCUE_BACKOFF_START * (2 ** min(_vif_rescue["fails"], 4)))
    _vif_rescue["fails"] += 1
    _vif_rescue["next_ok"] = now + delay
    pcat_logger.logger.warning(
        "wifi_wan: STA enabled but {} has no netdev; reloading wifi to "
        "recreate it (attempt {}, retry in {}s)".format(
            radio, _vif_rescue["fails"], int(delay)))
    _reload_wifi()
    _ifup(NET_IFACE)
    return "sta_vif_rescue_reload"


def _current_link_signal(radio):
    """Signal (dBm) of the STA's current association, or None. Uses iwinfo and
    falls back to `iw dev link` for drivers that report 0 dBm via iwinfo."""
    l3dev = _sta_ifname(radio)
    if not l3dev:
        return None
    info = ubus_call("iwinfo", "info", {"device": l3dev}) or {}
    signal = info.get("signal") or _iw_link_signal(l3dev)
    return signal if signal else None


def _roam_check(radio, saved, cur_ssid, current_online):
    """While associated, look for a *different* saved network that has come into
    range and, if it is a better bet, switch to it. Returns the SSID switched
    to, or None. Rate-limited by _ROAM_SCAN_INTERVAL so it doesn't scan every
    reconcile tick (a scan is power-hungry).

    Deliberately conservative when the current link works: we only jump between
    distinct saved SSIDs (a stronger BSS of the *same* SSID is the driver's job)
    and only when the rival beats the current link by _ROAM_SWITCH_MARGIN_DBM,
    so two comparable APs don't cause a switch storm. When the current link has
    *no* internet (portal/offline), we drop the margin and switch to any
    in-range saved network — a working alternative beats a dead one."""
    now = time.time()
    if now < _roam_scan["next_ok"]:
        return None
    _roam_scan["next_ok"] = now + _ROAM_SCAN_INTERVAL

    networks, err = scan(radio)
    if err and not networks:
        return None
    in_range = _strongest_by_ssid(networks)
    # Other saved networks (not the one we're on) that are actually in range.
    rivals = [n for n in saved
              if n.get("ssid") != cur_ssid and n.get("ssid") in in_range]
    if not rivals:
        return None

    cur_signal = _current_link_signal(radio)
    if cur_signal is None:
        cur_signal = -100  # unknown/very weak current link => allow the switch
    best = max(rivals, key=lambda n: in_range[n["ssid"]]["signal"])
    best_signal = in_range[best["ssid"]]["signal"]
    # Hysteresis only protects a link that actually reaches the internet; a
    # portal-locked or dead link should yield to any reachable saved network.
    margin = _ROAM_SWITCH_MARGIN_DBM if current_online else 0
    if best_signal - cur_signal < margin:
        return None  # not clearly better; stay put

    seen = in_range[best["ssid"]]
    pcat_logger.logger.info(
        "wifi_wan: roaming '{}' ({} dBm) -> '{}' ({} dBm)".format(
            cur_ssid, cur_signal, best["ssid"], best_signal))
    _set_wifi_metric(_metrics()[0])  # fresh join starts at the preferred metric
    apply_config(
        best["ssid"], best.get("key", ""), best.get("encryption", "none"),
        seen.get("band") or best.get("band", ""),
        seen.get("channel") or best.get("channel"),
        random_mac=bool(best.get("random_mac")),
        custom_mac=best.get("custom_mac", ""),
        eap=eap_from_saved(best))
    return best["ssid"]


def _assert_sta_mac_pinned():
    """Upgrade path for devices that joined before MAC pinning existed: an
    STA section with no macaddr drifts with mac80211's vif-order-derived
    addressing and churns DHCP leases (see pinned_sta_mac). Write the pin
    into UCI once; it takes effect at the next wifi reload/rejoin rather
    than bouncing a working link here."""
    cfg = _wireless_cfg()
    sta = "wireless.{}".format(STA_SECTION)
    if cfg.get(sta, "") != "wifi-iface" or cfg.get(sta + ".macaddr", ""):
        return
    pinned = pinned_sta_mac(cfg.get(sta + ".device", "") or get_sta_radio())
    if not pinned:
        return
    uci_set(sta + ".macaddr=" + pinned)
    uci_commit("wireless")
    pcat_logger.logger.info(
        "wifi_wan: pinned STA MAC to {} (was unset/drifting)".format(pinned))


def _enforce_sta_netdev_mac(radio=None, attempts=1, delay=1.0):
    """Make the live STA netdev carry the MAC configured in UCI.

    The aic8800 fullmac driver ignores the MAC handed to it at vif creation
    (nl80211 interface-add), so wireless.macaddr — pinned, private-random or
    custom — never reached the air: the STA always associated with the
    hardware address. rtnetlink SETLINK is honored (verified on hardware), so
    set it there: link down, address, up. wpa_supplicant re-associates with
    the new address on its own. Returns True when the netdev MAC matches.
    """
    cfg = _wireless_cfg()
    sta = "wireless.{}".format(STA_SECTION)
    want = (cfg.get(sta + ".macaddr", "") or "").lower()
    if not want or cfg.get(sta + ".disabled", "") == "1":
        return True
    radio = radio or cfg.get(sta + ".device", "") or get_sta_radio()
    for attempt in range(attempts):
        if attempt:
            time.sleep(delay)
        ifname = _sta_ifname(radio)
        if not ifname:
            continue
        try:
            with open("/sys/class/net/{}/address".format(ifname)) as f:
                have = f.read().strip().lower()
        except OSError:
            continue
        if have == want:
            return True
        pcat_logger.logger.info(
            "wifi_wan: STA netdev {} carries {} instead of the configured "
            "{}; enforcing via rtnetlink".format(ifname, have, want))
        subprocess.run(["ip", "link", "set", ifname, "down"],
                       capture_output=True, timeout=10)
        subprocess.run(["ip", "link", "set", ifname, "address", want],
                       capture_output=True, timeout=10)
        subprocess.run(["ip", "link", "set", ifname, "up"],
                       capture_output=True, timeout=10)
    try:
        ifname = _sta_ifname(radio)
        if ifname:
            with open("/sys/class/net/{}/address".format(ifname)) as f:
                return f.read().strip().lower() == want
    except OSError:
        pass
    return False


def _log_radio_deficit():
    """One line naming the missing radio class and the live phys, so a
    field log distinguishes 'PCIe card gone' from 'onboard card gone' --
    the UI's slot row only says the upstream is not detected."""
    try:
        from pcat_util import wan_manager
        classes = wan_manager._radio_classes()
        missing = [c for c, alive in classes.items() if not alive] or ["?"]
        try:
            phys = sorted(p for p in os.listdir("/sys/class/ieee80211")
                          if p.startswith("phy"))
        except OSError:
            phys = []
        pcat_logger.logger.warning(
            "wifi_wan: radio deficit: missing={} live phys={}".format(
                ",".join(missing), phys))
    except Exception as e:
        pcat_logger.logger.debug("wifi_wan: radio deficit log failed: {}".format(e))


def reconcile():
    """
    Keep the upstream WiFi connected like a phone does. Safe to call on a
    timer; returns a short status string for logging. No-op unless the priority
    table's wifi slot is enabled (priority mode is the upstream-WiFi "Smart WAN"
    replacement; the retired smart_wan mode is migrated to it on read).
    """
    try:
        import pcat_config
        if pcat_config.get_network_mode() != "priority":
            return "not_priority"
        from pcat_util import wan_manager
        if not wan_manager.is_slot_enabled("wifi"):
            return "wifi_slot_disabled"
    except Exception:
        return "config_error"

    _assert_sta_mac_pinned()
    # Re-enforce after any wifi reload from another path (boot, the AP
    # settings keeper, a settings save) recreated the vif with the hw MAC.
    _enforce_sta_netdev_mac()

    # Two live radios are the floor for the relay (one STA upstream, one AP
    # for clients). A deficit is treated as transient — hold off joining, and
    # only park an already-enabled STA (freeing its radio to serve clients
    # again) once the deficit has persisted across several ticks, i.e. a
    # radio really left rather than a slow boot. The priority-table slot
    # stays enabled either way, so the relay re-engages on its own as soon
    # as a second radio is back.
    if not wan_manager.relay_radios_ready():
        _radio_deficit["ticks"] += 1
        if _radio_deficit["ticks"] == 1:
            _log_radio_deficit()
        if is_sta_enabled() and _radio_deficit["ticks"] >= _RADIO_DEFICIT_PARK_TICKS:
            set_sta_enabled(False)
            _radio_deficit["parked"] = True
            return "sta_parked_need_two_radios"
        return "need_two_radios"
    if _radio_deficit["ticks"]:
        pcat_logger.logger.info(
            "wifi_wan: second radio is back after {} tick(s) without it".format(
                _radio_deficit["ticks"]))
    _radio_deficit["ticks"] = 0
    if _radio_deficit["parked"]:
        # The deficit is over and it was this keeper that parked the STA, so
        # the user's slot choice still stands: re-enable it outright. Going
        # through the scan path instead costs minutes -- on the bench a PCI
        # remove/rescan of the ath12k card (what the firmware-wedge watchdog
        # does; the phy comes back renumbered) left the STA parked, the next
        # scan from an AP-mode vif came back empty, and its backoff then held
        # the rejoin for another 90s on top. wpa_supplicant hunts for the
        # saved SSID by itself once the section is enabled.
        _radio_deficit["parked"] = False
        if is_configured() and not is_sta_enabled() and set_sta_enabled(True):
            return "sta_unparked_radios_back"

    saved = pcat_config.get_wifi_wan_networks()
    if not saved:
        return "no_saved_networks"

    radio = get_sta_radio()

    # The STA section can be missing outright even though the enabled wifi
    # slot and the saved networks say the relay should be running: a join
    # whose `uci commit wireless` died quietly leaves the section only in
    # /tmp/.uci staging, which a reboot wipes (see _commit_wireless_verified).
    # This state does NOT heal by scanning — the config-less radio has no
    # netdev to scan from (its AP is parked too) and the fallback AP-mode
    # scan on the other radio is unreliable — so rebuild the config directly
    # from the saved record: apply_config re-creates the section and reloads,
    # and wpa_supplicant hunts for the AP on its own from there. Restores the
    # last-joined network; with a single saved network there is no ambiguity.
    if not is_configured():
        last = pcat_config.get_user_config_key("wifi_wan_last_ssid", "")
        net = (next((n for n in saved if n.get("ssid") == last), None)
               or (saved[0] if len(saved) == 1 else None))
        if net:
            pcat_logger.logger.warning(
                "wifi_wan: STA config missing; rebuilding '{}' from saved "
                "networks".format(net["ssid"]))
            apply_config(
                net["ssid"], net.get("key", ""), net.get("encryption", "none"),
                net.get("band", ""), net.get("channel"),
                random_mac=bool(net.get("random_mac")),
                custom_mac=net.get("custom_mac", ""), eap=eap_from_saved(net))
            return "rebuilt:{}".format(net["ssid"])
        # Several saved networks and no last-joined record: fall through to
        # the scan path below, which picks the strongest one in range.

    # Already joined → stay-if-connected. Verify there is real internet behind
    # the association and handle captive portals; then, if a *different* saved
    # network has come into range and is a better bet, roam to it (the "arrived
    # at a new location" case). The roam scan is rate-limited inside
    # _roam_check so it doesn't fire every tick.
    if is_sta_enabled() and _is_associated(radio):
        _scan_backoff_reset()
        _vif_rescue_reset()
        cur = uci_get("wireless.{}.ssid".format(STA_SECTION))
        saved_cur = next((n for n in saved if n.get("ssid") == cur), None)
        l3dev = _sta_ifname(radio)
        metric_v4, _v6, metric_demoted = _metrics()
        # Skip the probe while the uplink is steadily online and the last verdict
        # is fresh (see _ONLINE_PROBE_INTERVAL); otherwise verify now.
        if connectivity_state() == "online" \
                and (time.time() - _conn_cache["time"]) < _ONLINE_PROBE_INTERVAL:
            conn = "online"
        else:
            conn = _refresh_connectivity(l3dev)

        # Captive portal: try the per-network auto-login hook before anything
        # else, so a portal that a script can clear doesn't trigger a roam.
        logged_in = False
        if conn == "portal" and saved_cur and saved_cur.get("login_cmd"):
            _run_login_cmd(saved_cur.get("login_cmd"), l3dev)
            conn = _refresh_connectivity(l3dev)
            logged_in = True

        # Roam to a clearly-better (or, if this link is dead, any) saved SSID.
        # 'unknown' (boot/join grace, see _refresh_connectivity) counts as
        # alive here: one unconfirmed failed round must not trigger the
        # roam-anywhere path any more than it may demote.
        roamed = _roam_check(radio, saved, cur,
                             current_online=(conn in ("online", "unknown")))
        if roamed:
            return "roamed:{}".format(roamed)

        if conn == "online":
            _clear_portal_access("online")
            _set_wifi_metric(metric_v4)  # restore preference if we'd demoted
            return "ok_online_after_login" if logged_in else "ok_online"
        if conn == "unknown":
            # Unconfirmed first failure (boot/fresh-join grace): keep the
            # metric where it is. Demoting here is what used to bounce egress
            # to cellular and back right after startup. A genuinely dead
            # uplink turns 'offline' on the next round and is demoted then.
            return "probe_unconfirmed"
        # Still blocked and no better saved network to roam to: demote so egress
        # fails over to the next uplink, but keep the link up so the captive
        # portal stays reachable for a login. "Reachable" needs more than the
        # link: the portal's DNS + routing must keep working through the WiFi
        # while the default route sits on cellular — that's the access pin.
        if conn == "portal":
            _ensure_portal_access(l3dev)
        else:
            _clear_portal_access("offline")
        # Manual login window: the user tapped "login required" on this slot,
        # so it is deliberately held as the primary uplink while they sign in
        # (the access pin alone doesn't help a portal that only answers to
        # traffic arriving on the default route). wan_manager closes the
        # window on success, on cancel, or when it times out, and this keeper
        # demotes on the next round.
        if wan_manager.portal_login_hold("wifi"):
            _set_wifi_metric(metric_v4)
            return "login_window_{}".format(conn)
        _set_wifi_metric(metric_demoted)
        return "demoted_{}".format(conn)

    # Not associated from here on: any portal pin belongs to a hotspot we've
    # left, and its scoped DNS entries would shadow real lookups elsewhere.
    _clear_portal_access("disassociated")

    # STA enabled in UCI but its netdev is gone: netifd will not re-create it
    # on its own and the scan path below can't help (nothing usable to scan
    # from). Re-assert the wireless config instead — see the vif-watchdog
    # note above. Deliberately ahead of the scan backoff: this path doesn't
    # scan, and a stack of failed scans from the vif-less state must not
    # delay the one action that actually fixes it.
    if is_sta_enabled() and not _sta_vif_present(radio):
        return _rescue_missing_vif(radio)
    _vif_rescue_reset()

    # Disassociated (or STA section missing/disabled). Scan and, if any saved
    # network is in range, (re)join the strongest one. Fruitless scans back
    # off (see _scan_backoff) so an away-from-home device doesn't burn the
    # radio on a full-band scan every 60s tick.
    if time.time() < _scan_backoff["next_ok"]:
        return "scan_backoff"
    networks, err = scan(radio)
    if err and not networks:
        _scan_backoff_hit()
        return "scan_failed:{}".format(err)
    in_range = _strongest_by_ssid(networks)
    candidates = [n for n in saved if n.get("ssid") in in_range]
    if not candidates:
        _scan_backoff_hit()
        return "no_saved_in_range"  # let it fall back to cellular

    # Strongest in-range saved network wins.
    _scan_backoff_reset()
    best = max(candidates, key=lambda n: in_range[n["ssid"]]["signal"])
    seen = in_range[best["ssid"]]
    _set_wifi_metric(_metrics()[0])  # fresh join starts at the preferred metric
    pcat_logger.logger.info(
        "wifi_wan: reconcile (re)joining '{}' (signal {} dBm)".format(
            best["ssid"], seen.get("signal")))
    apply_config(
        best["ssid"], best.get("key", ""), best.get("encryption", "none"),
        seen.get("band") or best.get("band", ""),
        seen.get("channel") or best.get("channel"),
        random_mac=bool(best.get("random_mac")),
        eap=eap_from_saved(best))
    return "rejoined:{}".format(best["ssid"])


def eap_from_saved(net):
    """Build the apply_config `eap` dict from a saved-network record, or None
    when the network is not EAP."""
    if not net or net.get("encryption") != "eap":
        return None
    return {
        "type": net.get("eap_type", "peap"),
        "identity": net.get("identity", ""),
        "anonymous_identity": net.get("anonymous_identity", ""),
        "ca_cert": net.get("ca_cert", ""),
    }
