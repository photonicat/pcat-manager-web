#!/usr/bin/env python3
"""Core-behaviour tests for pcat_util.wifi_wan — the WiFi-as-WAN relay.

WHY: this module is the state machine that decides whether a customer's
router has internet at all when riding an upstream hotspot: which radio
carries the STA, when to scan (battery), when to fail over to cellular
(captive portal / dead uplink), and how to come back.  Every branch here
maps to a field failure someone actually hit — stranded parked APs, DHCP
leases churning because the STA MAC drifted, a portal page eating all
egress, a commit that never reached flash so the upstream vanished on
reboot.  These tests pin those behaviours, not the implementation lines.

Runs anywhere: uci, ubus, subprocess, os, and time are all faked.  No
device, no radio, no network, no sleeps.

    python3 -m unittest tests.test_wifi_wan_core -v
"""

import io
import json
import os
import posixpath
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))

from pcat_util import wifi_wan
from pcat_util import wan_manager

STA = "wireless." + wifi_wan.STA_SECTION


class FakeUci(object):
    """In-memory stand-in for the device's UCI tree (flat dotted keys)."""

    def __init__(self, values):
        self.values = dict(values)

    def get(self, key):
        return self.values.get(key, "")

    def set(self, assignment):
        key, _, val = assignment.partition("=")
        self.values[key] = val
        return True

    def delete(self, key):
        for k in [k for k in self.values if k == key or k.startswith(key + ".")]:
            del self.values[k]
        return True

    def add_list(self, assignment):
        key, _, val = assignment.partition("=")
        cur = self.values.get(key, "")
        self.values[key] = (cur + " " + val).strip()
        return True


class FakeConfig(object):
    """Stand-in for pcat_config: user-config keys, saved networks, mode."""

    def __init__(self):
        self.keys = {}
        self.wifi_wan_radio = "onboard"
        self.network_mode = "priority"
        self.networks = []

    def get_user_config_key(self, key, fallback=None):
        return self.keys.get(key, fallback)

    def set_user_config_key(self, key, value):
        self.keys[key] = value

    def get_wifi_wan_radio(self):
        return self.wifi_wan_radio

    def set_wifi_wan_radio(self, cls):
        self.wifi_wan_radio = cls

    def get_network_mode(self):
        return self.network_mode

    def get_wifi_wan_networks(self):
        return self.networks


class FakeOs(object):
    """Just enough of `os` for wifi_wan: sysfs listings/links, exists(), and
    the file/dir/urandom bits the portal-access pin touches."""

    def __init__(self):
        self.listing = {}
        self.links = {}
        self.existing = set()
        self.environ = {}
        self.made_dirs = []
        outer = self

        class _Path(object):
            @staticmethod
            def exists(p):
                return p in outer.existing

            @staticmethod
            def join(*parts):
                return posixpath.join(*parts)

            @staticmethod
            def dirname(p):
                return posixpath.dirname(p)

        self.path = _Path()

    def listdir(self, d):
        if d not in self.listing:
            raise OSError(d)
        return list(self.listing[d])

    def readlink(self, p):
        if p not in self.links:
            raise OSError(p)
        return self.links[p]

    def makedirs(self, d, exist_ok=False):
        self.made_dirs.append(d)
        self.existing.add(d)

    def remove(self, p):
        if p not in self.existing:
            raise OSError(p)
        self.existing.discard(p)

    @staticmethod
    def urandom(n):
        return b"\x42" * n


class FakeTime(object):
    def __init__(self, now=1000000.0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, seconds):
        # Advances fake time instantly: retry loops stay fast in tests.
        self.now += seconds


class FakeProc(object):
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class FakeSubprocess(object):
    """Records every subprocess call; per-test handlers script the replies.
    Nothing ever shells out to a real /sbin/wifi, ubus, curl or iw."""

    PIPE = -1
    STDOUT = -2
    DEVNULL = -3

    def __init__(self):
        self.calls = []            # (argv, kwargs)
        self.run_handler = None    # callable(argv) -> FakeProc | None | raise
        self.check_output_handler = None  # callable(argv) -> str | raise

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if self.run_handler is not None:
            r = self.run_handler(list(cmd))
            if r is not None:
                return r
        return FakeProc()

    def check_output(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        if self.check_output_handler is not None:
            return self.check_output_handler(list(cmd))
        return ""


# radio0 = onboard (sdio path), radio1 = pcie; both serve the customer's AP.
BASE_UCI = {
    "wireless.radio0": "wifi-device",
    "wireless.radio0.path": "platform/soc/ffe0000.sdio/mmc_host",
    "wireless.radio0.band": "2g",
    "wireless.radio0.htmode": "HE20",
    "wireless.radio0.channel": "6",
    "wireless.default_radio0": "wifi-iface",
    "wireless.default_radio0.device": "radio0",
    "wireless.default_radio0.network": "lan",
    "wireless.default_radio0.mode": "ap",
    "wireless.default_radio0.ssid": "photonicat2-cafe01",
    "wireless.default_radio0.encryption": "psk2",
    "wireless.default_radio0.key": "hotspot-secret",
    "wireless.radio1": "wifi-device",
    "wireless.radio1.path": "soc/22000000.pcie/pci0000:00/0000:01:00.0",
    "wireless.radio1.band": "5g",
    "wireless.radio1.htmode": "HE80",
    "wireless.radio1.channel": "36",
    "wireless.default_radio1": "wifi-iface",
    "wireless.default_radio1.device": "radio1",
    "wireless.default_radio1.network": "lan",
    "wireless.default_radio1.mode": "ap",
    "wireless.default_radio1.ssid": "photonicat2-cafe01-5g",
    "wireless.default_radio1.encryption": "psk2",
    "wireless.default_radio1.key": "hotspot-secret",
    "firewall.@zone[0].name": "lan",
    "firewall.@zone[1].name": "wan",
    "firewall.@zone[1].network": "wan wan6",
}


class WifiWanHarness(unittest.TestCase):
    """Shared fixture: every external surface of wifi_wan is replaced by an
    in-memory fake, and all module-global mutable state is reset both before
    and after each test so nothing leaks into the rest of the suite."""

    maxDiff = None

    def setUp(self):
        self.uci = FakeUci(BASE_UCI)
        self.commits = []
        self.cfg = FakeConfig()
        self.fos = FakeOs()
        self.ftime = FakeTime()
        self.subproc = FakeSubprocess()
        self.ubus = {}
        self.ubus_log = []
        self.wifi_slot_enabled = True
        self.radios_ready = True
        self.mwan3_disables = []

        self.fos.listing["/sys/class/ieee80211"] = ["phy0", "phy1"]
        self.fos.links["/sys/class/ieee80211/phy0"] = (
            "../../devices/platform/soc/ffe0000.sdio/mmc_host/ieee80211/phy0")
        self.fos.links["/sys/class/ieee80211/phy1"] = (
            "../../devices/soc/22000000.pcie/pci0000:00/0000:01:00.0/ieee80211/phy1")

        def fake_ubus(obj, method, args=None):
            self.ubus_log.append((obj, method, args))
            val = self.ubus.get((obj, method))
            return val(args) if callable(val) else val

        self._patch(wifi_wan, "uci_get", self.uci.get)
        self._patch(wifi_wan, "uci_set", self.uci.set)
        self._patch(wifi_wan, "uci_delete", self.uci.delete)
        self._patch(wifi_wan, "uci_add_list", self.uci.add_list)
        self._patch(wifi_wan, "uci_commit", self.commits.append)
        self._patch(wifi_wan, "uci_show_dict", lambda *_a, **_kw: self.uci.values)
        self._patch(wifi_wan, "ubus_call", fake_ubus)
        self._patch(wifi_wan, "subprocess", self.subproc)
        self._patch(wifi_wan, "os", self.fos)
        self._patch(wifi_wan, "time", self.ftime)

        # wifi_wan reaches wan_manager lazily via `from pcat_util import ...`.
        self._patch(wan_manager, "wifi_metrics", lambda: (20, 21, 1020))
        self._patch(wan_manager, "is_slot_enabled",
                    lambda slot: self.wifi_slot_enabled)
        self._patch(wan_manager, "relay_radios_ready", lambda: self.radios_ready)
        self._patch(wan_manager, "disable_mwan3",
                    lambda: self.mwan3_disables.append(True))

        # wifi_wan does `import pcat_config` inside functions; intercept it.
        self._orig_pcat_config = sys.modules.get("pcat_config")
        sys.modules["pcat_config"] = self.cfg
        self.addCleanup(self._restore_pcat_config)

        # open() inside the module: sysfs MACs and the committed wireless file.
        self.files = {
            "/etc/config/wireless": "config wifi-iface 'wifinet_sta'\n",
            "/sys/class/ieee80211/phy0/macaddress": "00:11:22:33:44:55\n",
            "/sys/class/ieee80211/phy1/macaddress": "00:aa:bb:cc:dd:ee\n",
        }

        def fake_open(path, mode="r", *a, **kw):
            if "w" in mode:
                outer = self

                class _Writer(io.StringIO):
                    def close(inner):
                        outer.files[path] = inner.getvalue()
                        outer.fos.existing.add(path)
                        io.StringIO.close(inner)

                return _Writer()
            if path in self.files:
                return io.StringIO(self.files[path])
            raise OSError(path)

        self._patch(wifi_wan, "open", fake_open, create=True)

        self._reset_module_state()
        self.addCleanup(self._reset_module_state)

    def _patch(self, target, name, repl, create=False):
        p = mock.patch.object(target, name, repl, create=create)
        p.start()
        self.addCleanup(p.stop)

    def _restore_pcat_config(self):
        if self._orig_pcat_config is not None:
            sys.modules["pcat_config"] = self._orig_pcat_config
        else:
            sys.modules.pop("pcat_config", None)

    @staticmethod
    def _reset_module_state():
        wifi_wan._scan_backoff.update(fails=0, next_ok=0.0)
        wifi_wan._roam_scan["next_ok"] = 0.0
        wifi_wan._radio_deficit.update(ticks=0, parked=False)
        wifi_wan._vif_rescue.update(missing_since=0.0, fails=0, next_ok=0.0)
        wifi_wan._conn_cache.update(result="unknown", time=0.0, bad_streak=0)
        wifi_wan._eap_supported_cache = None
        wifi_wan._portal_seen["url"] = ""
        wifi_wan._portal_pin["routes"].clear()
        wifi_wan._portal_pin.update(url="", conf_path="")

    # ── helpers ──────────────────────────────────────────────────────────

    def _ran(self, *prefix):
        """Recorded subprocess calls whose argv starts with `prefix`."""
        prefix = list(prefix)
        return [c for c, _kw in self.subproc.calls if c[:len(prefix)] == prefix]

    def configure_sta(self, ssid="home_wifi", enabled=True, radio="radio0",
                      **opts):
        vals = {
            STA: "wifi-iface", STA + ".device": radio, STA + ".mode": "sta",
            STA + ".network": "wan_wifi", STA + ".ssid": ssid,
            STA + ".encryption": "psk2", STA + ".key": "upstream-pw",
            STA + ".disabled": "0" if enabled else "1",
        }
        for k, v in opts.items():
            vals[STA + "." + k] = v
        self.uci.values.update(vals)

    def wire_sta_netdev(self, radio="radio0", ifname="phy0-sta0",
                        associated=True, info=None, netdev_exists=True):
        """Give the STA radio a live netdev and (optionally) an association."""
        self.ubus[("network.wireless", "status")] = {
            radio: {"interfaces": [
                {"ifname": ifname, "config": {"mode": "sta"}}]}}
        if info is None:
            info = {"ssid": "home_wifi", "signal": -52,
                    "bitrate": 866000} if associated else {}
        self.ubus[("iwinfo", "info")] = info
        if netdev_exists:
            self.fos.existing.add("/sys/class/net/" + ifname)

    def curl_script(self, responses):
        """Script curl replies per URL: {url_substring: raw_stdout}."""
        def handler(cmd):
            if cmd[0] != "curl":
                return None
            for frag, out in responses.items():
                if frag in cmd[-1]:
                    return FakeProc(out)
            return FakeProc("")
        self.subproc.run_handler = handler


# ── deterministic MAC handling ────────────────────────────────────────────────

class StableMacTest(WifiWanHarness):
    def test_mac_is_locally_administered_unicast(self):
        """The derived MAC must be safe to put on air: locally administered
        (won't collide with a vendor OUI) and unicast (a multicast source
        MAC breaks association outright)."""
        mac = wifi_wan.stable_mac_for_ssid("Cafe WiFi")
        first = int(mac.split(":")[0], 16)
        self.assertTrue(first & 0x02, "locally-administered bit not set")
        self.assertFalse(first & 0x01, "multicast bit set on a source MAC")
        self.assertRegex(mac, r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")

    def test_same_ssid_same_mac_across_calls(self):
        """Stability per SSID is the whole point: captive-portal logins and
        DHCP leases survive rejoins only if the MAC does not change."""
        self.assertEqual(wifi_wan.stable_mac_for_ssid("HomeNet"),
                         wifi_wan.stable_mac_for_ssid("HomeNet"))

    def test_different_ssids_get_different_macs(self):
        """iPhone-style privacy: the hotspot at the cafe must not be able to
        correlate this device with the one seen at the office."""
        self.assertNotEqual(wifi_wan.stable_mac_for_ssid("CafeNet"),
                            wifi_wan.stable_mac_for_ssid("OfficeNet"))

    def test_pinned_mac_is_hw_mac_with_local_bit(self):
        """The non-random pin is the radio's permanent MAC + local bit, so an
        AP vif on the same phy (which gets the base MAC) can never collide."""
        self.assertEqual(wifi_wan.pinned_sta_mac("radio0"), "02:11:22:33:44:55")

    def test_pinned_mac_none_when_phy_missing(self):
        """A pulled PCIe card must degrade to 'no pin', not crash the join."""
        del self.fos.links["/sys/class/ieee80211/phy0"]
        self.assertIsNone(wifi_wan.pinned_sta_mac("radio0"))

    def test_pinned_mac_none_when_sysfs_unreadable(self):
        del self.files["/sys/class/ieee80211/phy0/macaddress"]
        self.assertIsNone(wifi_wan.pinned_sta_mac("radio0"))


class ValidCustomMacTest(WifiWanHarness):
    def test_normalizes_case_and_separator(self):
        self.assertEqual(wifi_wan.valid_custom_mac(" AA-BB-CC-11-22-33 "),
                         "aa:bb:cc:11:22:33")
        self.assertEqual(wifi_wan.valid_custom_mac("aa:bb:cc:11:22:33"),
                         "aa:bb:cc:11:22:33")

    def test_globally_administered_is_allowed(self):
        # The use case is cloning an address the upstream authorized, which
        # is a real vendor MAC -- the local bit must not be forced on.
        self.assertEqual(wifi_wan.valid_custom_mac("dc:56:7b:01:02:03"),
                         "dc:56:7b:01:02:03")

    def test_rejects_multicast_broadcast_zero_and_garbage(self):
        for bad in ("01:00:5e:00:00:01",         # multicast bit
                    "ff:ff:ff:ff:ff:ff",
                    "00:00:00:00:00:00",
                    "aa:bb:cc:11:22",             # too short
                    "aa:bb:cc:11:22:33:44",       # too long
                    "zz:bb:cc:11:22:33",
                    ""):
            self.assertEqual(wifi_wan.valid_custom_mac(bad), "", bad)


class EnforceStaNetdevMacTest(WifiWanHarness):
    """_enforce_sta_netdev_mac: the aic8800 fullmac driver ignores the MAC
    handed to it at vif creation, so the configured address must be pushed
    onto the live netdev over rtnetlink (down -> address -> up)."""

    WANT = "dc:56:7b:aa:bb:01"
    HW = "54:01:4a:99:c9:6b"

    def _arm(self, have=HW, want=WANT):
        self.configure_sta(ssid="home", enabled=True, macaddr=want)
        self.wire_sta_netdev(radio="radio0", ifname="phy0-sta0")
        self.files["/sys/class/net/phy0-sta0/address"] = have + "\n"

        def handler(cmd):
            # The driver honors SETLINK (verified on hardware): reflect the
            # address change into the fake sysfs.
            if cmd[:3] == ["ip", "link", "set"] and "address" in cmd:
                self.files["/sys/class/net/%s/address" % cmd[3]] = cmd[-1] + "\n"
            return None
        self.subproc.run_handler = handler

    def _ip_calls(self):
        return [c for c, _kw in self.subproc.calls if c[0] == "ip"]

    def test_mismatched_mac_is_set_down_address_up(self):
        self._arm()
        self.assertTrue(wifi_wan._enforce_sta_netdev_mac("radio0"))
        self.assertEqual(self._ip_calls(), [
            ["ip", "link", "set", "phy0-sta0", "down"],
            ["ip", "link", "set", "phy0-sta0", "address", self.WANT],
            ["ip", "link", "set", "phy0-sta0", "up"],
        ])

    def test_matching_mac_is_left_alone(self):
        # No spurious link bounce: downing a healthy STA drops the uplink.
        self._arm(have=self.WANT)
        self.assertTrue(wifi_wan._enforce_sta_netdev_mac("radio0"))
        self.assertEqual(self._ip_calls(), [])

    def test_no_configured_mac_is_a_noop(self):
        self.configure_sta(ssid="home", enabled=True)
        self.assertTrue(wifi_wan._enforce_sta_netdev_mac("radio0"))
        self.assertEqual(self._ip_calls(), [])

    def test_disabled_sta_is_a_noop(self):
        self.configure_sta(ssid="home", enabled=False, macaddr=self.WANT)
        self.assertTrue(wifi_wan._enforce_sta_netdev_mac("radio0"))
        self.assertEqual(self._ip_calls(), [])

    def test_missing_netdev_reports_failure_not_success(self):
        # No netdev to fix is not "fixed": the reconcile must see False and
        # try again next tick rather than believe the MAC is on the air.
        self.configure_sta(ssid="home", enabled=True, macaddr=self.WANT)
        self.assertFalse(wifi_wan._enforce_sta_netdev_mac("radio0", attempts=2))
        self.assertEqual(self._ip_calls(), [])


# ── radio selection / availability ───────────────────────────────────────────

class RadioSelectionTest(WifiWanHarness):
    def test_radio_by_class_matches_device_path_not_number(self):
        """radioN numbering is not stable across boots; the pcie/onboard
        distinction must come from the device path."""
        self.assertEqual(wifi_wan._radio_by_class("onboard"), "radio0")
        self.assertEqual(wifi_wan._radio_by_class("pcie"), "radio1")

    def test_radio_by_class_none_when_no_paths(self):
        for k in ("wireless.radio0.path", "wireless.radio1.path"):
            del self.uci.values[k]
        self.assertIsNone(wifi_wan._radio_by_class("pcie"))

    def test_get_sta_radio_follows_user_selection(self):
        self.assertEqual(wifi_wan.get_sta_radio(), "radio0")
        self.cfg.wifi_wan_radio = "pcie"
        self.assertEqual(wifi_wan.get_sta_radio(), "radio1")

    def test_get_sta_radio_falls_back_to_radio0(self):
        """With no usable paths at all the STA still lands somewhere."""
        for k in ("wireless.radio0.path", "wireless.radio1.path"):
            del self.uci.values[k]
        self.assertEqual(wifi_wan.get_sta_radio(), "radio0")

    def test_is_available_needs_two_phys(self):
        """One radio can't relay: the STA would park the device's only AP and
        lock out whoever enabled it over WiFi."""
        self.assertTrue(wifi_wan.is_available())
        self.fos.listing["/sys/class/ieee80211"] = ["phy0"]
        self.assertFalse(wifi_wan.is_available())

    def test_is_available_false_without_sysfs(self):
        del self.fos.listing["/sys/class/ieee80211"]
        self.assertFalse(wifi_wan.is_available())

    def test_phy_for_radio_maps_by_path(self):
        self.assertEqual(wifi_wan._phy_for_radio("radio1"), "phy1")
        self.assertIsNone(wifi_wan._phy_for_radio("radio9"))

    def test_phy_for_radio_survives_a_missing_sysfs(self):
        del self.fos.listing["/sys/class/ieee80211"]
        self.assertIsNone(wifi_wan._phy_for_radio("radio0"))


class StatusHelpersTest(WifiWanHarness):
    def test_unconfigured_device_reports_everything_off(self):
        self.assertFalse(wifi_wan.is_configured())
        self.assertFalse(wifi_wan.is_sta_enabled())
        self.assertFalse(wifi_wan.is_active())
        self.assertIsNone(wifi_wan.active_sta_radio())

    def test_configured_needs_an_ssid_not_just_a_section(self):
        """An empty-ssid leftover section must not count as configured, or
        reconcile would treat garbage as a joinable network."""
        self.configure_sta(ssid="")
        self.assertFalse(wifi_wan.is_configured())

    def test_enabled_and_active_in_priority_mode(self):
        self.configure_sta(enabled=True)
        self.assertTrue(wifi_wan.is_sta_enabled())
        self.assertTrue(wifi_wan.is_active())
        self.assertEqual(wifi_wan.active_sta_radio(), "radio0")

    def test_not_active_outside_priority_mode(self):
        """In legacy modes the relay must read inactive so the UI does not
        present the radio's AP as parked."""
        self.configure_sta(enabled=True)
        self.cfg.network_mode = "auto"
        self.assertFalse(wifi_wan.is_active())
        self.assertIsNone(wifi_wan.active_sta_radio())

    def test_disabled_sta_is_not_active(self):
        self.configure_sta(enabled=False)
        self.assertFalse(wifi_wan.is_sta_enabled())
        self.assertFalse(wifi_wan.is_active())

    def test_config_store_failure_reads_as_inactive(self):
        """A broken config layer must degrade to 'relay off', not crash the
        dashboard poll that calls is_active()."""
        self.configure_sta(enabled=True)

        def boom():
            raise RuntimeError("config store gone")
        self.cfg.get_network_mode = boom
        self.assertFalse(wifi_wan.is_active())

    def test_seeding_a_factory_ap_copies_identity_and_pmf(self):
        """A freed radio whose AP regenerated to factory 'OpenWrt' is
        re-seeded from the other radio: band suffix stripped, security and
        802.11w carried over — never an open 'OpenWrt' on air."""
        self.uci.values["wireless.default_radio0.ssid"] = "OpenWrt"
        self.uci.values["wireless.default_radio1.ieee80211w"] = "1"
        self.assertTrue(wifi_wan._ensure_ap_configured("radio0"))
        v = self.uci.values
        self.assertEqual(v["wireless.default_radio0.ssid"], "photonicat2-cafe01")
        self.assertEqual(v["wireless.default_radio0.encryption"], "psk2")
        self.assertEqual(v["wireless.default_radio0.key"], "hotspot-secret")
        self.assertEqual(v["wireless.default_radio0.ieee80211w"], "1")

    def test_seeding_the_pcie_radio_puts_its_own_suffix_back(self):
        """Copying from the onboard radio's bare name must re-add the PCIe
        radio's '-5g', not leave both radios on one name (issue #115)."""
        self.uci.values["wireless.default_radio1.ssid"] = "OpenWrt"
        self.assertTrue(wifi_wan._ensure_ap_configured("radio1"))
        self.assertEqual(self.uci.values["wireless.default_radio1.ssid"],
                         "photonicat2-cafe01-5g")

    def test_backup_taken_mid_relay_keeps_the_original_snapshot(self):
        """A second backup while already relaying must not overwrite the
        original AP identity with the parked/regenerated state."""
        wifi_wan._backup_radio_state("radio0")
        self.uci.values["wireless.default_radio0.ssid"] = "OpenWrt"
        wifi_wan._backup_radio_state("radio0")
        self.assertEqual(
            self.cfg.keys[wifi_wan._RADIO_BACKUP_KEY]["ap"]["ssid"],
            "photonicat2-cafe01")


# ── scanning ──────────────────────────────────────────────────────────────────

class ParseEncryptionTest(WifiWanHarness):
    def test_scan_encryption_mapping(self):
        """Each iwinfo auth flavour must map to a UCI value the STA can
        actually use; a wrong mapping means the join silently never
        associates."""
        cases = [
            (None, "none"),
            ({"enabled": False}, "none"),
            ({"enabled": True, "authentication": ["802.1x"]}, "eap"),
            ({"enabled": True, "authentication": ["sae"]}, "sae"),
            # WPA2/WPA3 mixed joins as psk2 for broadest compatibility.
            ({"enabled": True, "authentication": ["sae", "psk"]}, "psk2"),
            ({"enabled": True, "authentication": ["psk"], "wpa": [1]}, "psk"),
            ({"enabled": True, "authentication": ["psk"], "wpa": [2]}, "psk2"),
            ({"enabled": True}, "wep"),
            ({"enabled": True, "wpa": [2]}, "psk2"),
        ]
        for enc, expected in cases:
            self.assertEqual(wifi_wan._parse_encryption(enc), expected, enc)


class ScanTest(WifiWanHarness):
    def _scan_returns(self, results):
        payload = json.dumps({"results": results})
        self.subproc.check_output_handler = lambda cmd: payload

    def test_scan_dedupes_per_ssid_band_keeping_strongest(self):
        """A dual-AP SSID must show once per band with its best signal, or
        the UI ranks (and the keeper joins) the weak BSS."""
        self.wire_sta_netdev()
        self._scan_returns([
            {"ssid": "home", "band": 5, "channel": 36, "signal": -70,
             "encryption": {"enabled": True, "authentication": ["psk"], "wpa": [2]}},
            {"ssid": "home", "band": 5, "channel": 44, "signal": -48,
             "encryption": {"enabled": True, "authentication": ["psk"], "wpa": [2]}},
            {"ssid": "home", "band": 2, "channel": 6, "signal": -60,
             "encryption": {"enabled": True, "authentication": ["psk"], "wpa": [2]}},
        ])
        networks, err = wifi_wan.scan("radio0")
        self.assertIsNone(err)
        self.assertEqual([(n["ssid"], n["band"], n["signal"]) for n in networks],
                         [("home", "5g", -48), ("home", "2g", -60)])

    def test_scan_excludes_own_aps_and_hidden(self):
        """Joining the device's own hotspot would loop traffic back into
        itself; hidden SSIDs are join-by-hand only."""
        self.wire_sta_netdev()
        self._scan_returns([
            {"ssid": "photonicat2-cafe01", "band": 2, "signal": -30,
             "encryption": {"enabled": True, "authentication": ["psk"], "wpa": [2]}},
            {"ssid": "", "band": 2, "signal": -40, "encryption": None},
            {"ssid": "neighbour", "band": 2, "signal": -66,
             "encryption": {"enabled": True, "authentication": ["psk"], "wpa": [2]}},
        ])
        networks, _ = wifi_wan.scan("radio0")
        self.assertEqual([n["ssid"] for n in networks], ["neighbour"])

    def test_scan_flags_unsupported_networks(self):
        """WEP and 6 GHz results must be marked unsupported so the UI can
        grey them out instead of letting a join fail mysteriously."""
        self.wire_sta_netdev()
        self._scan_returns([
            {"ssid": "old-wep", "band": 2, "signal": -50,
             "encryption": {"enabled": True}},
            {"ssid": "wifi6e", "band": 6, "signal": -40,
             "encryption": {"enabled": True, "authentication": ["psk"], "wpa": [3]}},
            {"ssid": "normal", "band": 5, "signal": -45,
             "encryption": {"enabled": True, "authentication": ["sae"]}},
        ])
        networks, _ = wifi_wan.scan("radio0")
        by = {n["ssid"]: n for n in networks}
        self.assertFalse(by["old-wep"]["supported"])
        self.assertFalse(by["wifi6e"]["supported"])
        self.assertTrue(by["normal"]["supported"])

    def test_scan_retries_once_on_empty_result(self):
        """Busy radios return empty scans; one retry keeps the picker from
        showing 'no networks' in a crowded room."""
        self.wire_sta_netdev()
        outs = ["", json.dumps({"results": [
            {"ssid": "home", "band": 2, "signal": -50,
             "encryption": {"enabled": True, "authentication": ["psk"], "wpa": [2]}}]})]
        self.subproc.check_output_handler = lambda cmd: outs.pop(0)
        networks, err = wifi_wan.scan("radio0")
        self.assertIsNone(err)
        self.assertEqual(len(networks), 1)
        self.assertEqual(len(self._ran("ubus", "call", "iwinfo", "scan")), 2)

    def test_scan_with_no_usable_ifname_reports_radio_disabled(self):
        self.ubus[("network.wireless", "status")] = {}
        self.ubus[("iwinfo", "devices")] = {"devices": []}
        networks, err = wifi_wan.scan("radio0")
        self.assertEqual(networks, [])
        self.assertEqual(err, "radio_disabled")

    def test_ubus_scan_failure_is_swallowed(self):
        """A scan subprocess blowing up must never take the web app down."""
        def boom(cmd):
            raise RuntimeError("ubus timeout")
        self.subproc.check_output_handler = boom
        self.assertEqual(wifi_wan._ubus_scan("phy0-sta0"), [])


class ScanIfnameTest(WifiWanHarness):
    def test_prefers_the_live_sta_netdev(self):
        self.ubus[("network.wireless", "status")] = {
            "radio0": {"interfaces": [
                {"ifname": "phy0-ap0", "config": {"mode": "ap"}},
                {"ifname": "phy0-sta0", "config": {"mode": "sta"}}]}}
        self.assertEqual(wifi_wan._scan_ifname("radio0"), "phy0-sta0")
        self.assertEqual(wifi_wan._sta_ifname("radio0"), "phy0-sta0")

    def test_falls_back_to_any_iface_on_the_radio(self):
        self.ubus[("network.wireless", "status")] = {
            "radio0": {"interfaces": [
                {"ifname": "phy0-ap0", "config": {"mode": "ap"}}]}}
        self.assertEqual(wifi_wan._scan_ifname("radio0"), "phy0-ap0")
        self.assertIsNone(wifi_wan._sta_ifname("radio0"))

    def test_falls_back_to_kernel_netdev_matched_by_phy(self):
        """With the radio's netifd ifaces gone (AP parked, no STA yet) a
        lingering kernel netdev on the right phy still allows scanning."""
        self.ubus[("network.wireless", "status")] = {}
        self.ubus[("iwinfo", "devices")] = {"devices": ["phy1-ap0", "phy0-sta0"]}
        self.assertEqual(wifi_wan._scan_ifname("radio0"), "phy0-sta0")

    def test_last_resort_any_device_then_none(self):
        self.ubus[("network.wireless", "status")] = {}
        self.ubus[("iwinfo", "devices")] = {"devices": ["wlan9"]}
        del self.uci.values["wireless.radio0.path"]
        self.assertEqual(wifi_wan._scan_ifname("radio0"), "wlan9")
        self.ubus[("iwinfo", "devices")] = {"devices": []}
        self.assertIsNone(wifi_wan._scan_ifname("radio0"))


# ── UCI plumbing ─────────────────────────────────────────────────────────────

class EnsureNetworkIfacesTest(WifiWanHarness):
    def test_creates_both_interfaces_with_table_metrics(self):
        """wan_wifi/wan_wifi_v6 carry the table metrics (v6 = v4+1) and keep
        the DHCP lease across netifd bounces (norelease) so the router's WAN
        IP does not wander per reconnect."""
        self.assertTrue(wifi_wan._ensure_network_ifaces())
        v = self.uci.values
        self.assertEqual(v["network.wan_wifi"], "interface")
        self.assertEqual(v["network.wan_wifi.proto"], "dhcp")
        self.assertEqual(v["network.wan_wifi.metric"], "20")
        self.assertEqual(v["network.wan_wifi.norelease"], "1")
        self.assertEqual(v["network.wan_wifi_v6.proto"], "dhcpv6")
        self.assertEqual(v["network.wan_wifi_v6.device"], "@wan_wifi")
        self.assertEqual(v["network.wan_wifi_v6.metric"], "21")
        self.assertIn("network", self.commits)

    def test_second_run_is_a_no_op(self):
        wifi_wan._ensure_network_ifaces()
        self.commits[:] = []
        self.assertFalse(wifi_wan._ensure_network_ifaces())
        self.assertEqual(self.commits, [])


class EnsureFirewallTest(WifiWanHarness):
    def test_adds_upstream_networks_to_wan_zone_and_reloads(self):
        """Without the zone membership the joined upstream carries no NATed
        traffic at all — associated but nothing loads."""
        self.assertTrue(wifi_wan._ensure_firewall())
        self.assertEqual(self.uci.values["firewall.@zone[1].network"],
                         "wan wan6 wan_wifi wan_wifi_v6")
        self.assertIn("firewall", self.commits)
        self.assertTrue(self._ran("/etc/init.d/firewall", "reload"))

    def test_already_member_means_no_commit_no_reload(self):
        self.uci.values["firewall.@zone[1].network"] = "wan wan_wifi wan_wifi_v6"
        self.assertTrue(wifi_wan._ensure_firewall())
        self.assertEqual(self.commits, [])
        self.assertFalse(self._ran("/etc/init.d/firewall"))

    def test_missing_wan_zone_fails_loudly_not_silently(self):
        self.uci.values["firewall.@zone[1].name"] = "guest"
        self.assertFalse(wifi_wan._ensure_firewall())
        self.assertEqual(self.commits, [])

    def test_zone_search_gives_up_after_twenty_zones(self):
        for i in range(20):
            self.uci.values["firewall.@zone[{}].name".format(i)] = "z{}".format(i)
        self.assertFalse(wifi_wan._ensure_firewall())

    def test_firewall_reload_failure_does_not_undo_the_config(self):
        """The zone membership is committed even if the reload subprocess
        dies; the next firewall restart will pick it up."""
        def boom(cmd):
            if cmd[:1] == ["/etc/init.d/firewall"]:
                raise RuntimeError("procd busy")
            return None
        self.subproc.run_handler = boom
        self.assertTrue(wifi_wan._ensure_firewall())
        self.assertIn("firewall", self.commits)


class CommitVerifiedTest(WifiWanHarness):
    def test_verified_commit_succeeds_first_try(self):
        self.assertTrue(wifi_wan._commit_wireless_verified())
        self.assertEqual(self.commits, ["wireless"])

    def test_commit_that_never_reaches_flash_is_retried_then_reported(self):
        """The 'have to reconnect after every restart' bug: a quietly dead
        commit leaves the STA only in /tmp staging. It must be retried and
        the failure surfaced, never assumed OK."""
        self.files["/etc/config/wireless"] = "config wifi-device 'radio0'\n"
        self.assertFalse(wifi_wan._commit_wireless_verified())
        self.assertEqual(self.commits, ["wireless", "wireless"])

    def test_unreadable_config_file_counts_as_failure(self):
        del self.files["/etc/config/wireless"]
        self.assertFalse(wifi_wan._commit_wireless_verified())
        self.assertEqual(self.commits, ["wireless", "wireless"])


class BandTuningTest(WifiWanHarness):
    def test_band_defaults_are_applied(self):
        wifi_wan._tune_radio_for_band("radio0", "5g")
        self.assertEqual(self.uci.values["wireless.radio0.band"], "5g")
        self.assertEqual(self.uci.values["wireless.radio0.htmode"], "HE80")
        self.assertEqual(self.uci.values["wireless.radio0.channel"], "36")

    def test_explicit_channel_hint_wins(self):
        wifi_wan._tune_radio_for_band("radio0", "2g", channel=11)
        self.assertEqual(self.uci.values["wireless.radio0.htmode"], "HE20")
        self.assertEqual(self.uci.values["wireless.radio0.channel"], "11")

    def test_unknown_band_leaves_the_radio_alone(self):
        """A 6 GHz (or garbage) band must not half-configure the radio."""
        wifi_wan._tune_radio_for_band("radio0", "6g")
        self.assertEqual(self.uci.values["wireless.radio0.band"], "2g")
        self.assertEqual(self.uci.values["wireless.radio0.channel"], "6")


# ── encryption writing ───────────────────────────────────────────────────────

class WriteEncryptionTest(WifiWanHarness):
    def test_psk_writes_key_and_clears_stale_eap(self):
        """Switching a saved network from enterprise to PSK must not leave
        identity/password behind — wpa_supplicant chokes on the mix."""
        self.configure_sta()
        for opt, val in (("identity", "alice"), ("password", "old"),
                         ("eap_type", "peap"), ("auth", "EAP-MSCHAPV2")):
            self.uci.values[STA + "." + opt] = val
        wifi_wan._write_encryption("home", "newpass", "psk2", None)
        self.assertEqual(self.uci.values[STA + ".encryption"], "psk2")
        self.assertEqual(self.uci.values[STA + ".key"], "newpass")
        for opt in ("identity", "password", "eap_type", "auth"):
            self.assertNotIn(STA + "." + opt, self.uci.values)

    def test_open_network_clears_the_old_key(self):
        self.configure_sta()
        wifi_wan._write_encryption("cafe", "", "none", None)
        self.assertEqual(self.uci.values[STA + ".encryption"], "none")
        self.assertNotIn(STA + ".key", self.uci.values)

    def test_eap_peap_writes_identity_and_password_and_clears_psk_key(self):
        """Joining an EAP network writes identity+password and drops the
        stale PSK key, so eduroam-style networks actually authenticate."""
        self.configure_sta()
        wifi_wan._write_encryption("eduroam", "s3cret", "eap", {
            "type": "peap", "identity": "alice@uni.edu",
            "anonymous_identity": "anon@uni.edu", "ca_cert": "/etc/ca.pem"})
        v = self.uci.values
        self.assertEqual(v[STA + ".encryption"], "wpa2")
        self.assertEqual(v[STA + ".eap_type"], "peap")
        self.assertEqual(v[STA + ".auth"], "EAP-MSCHAPV2")
        self.assertEqual(v[STA + ".identity"], "alice@uni.edu")
        self.assertEqual(v[STA + ".password"], "s3cret")
        self.assertEqual(v[STA + ".anonymous_identity"], "anon@uni.edu")
        self.assertEqual(v[STA + ".ca_cert"], "/etc/ca.pem")
        self.assertNotIn(STA + ".key", v)

    def test_eap_tls_carries_no_password(self):
        self.configure_sta()
        wifi_wan._write_encryption("corp", "ignored", "eap",
                                   {"type": "tls", "ca_cert": "/etc/ca.pem"})
        self.assertEqual(self.uci.values[STA + ".eap_type"], "tls")
        self.assertNotIn(STA + ".password", self.uci.values)
        self.assertNotIn(STA + ".auth", self.uci.values)

    def test_unknown_eap_type_falls_back_to_peap(self):
        self.configure_sta()
        wifi_wan._write_encryption("x", "pw", "eap", {"type": "leap"})
        self.assertEqual(self.uci.values[STA + ".eap_type"], "peap")

    def test_eap_from_saved(self):
        self.assertIsNone(wifi_wan.eap_from_saved({"encryption": "psk2"}))
        self.assertIsNone(wifi_wan.eap_from_saved(None))
        d = wifi_wan.eap_from_saved({"encryption": "eap", "eap_type": "ttls",
                                     "identity": "bob"})
        self.assertEqual(d["type"], "ttls")
        self.assertEqual(d["identity"], "bob")


# ── apply_config / set_sta_enabled / remove_sta ──────────────────────────────

class ApplyConfigTest(WifiWanHarness):
    def test_join_writes_sta_parks_ap_and_brings_link_up(self):
        """The full join: STA section on the right radio and band, its own AP
        parked, MAC pinned, config committed, wifi reloaded and wan_wifi
        brought up — and the network remembered for the post-reboot rebuild."""
        self.assertTrue(wifi_wan.apply_config("home", "pw123", "psk2", "5g"))
        v = self.uci.values
        self.assertEqual(v[STA], "wifi-iface")
        self.assertEqual(v[STA + ".device"], "radio0")
        self.assertEqual(v[STA + ".mode"], "sta")
        self.assertEqual(v[STA + ".network"], "wan_wifi")
        self.assertEqual(v[STA + ".ssid"], "home")
        self.assertEqual(v[STA + ".key"], "pw123")
        self.assertEqual(v[STA + ".disabled"], "0")
        self.assertEqual(v[STA + ".macaddr"], "02:11:22:33:44:55")
        # radio retuned to the upstream's band, its own AP parked
        self.assertEqual(v["wireless.radio0.band"], "5g")
        self.assertEqual(v["wireless.radio0.htmode"], "HE80")
        self.assertEqual(v["wireless.default_radio0.disabled"], "1")
        # plumbing + persistence
        self.assertIn("wireless", self.commits)
        self.assertEqual(self.cfg.keys["wifi_wan_last_ssid"], "home")
        self.assertEqual(self.mwan3_disables, [True])
        self.assertTrue(self._ran("/sbin/wifi", "reload"))
        self.assertTrue(self._ran("ifup", "wan_wifi"))
        # radio backup was taken so the AP identity can come back later
        self.assertEqual(
            self.cfg.keys[wifi_wan._RADIO_BACKUP_KEY]["ap"]["ssid"],
            "photonicat2-cafe01")

    def test_random_mac_uses_the_stable_per_ssid_address(self):
        wifi_wan.apply_config("home", "pw", "psk2", "2g", random_mac=True)
        self.assertEqual(self.uci.values[STA + ".macaddr"],
                         wifi_wan.stable_mac_for_ssid("home"))

    def test_custom_mac_wins_over_random_and_pinned(self):
        # A user-supplied MAC (e.g. the address the upstream authorized) must
        # be written verbatim -- normalized -- even with random_mac also set.
        wifi_wan.apply_config("home", "pw", "psk2", "2g",
                              random_mac=True, custom_mac="AA-BB-CC-11-22-33")
        self.assertEqual(self.uci.values[STA + ".macaddr"], "aa:bb:cc:11:22:33")

    def test_no_phy_leaves_macaddr_unset_rather_than_wrong(self):
        del self.fos.links["/sys/class/ieee80211/phy0"]
        wifi_wan.apply_config("home", "pw", "psk2", "2g")
        self.assertNotIn(STA + ".macaddr", self.uci.values)

    def test_join_holds_off_the_roam_scan_one_interval(self):
        """A just-joined link must settle before the roamer may consider
        jumping off it, or two comparable APs ping-pong."""
        # Measured from the join moment: the MAC-enforcement retries at the
        # tail of apply_config advance the (fake) clock afterwards.
        t_join = self.ftime.now
        wifi_wan.apply_config("home", "pw", "psk2", "2g")
        self.assertEqual(wifi_wan._roam_scan["next_ok"],
                         t_join + wifi_wan._ROAM_SCAN_INTERVAL)


class SetStaEnabledTest(WifiWanHarness):
    def test_noop_when_nothing_configured(self):
        self.assertFalse(wifi_wan.set_sta_enabled(True))
        self.assertFalse(self._ran("/sbin/wifi"))

    def test_enable_retunes_parks_ap_and_reloads(self):
        """Re-entering smart WAN: the exit path restored the AP band, so the
        radio must be retuned to the remembered upstream band, the AP parked
        again, and the link brought up."""
        self.configure_sta(ssid="home", enabled=False)
        self.cfg.networks = [{"ssid": "home", "band": "5g", "channel": 40}]
        self.assertTrue(wifi_wan.set_sta_enabled(True))
        v = self.uci.values
        self.assertEqual(v[STA + ".disabled"], "0")
        self.assertEqual(v["wireless.radio0.band"], "5g")
        self.assertEqual(v["wireless.radio0.channel"], "40")
        self.assertEqual(v["wireless.default_radio0.disabled"], "1")
        self.assertTrue(self._ran("/sbin/wifi", "reload"))
        self.assertTrue(self._ran("ifup", "wan_wifi"))
        self.assertEqual(self.mwan3_disables, [True])

    def test_enable_when_already_enabled_changes_nothing(self):
        self.configure_sta(enabled=True)
        self.assertFalse(wifi_wan.set_sta_enabled(True))
        self.assertFalse(self._ran("/sbin/wifi"),
                         "an idempotent enable must not bounce the radio")

    def test_disable_restores_the_ap_and_downs_the_link(self):
        self.configure_sta(enabled=True)
        wifi_wan._backup_radio_state("radio0")
        self.uci.values["wireless.default_radio0.disabled"] = "1"
        self.assertTrue(wifi_wan.set_sta_enabled(False))
        self.assertEqual(self.uci.values[STA + ".disabled"], "1")
        self.assertNotEqual(
            self.uci.values.get("wireless.default_radio0.disabled"), "1",
            "the customer's hotspot stayed parked after the relay stopped")
        self.assertTrue(self._ran("ifdown", "wan_wifi"))


class RemoveStaTest(WifiWanHarness):
    def test_forget_drops_the_section_and_restores_the_ap(self):
        self.configure_sta(enabled=True)
        wifi_wan._backup_radio_state("radio0")
        self.uci.values["wireless.default_radio0.disabled"] = "1"
        self.assertTrue(wifi_wan.remove_sta())
        self.assertNotIn(STA, self.uci.values)
        self.assertNotIn(STA + ".ssid", self.uci.values)
        self.assertNotEqual(
            self.uci.values.get("wireless.default_radio0.disabled"), "1")
        self.assertIn("wireless", self.commits)
        self.assertTrue(self._ran("ifdown", "wan_wifi"))
        self.assertTrue(self._ran("/sbin/wifi", "reload"))

    def test_nothing_to_remove_is_a_quiet_no_op(self):
        self.assertFalse(wifi_wan.remove_sta())
        self.assertEqual(self.commits, [])
        self.assertFalse(self._ran("/sbin/wifi"))


class SetStaRadioTest(WifiWanHarness):
    def test_rejects_unknown_class_and_no_op_selection(self):
        self.assertFalse(wifi_wan.set_sta_radio("usb"))
        self.assertFalse(wifi_wan.set_sta_radio("onboard"))  # already there

    def test_refuses_to_move_onto_a_dead_radio(self):
        """Moving the upstream onto a pulled PCIe card would take WiFi-as-WAN
        down with no way back; a UCI section alone is not live hardware."""
        del self.fos.links["/sys/class/ieee80211/phy1"]
        self.assertFalse(wifi_wan.set_sta_radio("pcie"))
        self.assertEqual(self.cfg.wifi_wan_radio, "onboard")

    def test_moving_a_disabled_sta_just_repoints_it(self):
        self.configure_sta(ssid="home", enabled=False)
        self.assertTrue(wifi_wan.set_sta_radio("pcie"))
        self.assertEqual(self.cfg.wifi_wan_radio, "pcie")
        self.assertEqual(self.uci.values[STA + ".device"], "radio1")
        self.assertIn("wireless", self.commits)
        self.assertFalse(self._ran("/sbin/wifi"),
                         "no live link, nothing to reload")

    def test_moving_a_live_sta_swaps_the_radio_roles(self):
        """The freed radio gets its AP back on air; the new radio is retuned
        to the upstream band and its AP parked — the radios swap roles."""
        self.configure_sta(ssid="home", enabled=True)
        wifi_wan._backup_radio_state("radio0")
        self.uci.values["wireless.default_radio0.disabled"] = "1"
        self.cfg.networks = [{"ssid": "home", "band": "5g", "channel": 44}]

        self.assertTrue(wifi_wan.set_sta_radio("pcie"))
        v = self.uci.values
        self.assertEqual(v[STA + ".device"], "radio1")
        # old radio serves clients again
        self.assertNotEqual(v.get("wireless.default_radio0.disabled"), "1")
        self.assertEqual(v["wireless.radio0.disabled"], "0")
        # new radio relays: tuned to the saved band, AP parked
        self.assertEqual(v["wireless.radio1.channel"], "44")
        self.assertEqual(v["wireless.default_radio1.disabled"], "1")
        # fresh backup for the new radio so ITS AP can come back later
        self.assertEqual(
            self.cfg.keys[wifi_wan._RADIO_BACKUP_KEY]["ap"]["ssid"],
            "photonicat2-cafe01-5g")
        self.assertTrue(self._ran("/sbin/wifi", "reload"))
        self.assertTrue(self._ran("ifup", "wan_wifi"))


# ── connectivity probe / captive-portal detection ────────────────────────────

class IpEchoTest(WifiWanHarness):
    def test_ip_bodies_pass_and_html_fails(self):
        self.assertTrue(wifi_wan._looks_like_ip_echo("203.0.113.9"))
        self.assertTrue(wifi_wan._looks_like_ip_echo(" 2001:db8::1 \n"))
        self.assertFalse(wifi_wan._looks_like_ip_echo("<html>login</html>"))
        self.assertFalse(wifi_wan._looks_like_ip_echo(""))
        self.assertFalse(wifi_wan._looks_like_ip_echo(None))
        self.assertFalse(wifi_wan._looks_like_ip_echo("x" * 100))


class CurlProbeTest(WifiWanHarness):
    def test_splits_body_from_status_code(self):
        self.subproc.run_handler = lambda cmd: FakeProc("1.2.3.4\n200\t")
        self.assertEqual(wifi_wan._curl_probe("http://x", "wlan0"),
                         ("200", "1.2.3.4", ""))

    def test_captures_the_redirect_target(self):
        """A portal's 30x carries its login URL in Location; -w surfaces it
        after the status code so portal handling knows where to pin."""
        self.subproc.run_handler = lambda cmd: FakeProc(
            "\n302\thttp://gcppty.gw/login?dst=http%3A%2F%2Fbbb.com%2F")
        self.assertEqual(
            wifi_wan._curl_probe("http://x", "wlan0"),
            ("302", "", "http://gcppty.gw/login?dst=http%3A%2F%2Fbbb.com%2F"))

    def test_no_output_or_crash_reads_as_000(self):
        self.subproc.run_handler = lambda cmd: FakeProc("")
        self.assertEqual(wifi_wan._curl_probe("http://x", "wlan0"),
                         ("000", "", ""))

        def boom(cmd):
            raise RuntimeError("no curl")
        self.subproc.run_handler = boom
        self.assertEqual(wifi_wan._curl_probe("http://x", "wlan0"),
                         ("000", "", ""))

    def test_probe_binds_to_the_upstream_netdev(self):
        """The probe must test the upstream specifically, not whatever the
        default route happens to be, or a live cellular link masks a dead
        WiFi."""
        self.subproc.run_handler = lambda cmd: FakeProc("\n200")
        wifi_wan._curl_probe("http://x", "phy0-sta0")
        argv = self.subproc.calls[-1][0]
        self.assertIn("--interface", argv)
        self.assertEqual(argv[argv.index("--interface") + 1], "phy0-sta0")


class ProbeOnceTest(WifiWanHarness):
    def test_tls_200_is_online(self):
        """A valid TLS 200 cannot be a spoofed portal page."""
        self.curl_script({"https://": "203.0.113.9\n200"})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "online")

    def test_nothing_answering_is_offline(self):
        self.curl_script({})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "offline")

    def test_http_ip_echo_with_broken_tls_is_still_online(self):
        """TLS hiccups happen; our own IP echoed back over HTTP proves the
        uplink works. Calling this a portal caused needless cellular
        failovers."""
        self.curl_script({"https://": "", "http://": "203.0.113.9\n200"})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "online")

    def test_http_redirect_is_a_portal(self):
        self.curl_script({"https://": "", "http://": "\n302"})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")

    def test_http_200_with_foreign_body_is_a_portal(self):
        self.curl_script({"https://": "",
                          "http://": "<html>hotel login</html>\n200"})
        self.assertEqual(wifi_wan._probe_once("wlan0"), "portal")


class ProbeConnectivityTest(WifiWanHarness):
    def test_no_netdev_is_offline_without_probing(self):
        self.assertEqual(wifi_wan._probe_connectivity(None), "offline")
        self.assertEqual(self.subproc.calls, [])

    def test_one_good_attempt_wins_immediately(self):
        """Biased toward online: a single dropped packet must not flip the
        verdict and bounce egress to cellular."""
        rounds = {"n": 0}

        def handler(cmd):
            if "https://" in cmd[-1]:
                rounds["n"] += 1
                return FakeProc("203.0.113.9\n200" if rounds["n"] > 1 else "")
            return FakeProc("\n302")
        self.subproc.run_handler = handler
        self.assertEqual(wifi_wan._probe_connectivity("wlan0"), "online")

    def test_persistent_portal_is_reported(self):
        self.curl_script({"https://": "", "http://": "\n302"})
        self.assertEqual(wifi_wan._probe_connectivity("wlan0"), "portal")


class RefreshConnectivityTest(WifiWanHarness):
    def _probe_seq(self, results):
        seq = list(results)
        self._patch(wifi_wan, "_probe_connectivity", lambda dev: seq.pop(0))

    def test_single_bad_round_does_not_leave_online(self):
        """Flap damping: one glitch must not demote the route and bounce all
        egress to cellular for a reconcile cycle."""
        self._probe_seq(["online", "offline", "offline"])
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online")
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online",
                         "first bad round flipped the verdict")
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "offline",
                         "sustained failure must eventually be believed")

    def test_recovery_to_online_is_immediate(self):
        # The first failed round from boot's 'unknown' is held (it races
        # DHCP/DNS settling); sustained failure is believed on the second.
        self._probe_seq(["offline", "offline", "online"])
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "unknown")
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "offline")
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "online")
        self.assertEqual(wifi_wan._conn_cache["bad_streak"], 0)

    def test_portal_offline_changes_pass_through_undamped(self):
        self._probe_seq(["portal", "offline"])
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "portal")
        self.assertEqual(wifi_wan._refresh_connectivity("wlan0"), "offline")

    def test_connectivity_state_reads_the_cache(self):
        self.assertEqual(wifi_wan.connectivity_state(), "unknown")
        self._probe_seq(["portal"])
        wifi_wan._refresh_connectivity("wlan0")
        self.assertEqual(wifi_wan.connectivity_state(), "portal")


class LoginHookTest(WifiWanHarness):
    def test_hook_runs_with_iface_and_gateway_exported(self):
        """Travelmate-style auto-login scripts need to know which netdev and
        gateway to talk to."""
        self.ubus[("network.interface.wan_wifi", "status")] = {
            "route": [{"target": "0.0.0.0", "nexthop": "192.168.8.1"}]}
        wifi_wan._run_login_cmd("curl portal/login", "phy0-sta0")
        calls = self._ran("sh", "-c", "curl portal/login")
        self.assertEqual(len(calls), 1)
        env = self.subproc.calls[-1][1]["env"]
        self.assertEqual(env["WIFI_WAN_IFACE"], "phy0-sta0")
        self.assertEqual(env["WIFI_WAN_GATEWAY"], "192.168.8.1")

    def test_no_command_no_subprocess(self):
        wifi_wan._run_login_cmd("", "phy0-sta0")
        self.assertEqual(self.subproc.calls, [])

    def test_hook_failure_never_raises(self):
        self.ubus[("network.interface.wan_wifi", "status")] = {}

        def boom(cmd):
            raise RuntimeError("sh missing")
        self.subproc.run_handler = boom
        wifi_wan._run_login_cmd("exit 1", "phy0-sta0")  # must not raise


# ── metric demotion / promotion ──────────────────────────────────────────────

class WifiMetricTest(WifiWanHarness):
    def test_demotion_moves_both_address_families(self):
        """Demoting only v4 left all IPv6 flowing to a portal-locked uplink;
        both families must move together, v6 one above v4."""
        wifi_wan._ensure_network_ifaces()
        self.assertTrue(wifi_wan._set_wifi_metric(1020))
        self.assertEqual(self.uci.values["network.wan_wifi.metric"], "1020")
        self.assertEqual(self.uci.values["network.wan_wifi_v6.metric"], "1021")
        self.assertTrue(self._ran("ifup", "wan_wifi"))
        self.assertTrue(self._ran("ifup", "wan_wifi_v6"))

    def test_half_demoted_device_is_repaired(self):
        """A device left half-demoted by the old v4-only code (v4 moved, v6
        stuck) must be corrected even though its v4 already matches."""
        wifi_wan._ensure_network_ifaces()
        self.uci.values["network.wan_wifi.metric"] = "1020"
        self.uci.values["network.wan_wifi_v6.metric"] = "21"
        self.assertTrue(wifi_wan._set_wifi_metric(1020))
        self.assertEqual(self.uci.values["network.wan_wifi_v6.metric"], "1021")

    def test_matching_metrics_are_left_alone(self):
        wifi_wan._ensure_network_ifaces()
        self.commits[:] = []
        self.assertFalse(wifi_wan._set_wifi_metric(20))
        self.assertEqual(self.commits, [])


class PromoteIfConnectedTest(WifiWanHarness):
    def _wire_up(self, up=True, ip="10.0.0.2"):
        self.ubus[("network.interface.wan_wifi", "status")] = {
            "up": up, "ipv4-address": [{"address": ip}] if ip else []}
        self.ubus[("network.interface.wan_wifi_v6", "status")] = {
            "ipv6-address": []}

    def test_restores_preference_when_the_sta_has_an_address(self):
        """When a higher-priority uplink dies, a still-connected WiFi must not
        stay demoted under cellular."""
        self.configure_sta(enabled=True)
        wifi_wan._ensure_network_ifaces()
        self.uci.values["network.wan_wifi.metric"] = "1020"
        self.uci.values["network.wan_wifi_v6.metric"] = "1021"
        self._wire_up()
        self.assertTrue(wifi_wan.promote_if_connected())
        self.assertEqual(self.uci.values["network.wan_wifi.metric"], "20")
        self.assertEqual(wifi_wan._conn_cache["result"], "online",
                         "flap state must be cleared so one post-handoff "
                         "glitch does not re-demote")

    def test_already_preferred_reports_true_without_churn(self):
        self.configure_sta(enabled=True)
        wifi_wan._ensure_network_ifaces()
        self._wire_up()
        self.commits[:] = []
        self.assertTrue(wifi_wan.promote_if_connected())
        self.assertEqual(self.commits, [])

    def test_disabled_down_or_addressless_sta_is_not_promoted(self):
        self.assertFalse(wifi_wan.promote_if_connected())  # not enabled
        self.configure_sta(enabled=True)
        self._wire_up(up=False)
        self.assertFalse(wifi_wan.promote_if_connected())
        self._wire_up(up=True, ip=None)
        self.assertFalse(wifi_wan.promote_if_connected())

    def test_nudge_rejoin_clears_both_scan_timers(self):
        wifi_wan._scan_backoff.update(fails=3, next_ok=self.ftime.now + 60)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        wifi_wan.nudge_rejoin()
        self.assertEqual(wifi_wan._scan_backoff["next_ok"], 0.0)
        self.assertEqual(wifi_wan._roam_scan["next_ok"], 0.0)


# ── link signal parsing / get_state ──────────────────────────────────────────

class IwLinkSignalTest(WifiWanHarness):
    def test_parses_the_signal_line(self):
        self.subproc.run_handler = lambda cmd: FakeProc(
            "Connected to aa:bb:cc:dd:ee:ff (on phy0-sta0)\n"
            "\tsignal: -45 dBm\n\trx bitrate: 866.7 MBit/s\n")
        self.assertEqual(wifi_wan._iw_link_signal("phy0-sta0"), -45)

    def test_garbage_and_failures_read_as_none(self):
        self.subproc.run_handler = lambda cmd: FakeProc("signal: strong\n")
        self.assertIsNone(wifi_wan._iw_link_signal("phy0-sta0"))
        self.subproc.run_handler = lambda cmd: FakeProc("Not connected.\n")
        self.assertIsNone(wifi_wan._iw_link_signal("phy0-sta0"))

        def boom(cmd):
            raise RuntimeError("no iw")
        self.subproc.run_handler = boom
        self.assertIsNone(wifi_wan._iw_link_signal("phy0-sta0"))

    def test_dbm_to_percent_scale(self):
        self.assertEqual(wifi_wan.signal_dbm_to_percent(-50), 100)
        self.assertEqual(wifi_wan.signal_dbm_to_percent(-30), 100)
        self.assertEqual(wifi_wan.signal_dbm_to_percent(-100), 0)
        self.assertEqual(wifi_wan.signal_dbm_to_percent(-120), 0)
        self.assertEqual(wifi_wan.signal_dbm_to_percent(-75), 50)
        self.assertIsNone(wifi_wan.signal_dbm_to_percent(None))


class GetStateTest(WifiWanHarness):
    def test_unconfigured_state_is_all_defaults(self):
        st = wifi_wan.get_state()
        self.assertFalse(st["configured"])
        self.assertFalse(st["connected"])
        self.assertEqual(st["ssid"], "")

    def test_disabled_sta_reports_config_only(self):
        """The settings page shows the remembered network even while the
        relay is off; no live probing may happen for it."""
        self.configure_sta(ssid="home", enabled=False)
        st = wifi_wan.get_state()
        self.assertTrue(st["configured"])
        self.assertFalse(st["sta_enabled"])
        self.assertEqual(st["ssid"], "home")
        self.assertEqual(st["band"], "2g")
        self.assertEqual(self.ubus_log, [], "disabled STA must not probe ubus")

    def test_eap_is_reported_as_eap_not_wpa2(self):
        """UCI stores enterprise as encryption=wpa2 + eap_type; the UI must
        see 'eap' or the edit dialog opens with the wrong security type."""
        self.configure_sta(ssid="eduroam", enabled=False,
                           encryption="wpa2", eap_type="peap")
        self.assertEqual(wifi_wan.get_state()["encryption"], "eap")

    def test_random_mac_recognized_only_as_the_derived_value(self):
        """A pinned per-radio MAC is 'macaddr set' too — only the exact
        per-SSID derived address may light the 'private MAC' toggle."""
        self.configure_sta(ssid="home", enabled=False,
                           macaddr=wifi_wan.stable_mac_for_ssid("home"))
        self.assertTrue(wifi_wan.get_state()["random_mac"])
        self.uci.values[STA + ".macaddr"] = "02:11:22:33:44:55"
        self.assertFalse(wifi_wan.get_state()["random_mac"])

    def test_custom_mac_reported_when_neither_derived_nor_pinned(self):
        """The edit dialog must show the address the user typed; the pinned
        default and the derived private MAC must not masquerade as custom."""
        self.configure_sta(ssid="home", enabled=False,
                           macaddr="dc:56:7b:01:02:03")
        st = wifi_wan.get_state()
        self.assertEqual(st["custom_mac"], "dc:56:7b:01:02:03")
        self.assertFalse(st["random_mac"])
        # the pinned per-radio address is the default, not a custom choice
        self.uci.values[STA + ".macaddr"] = "02:11:22:33:44:55"
        self.assertEqual(wifi_wan.get_state()["custom_mac"], "")

    def test_live_state_with_portal_verdict(self):
        """Associated with an IP but behind a portal: connected=True,
        internet=False, captive_portal=True — that is what makes the
        dashboard show 'sign-in required' instead of a green tick."""
        self.configure_sta(ssid="hotel", enabled=True)
        self.wire_sta_netdev(info={"ssid": "hotel", "signal": -58,
                                   "bitrate": 300000})
        self.ubus[("network.interface.wan_wifi", "status")] = {
            "up": True, "uptime": 321, "l3_device": "phy0-sta0",
            "ipv4-address": [{"address": "10.20.30.40"}]}
        self.ubus[("network.interface.wan_wifi_v6", "status")] = {
            "ipv6-address": [{"address": "fd00::2"}]}
        wifi_wan._conn_cache["result"] = "portal"
        st = wifi_wan.get_state()
        self.assertTrue(st["associated"])
        self.assertTrue(st["connected"])
        self.assertEqual(st["ip"], "10.20.30.40")
        self.assertEqual(st["ip6"], "fd00::2")
        self.assertEqual(st["signal"], -58)
        self.assertEqual(st["signal_percent"], 84)
        self.assertEqual(st["bitrate"], 300000)
        self.assertEqual(st["uptime"], 321)
        self.assertFalse(st["internet"])
        self.assertTrue(st["captive_portal"])

    def test_zero_dbm_iwinfo_falls_back_to_iw(self):
        """The AIC8800 reports 0 dBm via iwinfo; the true value must come
        from `iw dev link` instead of showing a full-bars lie."""
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(info={"ssid": "home", "signal": 0})
        self.ubus[("network.interface.wan_wifi", "status")] = {
            "up": True, "l3_device": "phy0-sta0",
            "ipv4-address": [{"address": "10.0.0.5"}]}
        self.subproc.run_handler = lambda cmd: (
            FakeProc("\tsignal: -61 dBm\n") if cmd[:1] == ["iw"] else None)
        st = wifi_wan.get_state()
        self.assertEqual(st["signal"], -61)


# ── EAP capability probing ───────────────────────────────────────────────────

class EapSupportedTest(WifiWanHarness):
    def test_full_wpad_is_detected_and_cached(self):
        self.fos.existing.add("/usr/sbin/wpad")
        self.subproc.run_handler = lambda cmd: FakeProc(returncode=0)
        self.assertTrue(wifi_wan.eap_supported())
        n = len(self.subproc.calls)
        self.assertTrue(wifi_wan.eap_supported())
        self.assertEqual(len(self.subproc.calls), n, "result must be cached")

    def test_mini_wpad_or_no_binary_means_no_eap(self):
        """Offering EAP on a wpad-mini build would let the user save a
        network that can never associate."""
        self.assertFalse(wifi_wan.eap_supported())
        wifi_wan._eap_supported_cache = None
        self.fos.existing.add("/usr/sbin/wpa_supplicant")
        self.subproc.run_handler = lambda cmd: FakeProc(returncode=1)
        self.assertFalse(wifi_wan.eap_supported())

    def test_probe_crash_reads_as_unsupported_not_a_crash(self):
        self.fos.existing.add("/usr/sbin/wpa_supplicant")

        def boom(cmd):
            raise RuntimeError("grep missing")
        self.subproc.run_handler = boom
        self.assertFalse(wifi_wan.eap_supported())


# ── roaming ──────────────────────────────────────────────────────────────────

class RoamCheckTest(WifiWanHarness):
    SAVED = [{"ssid": "home", "key": "pw1", "encryption": "psk2", "band": "5g"},
             {"ssid": "office", "key": "pw2", "encryption": "psk2", "band": "5g"}]

    def setUp(self):
        super(RoamCheckTest, self).setUp()
        self.applied = []
        self._patch(wifi_wan, "apply_config",
                    lambda *a, **kw: self.applied.append((a, kw)) or True)
        self.scan_result = ([], None)
        self._patch(wifi_wan, "scan", lambda radio=None: self.scan_result)
        self.cur_signal = -60
        self._patch(wifi_wan, "_current_link_signal",
                    lambda radio: self.cur_signal)

    def _rival(self, ssid="office", signal=-40, band="5g", channel=44):
        return {"ssid": ssid, "band": band, "channel": channel,
                "signal": signal, "encryption": "psk2"}

    def test_rate_limited_between_scans(self):
        """Roam scans are power-hungry; inside the interval no scan may fire."""
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 30
        self.scan_result = ([self._rival(signal=-10)], None)
        self.assertIsNone(
            wifi_wan._roam_check("radio0", self.SAVED, "home", True))
        self.assertEqual(self.applied, [])

    def test_comparable_rival_does_not_cause_a_switch(self):
        """Hysteresis: two APs a few dB apart must not ping-pong."""
        self.scan_result = ([self._rival(signal=-55)], None)  # 5 dB better
        self.assertIsNone(
            wifi_wan._roam_check("radio0", self.SAVED, "home", True))
        self.assertEqual(self.applied, [])
        # and the next scan is pushed out a full interval
        self.assertEqual(wifi_wan._roam_scan["next_ok"],
                         self.ftime.now + wifi_wan._ROAM_SCAN_INTERVAL)

    def test_clearly_stronger_saved_ssid_wins(self):
        """Arriving at a new site: the rival beats the margin, so the keeper
        jumps — this is the 'still clinging to the old weak AP' fix."""
        self.scan_result = ([self._rival(signal=-40)], None)  # 20 dB better
        self.assertEqual(
            wifi_wan._roam_check("radio0", self.SAVED, "home", True), "office")
        (args, kw) = self.applied[0]
        self.assertEqual(args[0], "office")
        self.assertEqual(args[3], "5g")
        self.assertEqual(args[4], 44)

    def test_dead_link_yields_to_any_saved_network(self):
        """A portal-locked/offline link drops the margin: a working
        alternative beats a dead one even at equal signal."""
        self.scan_result = ([self._rival(signal=-60)], None)  # equal signal
        self.assertEqual(
            wifi_wan._roam_check("radio0", self.SAVED, "home",
                                 current_online=False), "office")

    def test_unknown_current_signal_allows_the_switch(self):
        self.cur_signal = None
        self.scan_result = ([self._rival(signal=-80)], None)
        self.assertEqual(
            wifi_wan._roam_check("radio0", self.SAVED, "home", True), "office")

    def test_no_rivals_or_failed_scan_stays_put(self):
        self.scan_result = ([], "radio_disabled")
        self.assertIsNone(
            wifi_wan._roam_check("radio0", self.SAVED, "home", True))
        wifi_wan._roam_scan["next_ok"] = 0.0
        # only the current ssid in range -> nothing to jump to
        self.scan_result = ([self._rival(ssid="home")], None)
        self.assertIsNone(
            wifi_wan._roam_check("radio0", self.SAVED, "home", True))

    def test_strongest_by_ssid_keeps_the_strongest_entry(self):
        nets = [{"ssid": "a", "signal": -40}, {"ssid": "b", "signal": -50},
                {"ssid": "a", "signal": -70}]
        self.assertEqual(wifi_wan._strongest_by_ssid(nets)["a"]["signal"], -40)


class CurrentLinkTest(WifiWanHarness):
    def test_current_link_signal_comes_from_iwinfo(self):
        self.wire_sta_netdev(info={"ssid": "home", "signal": -47})
        self.assertEqual(wifi_wan._current_link_signal("radio0"), -47)

    def test_zero_dbm_falls_back_to_iw_and_no_netdev_is_none(self):
        """Roam decisions compare dBm; the AIC8800's 0-dBm iwinfo lie would
        make the current link look infinitely strong and block every roam."""
        self.wire_sta_netdev(info={"ssid": "home", "signal": 0})
        self.subproc.run_handler = lambda cmd: (
            FakeProc("\tsignal: -63 dBm\n") if cmd[:1] == ["iw"] else None)
        self.assertEqual(wifi_wan._current_link_signal("radio0"), -63)
        self.ubus[("network.wireless", "status")] = {}
        self.ubus[("iwinfo", "devices")] = {"devices": []}
        self.assertIsNone(wifi_wan._current_link_signal("radio0"))

    def test_is_associated_false_without_a_sta_netdev(self):
        self.ubus[("network.wireless", "status")] = {}
        self.assertFalse(wifi_wan._is_associated("radio0"))

    def test_mac_pin_asserter_leaves_unset_when_no_phy(self):
        """With no live phy there is no pin to write; the section must not be
        committed with a bogus MAC."""
        self.configure_sta(ssid="home", enabled=True)
        del self.fos.links["/sys/class/ieee80211/phy0"]
        wifi_wan._assert_sta_mac_pinned()
        self.assertNotIn(STA + ".macaddr", self.uci.values)
        self.assertEqual(self.commits, [])


# ── reconcile ────────────────────────────────────────────────────────────────

class ReconcileTest(WifiWanHarness):
    def setUp(self):
        super(ReconcileTest, self).setUp()
        self.cfg.networks = [
            {"ssid": "home", "key": "pw1", "encryption": "psk2", "band": "5g"}]
        self.applied = []

    def _stub_apply(self):
        self._patch(wifi_wan, "apply_config",
                    lambda *a, **kw: self.applied.append((a, kw)) or True)

    def _stub_scan(self, networks, err=None):
        self._patch(wifi_wan, "scan", lambda radio=None: (networks, err))

    def test_inactive_modes_are_no_ops(self):
        self.cfg.network_mode = "auto"
        self.assertEqual(wifi_wan.reconcile(), "not_priority")
        self.cfg.network_mode = "priority"
        self.wifi_slot_enabled = False
        self.assertEqual(wifi_wan.reconcile(), "wifi_slot_disabled")

    def test_config_layer_failure_is_contained(self):
        def boom():
            raise RuntimeError("config store gone")
        self.cfg.get_network_mode = boom
        self.assertEqual(wifi_wan.reconcile(), "config_error")

    def test_radio_deficit_parks_only_after_it_persists(self):
        """One visible radio right after boot is usually drivers still
        settling; the relay must be delayed, not silently disabled. A real
        removal still parks within a few ticks."""
        self.configure_sta(ssid="home", enabled=True)
        self.radios_ready = False
        self.assertEqual(wifi_wan.reconcile(), "need_two_radios")
        self.assertEqual(wifi_wan.reconcile(), "need_two_radios")
        self.assertTrue(wifi_wan.is_sta_enabled(),
                        "parked too early — a slow boot would kill the relay")
        self.assertEqual(wifi_wan.reconcile(), "sta_parked_need_two_radios")
        self.assertFalse(wifi_wan.is_sta_enabled())

    def test_radio_deficit_counter_resets_when_radios_return(self):
        self.configure_sta(ssid="home", enabled=True)
        self.radios_ready = False
        wifi_wan.reconcile()
        wifi_wan.reconcile()
        self.radios_ready = True
        self.wire_sta_netdev(associated=True)
        wifi_wan.reconcile()
        self.assertEqual(wifi_wan._radio_deficit["ticks"], 0)
        self.radios_ready = False
        self.assertEqual(wifi_wan.reconcile(), "need_two_radios",
                         "stale deficit ticks must not park on the first tick")

    def test_parked_sta_re_engages_when_the_second_radio_returns(self):
        """Bench repro 2026-09-03: a PCI remove/rescan of the ath12k card
        (what the firmware-wedge watchdog does; a slow re-probe looks the
        same) takes the PCIe phy away for over a minute and brings it back
        renumbered. The keeper parks the STA meanwhile. Once both radios are
        back it must re-enable the STA directly: the scan path from an
        AP-mode vif came back empty and its backoff then held the rejoin for
        another 90s, so the upstream stayed on cellular ~2 min longer than
        the card was actually gone."""
        self.configure_sta(ssid="home", enabled=True)
        self.radios_ready = False
        result = None
        for _ in range(wifi_wan._RADIO_DEFICIT_PARK_TICKS):
            result = wifi_wan.reconcile()
        self.assertEqual(result, "sta_parked_need_two_radios")
        self.assertFalse(wifi_wan.is_sta_enabled())
        self.assertTrue(wifi_wan._radio_deficit["parked"])

        self.radios_ready = True
        self.assertEqual(wifi_wan.reconcile(), "sta_unparked_radios_back")
        self.assertTrue(wifi_wan.is_sta_enabled(),
                        "keeper-parked STA must come back with the radio")
        self.assertFalse(wifi_wan._radio_deficit["parked"])
        self.assertEqual(wifi_wan._radio_deficit["ticks"], 0)

    def test_user_disabled_sta_is_not_revived_by_a_radio_flap(self):
        """Only a keeper park is undone by the radio's return. A STA the
        user (or wan_manager) disabled stays disabled across a flap."""
        self.configure_sta(ssid="home", enabled=False)
        self.radios_ready = False
        for _ in range(wifi_wan._RADIO_DEFICIT_PARK_TICKS + 1):
            self.assertEqual(wifi_wan.reconcile(), "need_two_radios")
        self.assertFalse(wifi_wan._radio_deficit["parked"])
        self.radios_ready = True
        self.assertNotEqual(wifi_wan.reconcile(), "sta_unparked_radios_back")
        self.assertFalse(wifi_wan.is_sta_enabled())

    def test_no_saved_networks_is_a_no_op(self):
        self.cfg.networks = []
        self.assertEqual(wifi_wan.reconcile(), "no_saved_networks")

    def test_missing_sta_config_is_rebuilt_from_the_saved_record(self):
        """The reboot-after-dead-commit case: slot enabled, network saved,
        but no STA section. Scanning can't fix it (no netdev); the config
        must be rebuilt from the remembered network."""
        self.cfg.keys["wifi_wan_last_ssid"] = "home"
        self._stub_apply()
        self.assertEqual(wifi_wan.reconcile(), "rebuilt:home")
        self.assertEqual(self.applied[0][0][0], "home")

    def test_single_saved_network_rebuilds_without_a_last_record(self):
        self._stub_apply()
        self.assertEqual(wifi_wan.reconcile(), "rebuilt:home")

    def test_rebuild_carries_the_saved_custom_mac(self):
        # A network joined with a custom MAC (the address the upstream
        # authorized) must come back with it after the config rebuild, or the
        # rejoin knocks on the door with the wrong face.
        self.cfg.networks = [
            {"ssid": "home", "key": "pw1", "encryption": "psk2", "band": "5g",
             "custom_mac": "dc:56:7b:aa:bb:01"}]
        self._stub_apply()
        self.assertEqual(wifi_wan.reconcile(), "rebuilt:home")
        _args, kwargs = self.applied[0]
        self.assertEqual(kwargs.get("custom_mac"), "dc:56:7b:aa:bb:01")

    def test_many_saved_and_no_last_record_falls_through_to_scan(self):
        """With several candidates and no memory of the last one, guessing
        would be wrong half the time — scan and take the strongest."""
        self.cfg.networks = [
            {"ssid": "home", "key": "p", "encryption": "psk2"},
            {"ssid": "office", "key": "q", "encryption": "psk2"}]
        self._stub_apply()
        self._stub_scan([
            {"ssid": "office", "band": "5g", "channel": 40, "signal": -50},
            {"ssid": "home", "band": "2g", "channel": 3, "signal": -70}])
        self.assertEqual(wifi_wan.reconcile(), "rejoined:office")

    def test_associated_and_online_stays_put(self):
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60  # no roam scan
        wifi_wan._conn_cache.update(result="online", time=self.ftime.now - 10)
        self.assertEqual(wifi_wan.reconcile(), "ok_online")
        self.assertFalse(self._ran("curl"),
                         "fresh online verdict must skip the probe (battery)")
        self.assertEqual(self.uci.values["network.wan_wifi.metric"], "20")

    def test_stale_online_verdict_is_reverified(self):
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        wifi_wan._conn_cache.update(result="online",
                                    time=self.ftime.now - 120)  # stale
        self.curl_script({"https://": "203.0.113.9\n200"})
        self.assertEqual(wifi_wan.reconcile(), "ok_online")
        self.assertTrue(self._ran("curl"), "stale verdict must be re-probed")

    def test_dead_uplink_is_demoted_but_kept_up(self):
        """Associated but nothing answers: egress must fail over (demoted
        metric) while the link stays up so a portal login page would still
        be reachable."""
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        wifi_wan._ensure_network_ifaces()
        self.curl_script({})  # every curl fails
        # First failed round from the fresh-join 'unknown' is held (it races
        # DHCP settling); the sustained failure demotes on the next tick.
        self.assertEqual(wifi_wan.reconcile(), "probe_unconfirmed")
        self.assertEqual(wifi_wan.reconcile(), "demoted_offline")
        self.assertEqual(self.uci.values["network.wan_wifi.metric"], "1020")
        self.assertEqual(self.uci.values["network.wan_wifi_v6.metric"], "1021")

    def test_portal_runs_the_login_hook_then_recovers(self):
        """A scripted portal login clears the block without a roam or a
        failover: the hook runs, the re-probe sees the internet, done."""
        self.cfg.networks = [{"ssid": "home", "key": "pw1",
                              "encryption": "psk2", "login_cmd": "do-login"}]
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        state = {"logged_in": False}

        def handler(cmd):
            if cmd[0] == "sh":
                state["logged_in"] = True
                return FakeProc()
            if cmd[0] == "curl":
                if state["logged_in"] and "https://" in cmd[-1]:
                    return FakeProc("203.0.113.9\n200")
                return FakeProc("" if "https://" in cmd[-1] else "\n302")
            return FakeProc()
        self.subproc.run_handler = handler
        self.assertEqual(wifi_wan.reconcile(), "ok_online_after_login")
        self.assertTrue(state["logged_in"])

    def test_roam_result_is_surfaced(self):
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._conn_cache.update(result="online", time=self.ftime.now)
        self._patch(wifi_wan, "_roam_check",
                    lambda radio, saved, cur, current_online: "office")
        self.assertEqual(wifi_wan.reconcile(), "roamed:office")

    def test_association_resets_scan_backoff_and_vif_watchdog(self):
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        wifi_wan._conn_cache.update(result="online", time=self.ftime.now)
        wifi_wan._scan_backoff.update(fails=4, next_ok=self.ftime.now + 60)
        wifi_wan._vif_rescue.update(missing_since=1.0, fails=2, next_ok=99.0)
        wifi_wan.reconcile()
        self.assertEqual(wifi_wan._scan_backoff["fails"], 0)
        self.assertEqual(wifi_wan._vif_rescue["fails"], 0)

    def test_scan_backoff_defers_the_scan(self):
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=False)
        wifi_wan._scan_backoff["next_ok"] = self.ftime.now + 30
        self.assertEqual(wifi_wan.reconcile(), "scan_backoff")

    def test_failed_scan_backs_off(self):
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=False)
        self._stub_scan([], "radio_disabled")
        self.assertEqual(wifi_wan.reconcile(), "scan_failed:radio_disabled")
        self.assertEqual(wifi_wan._scan_backoff["fails"], 1)
        self.assertGreater(wifi_wan._scan_backoff["next_ok"], self.ftime.now)

    def test_away_from_every_saved_network_backs_off(self):
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=False)
        self._stub_scan([{"ssid": "stranger", "band": "2g", "channel": 1,
                          "signal": -50}])
        self.assertEqual(wifi_wan.reconcile(), "no_saved_in_range")
        self.assertEqual(wifi_wan._scan_backoff["fails"], 1)

    def test_rejoins_the_strongest_saved_network_in_range(self):
        """Disassociated with two saved networks visible: the stronger one is
        joined with the band/channel the scan actually saw."""
        self.cfg.networks = [
            {"ssid": "home", "key": "p1", "encryption": "psk2"},
            {"ssid": "office", "key": "p2", "encryption": "psk2"}]
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=False)
        self._stub_apply()
        self._stub_scan([
            {"ssid": "home", "band": "5g", "channel": 149, "signal": -44},
            {"ssid": "office", "band": "2g", "channel": 6, "signal": -71}])
        wifi_wan._scan_backoff.update(fails=2, next_ok=0.0)
        self.assertEqual(wifi_wan.reconcile(), "rejoined:home")
        args, kw = self.applied[0]
        self.assertEqual(args[:2], ("home", "p1"))
        self.assertEqual(args[3], "5g")
        self.assertEqual(args[4], 149)
        self.assertEqual(wifi_wan._scan_backoff["fails"], 0,
                         "a successful join must reset the backoff")

    def test_missing_vif_waits_out_the_grace_window(self):
        """UCI says enabled but the netdev is gone: within the grace window
        (boot transients) nothing is reloaded."""
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=False, netdev_exists=False)
        self.assertEqual(wifi_wan.reconcile(), "sta_vif_missing")
        self.assertFalse(self._ran("/sbin/wifi"))

    def test_missing_vif_is_rescued_after_grace_with_backoff(self):
        """Past the grace window the keeper does what the manual reconnect
        did — reload wifi — and backs off exponentially so a wedged driver
        can't cause a reload storm."""
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=False, netdev_exists=False)
        self.assertEqual(wifi_wan.reconcile(), "sta_vif_missing")
        self.ftime.now += wifi_wan._VIF_MISSING_GRACE + 1
        self.assertEqual(wifi_wan.reconcile(), "sta_vif_rescue_reload")
        self.assertTrue(self._ran("/sbin/wifi", "reload"))
        self.assertTrue(self._ran("ifup", "wan_wifi"))
        # immediately after: inside the retry backoff
        self.assertEqual(wifi_wan.reconcile(), "sta_vif_rescue_wait")
        # after the first backoff expires a second rescue runs
        self.ftime.now += wifi_wan._VIF_RESCUE_BACKOFF_START + 1
        self.assertEqual(wifi_wan.reconcile(), "sta_vif_rescue_reload")
        self.assertEqual(wifi_wan._vif_rescue["fails"], 2)

    def test_unpinned_sta_mac_is_pinned_during_reconcile(self):
        """Upgrade path: a pre-pinning STA section gets macaddr written once
        so its DHCP lease stops churning with vif order."""
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        wifi_wan._conn_cache.update(result="online", time=self.ftime.now)
        self.assertNotIn(STA + ".macaddr", self.uci.values)
        wifi_wan.reconcile()
        self.assertEqual(self.uci.values[STA + ".macaddr"],
                         "02:11:22:33:44:55")
        # and it is not rewritten every tick
        self.commits[:] = []
        wifi_wan.reconcile()
        self.assertNotIn("wireless", self.commits)


# ── subprocess wrappers never raise ──────────────────────────────────────────


class PortalAccessPinTest(WifiWanHarness):
    """Demoting a portal-locked uplink is not enough — the login page must
    stay REACHABLE through it: the portal hostname only resolves via the
    hotspot's DNS (dnsmasq all_servers lets the carrier's NXDOMAIN win the
    race, and rebind protection eats the private-IP answer), and an
    off-subnet portal address would route out over cellular. The pin fixes
    both; these tests cover its lifecycle around reconcile()."""

    PORTAL = "http://gcppty.gw/login?dst=http%3A%2F%2Fbbb.com%2F"
    CONF = "/tmp/dnsmasq.d/pcat_portal_pin.conf"

    def _portal_tick(self):
        self.cfg.networks = [{"ssid": "home", "key": "pw1",
                              "encryption": "psk2"}]
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        wifi_wan._ensure_network_ifaces()
        self.ubus[("network.interface.wan_wifi", "status")] = {
            "up": True, "l3_device": "phy0-sta0",
            "ipv4-address": [{"address": "10.20.30.40", "mask": 24}],
            "route": [{"target": "0.0.0.0", "nexthop": "10.20.30.1"}],
            "dns-server": ["10.20.30.1"],
        }
        self.curl_script({"https://": "", "http://": "\n302\t" + self.PORTAL})
        # The portal resolves off-subnet (separate gateway box) => needs a
        # route pin; the chain ends there (no further redirect hop).
        self._patch(wifi_wan, "_dns_query_a",
                    lambda host, server, dev, timeout=2: ["10.99.0.5"])
        self._patch(wifi_wan, "_next_hop", lambda *a, **kw: "")
        return wifi_wan.reconcile()

    def test_portal_pins_dns_and_route_while_demoting(self):
        self.assertEqual(self._portal_tick(), "demoted_portal")
        # Egress failed over…
        self.assertEqual(self.uci.values["network.wan_wifi.metric"], "1020")
        # …but the portal hostname is scoped to the hotspot's resolver and
        # rebind-exempted so clients can still resolve it through us…
        self.assertEqual(self.files[self.CONF],
                         "server=/gcppty.gw/10.20.30.1\n"
                         "rebind-domain-ok=/gcppty.gw/\n")
        self.assertTrue(self._ran("/etc/init.d/dnsmasq", "restart"))
        # …and its off-subnet address is steered through the WiFi.
        self.assertTrue(self._ran("ip", "route", "replace", "10.99.0.5/32",
                                  "via", "10.20.30.1", "dev", "phy0-sta0"))
        self.assertEqual(wifi_wan._portal_seen["url"], self.PORTAL)

    def test_second_portal_tick_does_not_churn_dnsmasq(self):
        self._portal_tick()
        restarts = len(self._ran("/etc/init.d/dnsmasq", "restart"))
        self.assertEqual(wifi_wan.reconcile(), "demoted_portal")
        # Same portal => no re-walk, no second restart; the /32 is simply
        # re-asserted (an ifup/DHCP renew silently flushes it).
        self.assertEqual(
            len(self._ran("/etc/init.d/dnsmasq", "restart")), restarts)
        self.assertGreaterEqual(
            len(self._ran("ip", "route", "replace", "10.99.0.5/32")), 2)

    def test_login_success_clears_the_pin(self):
        self._portal_tick()
        self.curl_script({"https://": "203.0.113.9\n200\t"})
        self.assertEqual(wifi_wan.reconcile(), "ok_online")
        self.assertNotIn(self.CONF, self.fos.existing)
        self.assertTrue(self._ran("ip", "route", "del", "10.99.0.5/32"))
        self.assertEqual(self.uci.values["network.wan_wifi.metric"], "20")
        self.assertEqual(wifi_wan._portal_seen["url"], "")

    def test_unresolvable_portal_still_pins_dns_and_retries(self):
        self.cfg.networks = [{"ssid": "home", "key": "pw1",
                              "encryption": "psk2"}]
        self.configure_sta(ssid="home", enabled=True)
        self.wire_sta_netdev(associated=True)
        wifi_wan._roam_scan["next_ok"] = self.ftime.now + 60
        wifi_wan._ensure_network_ifaces()
        self.ubus[("network.interface.wan_wifi", "status")] = {
            "up": True, "l3_device": "phy0-sta0",
            "ipv4-address": [{"address": "10.20.30.40", "mask": 24}],
            "route": [{"target": "0.0.0.0", "nexthop": "10.20.30.1"}],
            "dns-server": ["10.20.30.1"],
        }
        self.curl_script({"https://": "", "http://": "\n302\t" + self.PORTAL})
        self._patch(wifi_wan, "_dns_query_a",
                    lambda host, server, dev, timeout=2: [])
        self.assertEqual(wifi_wan.reconcile(), "demoted_portal")
        # The DNS scope is still worth installing (dnsmasq keeps retrying
        # where our one-shot query may have raced DHCP settling)…
        self.assertIn(self.CONF, self.files)
        # …but with no address there is no route to pin, and nothing is
        # cached: the next tick retries the whole walk.
        self.assertFalse(self._ran("ip", "route", "replace"))
        self.assertEqual(wifi_wan._portal_pin["url"], "")


class SubprocessSafetyTest(WifiWanHarness):
    def test_wifi_reload_and_if_updown_swallow_failures(self):
        """A missing binary or a hung netifd must degrade to a log line, not
        take the reconcile thread down."""
        def boom(cmd):
            raise RuntimeError("exec failed")
        self.subproc.run_handler = boom
        wifi_wan._reload_wifi()
        wifi_wan._ifup("wan_wifi")
        wifi_wan._ifdown("wan_wifi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
