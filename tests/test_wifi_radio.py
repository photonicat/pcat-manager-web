#!/usr/bin/env python3
"""Unit tests for the boot-time radio reconcile (pcat_util/wifi_radio).

Pins the wireless-config parsing (which sections are radios, which vifs are
theirs, what counts as enabled) and, more importantly, the intent state
machine: a radio the firmware left switched off must come on, and a radio
someone deliberately switched off must not.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_wifi_radio`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

# pcat_logger opens its log file at import time, which fails in sandboxed test
# runs -- mock it before anything imports it, same idiom as test_lxc_manager.
_logger_patch = patch.dict('sys.modules', {
    'pcat_logger': MagicMock(),
    'pcat_util.pcat_logger': MagicMock(),
})
_logger_patch.start()

from pcat_util import wifi_radio as wr


# A two-radio box: onboard 2.4 GHz up, PCIe 5 GHz present but disabled --
# exactly the state a freshly flashed unit is reported to come up in.
PCIE_DISABLED = {
    "wireless.radio0": "wifi-device",
    "wireless.radio0.path": "platform/soc/wifi0",
    "wireless.default_radio0": "wifi-iface",
    "wireless.default_radio0.device": "radio0",
    "wireless.default_radio0.mode": "ap",
    "wireless.default_radio0.ssid": "photonicat2-4a99abb9",
    "wireless.radio1": "wifi-device",
    "wireless.radio1.path": "soc/22000000.pcie/pci0000:00/0000:00:01.0",
    "wireless.radio1.disabled": "1",
    "wireless.default_radio1": "wifi-iface",
    "wireless.default_radio1.device": "radio1",
    "wireless.default_radio1.mode": "ap",
    "wireless.default_radio1.ssid": "photonicat2-4a99abb9-5g",
    "wireless.default_radio1.disabled": "1",
}


def both_enabled():
    d = dict(PCIE_DISABLED)
    del d["wireless.radio1.disabled"]
    del d["wireless.default_radio1.disabled"]
    return d


class ConfigParsingTests(unittest.TestCase):

    def test_wifi_devices_lists_radios_only(self):
        self.assertEqual(wr.wifi_devices(PCIE_DISABLED), ["radio0", "radio1"])

    def test_ap_vifs_are_matched_by_device(self):
        self.assertEqual(wr.ap_vifs(PCIE_DISABLED, "radio1"),
                         ["default_radio1"])

    def test_sta_relay_vif_is_never_claimed(self):
        cfg = dict(PCIE_DISABLED)
        cfg["wireless.wifinet_sta"] = "wifi-iface"
        cfg["wireless.wifinet_sta.device"] = "radio0"
        cfg["wireless.wifinet_sta.mode"] = "sta"
        self.assertEqual(wr.ap_vifs(cfg, "radio0"), ["default_radio0"])

    def test_absent_disabled_option_means_enabled(self):
        cfg = both_enabled()
        self.assertTrue(wr.is_enabled(cfg, "radio1", ["default_radio1"]))

    def test_disabled_radio_or_disabled_vif_both_count_as_off(self):
        self.assertFalse(
            wr.is_enabled(PCIE_DISABLED, "radio1", ["default_radio1"]))
        vif_only = both_enabled()
        vif_only["wireless.default_radio1.disabled"] = "1"
        self.assertFalse(
            wr.is_enabled(vif_only, "radio1", ["default_radio1"]))

    def test_radio_without_path_is_assumed_present(self):
        self.assertTrue(wr.radio_present({}, "radio1"))


class _ReconcileCase(unittest.TestCase):
    """Drives startup() against an in-memory wireless config."""

    def setUp(self):
        self.saved = {}
        self.enabled = []
        self.reloaded = []
        self.detect_calls = []
        self.sets = []
        self.commits = []

        patches = [
            patch.object(wr, 'save_intents', self._save),
            patch.object(wr, 'reload_wifi',
                         lambda: self.reloaded.append(True)),
            patch.object(wr, 'radio_present', lambda cfg, radio: True),
            patch.object(wr, 'detect_new_radios', self._detect),
            patch.object(wr, 'uci_set',
                         lambda kv: self.sets.append(kv) or True),
            patch.object(wr, 'uci_commit',
                         lambda cfg: self.commits.append(cfg) or True),
            # Reaching into wifi_wan (and through it pcat_config) is not what
            # most of these tests are about; the ones that care patch it back.
            patch.object(wr, '_relay_ap_backup', lambda radio: None),
            patch.object(wr, '_relay_radio_backup', lambda radio: None),
            patch.object(wr, 'active_relay_radio', lambda: None),
            # Same for the remembered-hotspot store: it lives in the real
            # pcat_config, so left unpatched these tests would leak learned
            # identities into each other through it (and onto the dev box's
            # user config). Its own behaviour is pinned by
            # test_wifi_ap_persist, which fakes the store per test.
            patch.object(wr, 'get_ap_settings', lambda: {}),
            patch.object(wr, 'save_ap_settings', lambda store: True),
            patch.object(wr, 'factory_ssid_stem', lambda: ""),
            # Keep the boot-time wait out of the suite's wall clock; its own
            # behaviour is pinned by WirelessReadyWaitTests.
            patch.object(wr, 'WIRELESS_WAIT_SECONDS', 0),
            # No background re-check thread escaping into the suite; it is
            # covered directly by RecheckTests.
            patch.object(wr, 'RECHECK_SCHEDULE', ()),
            patch.object(wr, 'declared_radio_count', lambda: 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _save(self, intents):
        self.saved = dict(intents)
        return True

    def _detect(self):
        self.detect_calls.append(True)
        return False

    def run_startup(self, config, intents, phys=2, enable_ok=True):
        def _enable(radio, vifs):
            if not enable_ok:
                return False
            self.enabled.append(radio)
            return True

        with patch.object(wr, 'uci_show_dict', lambda *a, **k: config), \
             patch.object(wr, 'get_intents', lambda: dict(intents)), \
             patch.object(wr, 'phy_count', lambda: phys), \
             patch.object(wr, 'enable_radio', _enable):
            return wr.startup()


class ReconcileTests(_ReconcileCase):

    def test_fresh_flash_enables_the_disabled_pcie_radio(self):
        note = self.run_startup(PCIE_DISABLED, {})
        self.assertEqual(self.enabled, ["radio1"])
        self.assertIn("enabled radio1", note)
        self.assertEqual(self.saved, {"radio0": True, "radio1": True})
        self.assertTrue(self.reloaded)

    def test_radio_the_user_turned_off_here_stays_off(self):
        note = self.run_startup(PCIE_DISABLED,
                                {"radio0": True, "radio1": False})
        self.assertEqual(self.enabled, [])
        self.assertIsNone(note)
        self.assertFalse(self.reloaded)

    def test_off_switch_outside_this_ui_is_respected_from_then_on(self):
        # We enabled it on an earlier boot (intent True) and it is off again:
        # a real choice made elsewhere. Record it and never re-enable.
        note = self.run_startup(PCIE_DISABLED,
                                {"radio0": True, "radio1": True})
        self.assertEqual(self.enabled, [])
        self.assertIs(self.saved["radio1"], False)
        self.assertIn("switched off elsewhere", note)

        # Second boot in that state: still off, still untouched.
        self.enabled = []
        note = self.run_startup(PCIE_DISABLED, self.saved)
        self.assertEqual(self.enabled, [])
        self.assertIsNone(note)

    def test_failed_enable_leaves_intent_unknown_so_it_retries(self):
        note = self.run_startup(PCIE_DISABLED, {}, enable_ok=False)
        self.assertNotIn("radio1", self.saved)
        self.assertIn("enable failed", note)
        self.assertFalse(self.reloaded)

    def test_healthy_boot_is_a_no_op(self):
        note = self.run_startup(both_enabled(), {"radio0": True, "radio1": True})
        self.assertEqual(self.enabled, [])
        self.assertIsNone(note)
        self.assertFalse(self.reloaded)

    def test_enabled_radio_seeds_its_intent(self):
        note = self.run_startup(both_enabled(), {})
        self.assertEqual(self.saved, {"radio0": True, "radio1": True})
        self.assertIsNone(note)

    def test_unconfigured_phy_triggers_detection(self):
        one_radio = {k: v for k, v in both_enabled().items()
                     if not k.startswith("wireless.radio1")
                     and not k.startswith("wireless.default_radio1")}
        self.run_startup(one_radio, {}, phys=2)
        self.assertTrue(self.detect_calls)

    def test_detection_is_skipped_when_every_phy_has_a_section(self):
        self.run_startup(both_enabled(), {}, phys=2)
        self.assertFalse(self.detect_calls)

    def test_radio_with_no_ap_vif_is_left_to_wifi_wan(self):
        sta_only = {
            "wireless.radio1": "wifi-device",
            "wireless.radio1.disabled": "1",
            "wireless.wifinet_sta": "wifi-iface",
            "wireless.wifinet_sta.device": "radio1",
            "wireless.wifinet_sta.mode": "sta",
        }
        note = self.run_startup(sta_only, {}, phys=1)
        self.assertEqual(self.enabled, [])
        self.assertIsNone(note)

    def test_unreadable_uci_does_nothing(self):
        self.assertIsNone(self.run_startup({}, {}, phys=2))
        self.assertEqual(self.enabled, [])

    def test_missing_hardware_is_skipped(self):
        with patch.object(wr, 'radio_present',
                          lambda cfg, radio: radio != "radio1"):
            note = self.run_startup(PCIE_DISABLED, {})
        self.assertEqual(self.enabled, [])
        self.assertEqual(self.saved, {"radio0": True})
        self.assertIsNone(note)


class StartupRepairTests(_ReconcileCase):
    """The '-5g-5g' repair is part of the boot reconcile: written, committed
    and reloaded, or the wrong name stays on air until someone renames it."""

    def test_boot_renames_a_doubled_suffix_ap_and_reloads(self):
        cfg = both_enabled()
        cfg["wireless.default_radio0.ssid"] = "photonicat2-4a99abb9-5g-5g"
        note = self.run_startup(cfg, {"radio0": True, "radio1": True})
        self.assertIn("wireless.default_radio0.ssid=photonicat2-4a99abb9",
                      self.sets)
        self.assertEqual(self.commits, ["wireless"])
        self.assertIn("renamed default_radio0", note)
        self.assertTrue(self.reloaded)

    def test_correctly_named_radios_are_left_alone(self):
        self.run_startup(both_enabled(), {"radio0": True, "radio1": True})
        self.assertEqual(self.sets, [])
        self.assertEqual(self.commits, [])
        self.assertFalse(self.reloaded)


class BrandingTests(unittest.TestCase):
    """A factory-stock AP must never go on the air as-is: it carries the stock
    name and no passphrase. Whoever generated it -- our own `wifi config` run
    or the firmware regenerating a pruned section -- it gets the device's
    identity back first."""

    def setUp(self):
        self.sets = []
        p = patch.object(wr, 'uci_set', lambda kv: self.sets.append(kv) or True)
        p.start()
        self.addCleanup(p.stop)
        # No relay backup unless a test asks for one.
        p2 = patch.object(wr, '_relay_ap_backup', lambda radio: None)
        p2.start()
        self.addCleanup(p2.stop)

    def _stock(self, radio, path, ssid="OpenWrt"):
        """A config with one configured radio and one stock AP on `radio`."""
        cfg = {
            "wireless.radio9": "wifi-device",
            "wireless.radio9.path": "platform/soc/other/usb1/1-1/1-1.9",
            "wireless.default_radio9": "wifi-iface",
            "wireless.default_radio9.device": "radio9",
            "wireless.default_radio9.mode": "ap",
            "wireless.default_radio9.ssid": "photonicat2-4a99abb9",
            "wireless.default_radio9.encryption": "psk2",
            "wireless.default_radio9.key": "photonicat",
            "wireless.{}".format(radio): "wifi-device",
            "wireless.{}.path".format(radio): path,
            "wireless.default_{}".format(radio): "wifi-iface",
            "wireless.default_{}.device".format(radio): radio,
            "wireless.default_{}.mode".format(radio): "ap",
        }
        if ssid is not None:
            cfg["wireless.default_{}.ssid".format(radio)] = ssid
        return cfg

    def test_stock_ap_is_detected_by_name_or_by_having_none(self):
        self.assertTrue(wr.is_stock_ap({}, "default_radio0"))
        self.assertTrue(wr.is_stock_ap(
            {"wireless.default_radio0.ssid": "OpenWrt"}, "default_radio0"))
        self.assertTrue(wr.is_stock_ap(
            {"wireless.default_radio0.ssid": "OpenWrt-5g"}, "default_radio0"))
        self.assertTrue(wr.is_stock_ap(
            {"wireless.default_radio0.ssid": "LEDE"}, "default_radio0"))
        self.assertFalse(wr.is_stock_ap(
            {"wireless.default_radio0.ssid": "my-network"}, "default_radio0"))

    def test_a_network_the_user_named_after_openwrt_is_theirs(self):
        """"Stock" is the exact factory name, not anything starting with it.
        Read as a prefix, someone who called their network 'OpenWrt-home' had
        its SSID and passphrase rewritten from the other radio's -- the exact
        harm this file exists to prevent."""
        for ssid in ("OpenWrt-home", "OpenWrtFTW", "LEDEwifi", "myOpenWrt"):
            cfg = {"wireless.default_radio0.ssid": ssid}
            self.assertFalse(wr.is_stock_ap(cfg, "default_radio0"), ssid)

    def test_a_user_named_openwrt_something_is_never_rebranded(self):
        cfg = self._stock("radio1", "soc/22000000.pcie/x", ssid="OpenWrt-home")
        self.assertEqual(wr.restore_stock_aps(cfg, ["radio9", "radio1"]), [])
        self.assertEqual(self.sets, [])

    def test_stock_pcie_ap_inherits_stem_credentials_and_5g_suffix(self):
        cfg = self._stock("radio1", "soc/22000000.pcie/pci0000:00/0000:01:00.0")
        branded = wr.restore_stock_aps(cfg, ["radio9", "radio1"])
        self.assertEqual(branded, [("radio1", "default_radio1", True)])
        self.assertIn(
            "wireless.default_radio1.ssid=photonicat2-4a99abb9-5g", self.sets)
        self.assertIn("wireless.default_radio1.encryption=psk2", self.sets)
        self.assertIn("wireless.default_radio1.key=photonicat", self.sets)

    def test_stock_onboard_ap_takes_the_bare_stem_not_a_second_suffix(self):
        """Issue #115: the radio already configured is as often the PCIe one
        as the onboard one, and inheriting its name verbatim produced
        'photonicat2-xxxx-5g-5g'."""
        cfg = self._stock("radio0", "platform/soc/23000000.usb/usb1/1-1/1-1.3")
        cfg["wireless.radio9.path"] = "soc/22000000.pcie/pci0000:00/0000:01:00.0"
        cfg["wireless.default_radio9.ssid"] = "photonicat2-46a4d8dd-5g"
        wr.restore_stock_aps(cfg, ["radio9", "radio0"])
        self.assertIn(
            "wireless.default_radio0.ssid=photonicat2-46a4d8dd", self.sets)

    def test_an_ap_with_no_ssid_at_all_is_branded_too(self):
        cfg = self._stock("radio1", "soc/22000000.pcie/x", ssid=None)
        self.assertTrue(wr.restore_stock_aps(cfg, ["radio9", "radio1"]))

    def test_branding_never_overwrites_a_real_ssid(self):
        cfg = self._stock("radio1", "soc/22000000.pcie/x", ssid="my-network")
        self.assertEqual(wr.restore_stock_aps(cfg, ["radio9", "radio1"]), [])
        self.assertEqual(self.sets, [])

    def test_nothing_to_inherit_from_means_no_branding(self):
        cfg = {
            "wireless.radio1": "wifi-device",
            "wireless.default_radio1": "wifi-iface",
            "wireless.default_radio1.device": "radio1",
            "wireless.default_radio1.mode": "ap",
            "wireless.default_radio1.ssid": "OpenWrt",
        }
        self.assertEqual(wr.restore_stock_aps(cfg, ["radio1"]), [])
        self.assertEqual(self.sets, [])

    def test_the_relay_backup_wins_and_is_used_verbatim(self):
        """The snapshot wifi_wan took before the radio became the upstream is
        the only surviving copy of the customer's own name and passphrase, so
        it beats deriving one from the neighbour -- and it is this radio's own
        name already, so it must not be re-suffixed."""
        cfg = self._stock("radio0", "platform/soc/23000000.usb/usb1/1-1/1-1.3")
        backup = {"ssid": "TheSmiths-5g", "encryption": "sae",
                  "key": "hunter2hunter2"}
        with patch.object(wr, '_relay_ap_backup',
                          lambda radio: backup if radio == "radio0" else None):
            wr.restore_stock_aps(cfg, ["radio9", "radio0"])
        self.assertIn("wireless.default_radio0.ssid=TheSmiths-5g", self.sets)
        self.assertIn("wireless.default_radio0.encryption=sae", self.sets)
        self.assertIn("wireless.default_radio0.key=hunter2hunter2", self.sets)


class SsidNamingTests(unittest.TestCase):
    """The firmware's AP naming: onboard bare, PCIe suffixed."""

    PATHS = {
        "wireless.radio0.path": "platform/soc/23000000.usb/usb1/1-1/1-1.3",
        "wireless.radio1.path": "soc/22000000.pcie/pci0000:00/0000:01:00.0",
    }

    def test_stem_strips_one_suffix_and_a_stacked_pair(self):
        self.assertEqual(wr.ssid_stem("photonicat2-x"), "photonicat2-x")
        self.assertEqual(wr.ssid_stem("photonicat2-x-5g"), "photonicat2-x")
        self.assertEqual(wr.ssid_stem("photonicat2-x-5g-5g"), "photonicat2-x")
        self.assertEqual(wr.ssid_stem("photonicat2-x-2.4g-5g"), "photonicat2-x")

    def test_stem_of_a_bare_suffix_is_left_alone(self):
        """Stripping to nothing would leave an AP with no name at all."""
        self.assertEqual(wr.ssid_stem("-5g"), "-5g")

    def test_radio_class_comes_from_the_device_path(self):
        self.assertTrue(wr.is_pcie_radio(self.PATHS, "radio1"))
        self.assertFalse(wr.is_pcie_radio(self.PATHS, "radio0"))
        self.assertFalse(wr.is_pcie_radio({}, "radio0"))

    def test_ap_names_follow_the_radio_not_the_band(self):
        self.assertEqual(wr.ap_ssid_for(self.PATHS, "radio0", "pcat"), "pcat")
        self.assertEqual(wr.ap_ssid_for(self.PATHS, "radio1", "pcat"),
                         "pcat-5g")


class StackedSuffixRepairTests(unittest.TestCase):
    """Issue #115: an AP left named '...-5g-5g' can only be fixed by hand --
    branding won't touch a non-stock section -- so the reconcile repairs it."""

    def _cfg(self, onboard_ssid):
        return {
            "wireless.radio0": "wifi-device",
            "wireless.radio0.path": "platform/soc/23000000.usb/usb1/1-1/1-1.3",
            "wireless.default_radio0": "wifi-iface",
            "wireless.default_radio0.device": "radio0",
            "wireless.default_radio0.mode": "ap",
            "wireless.default_radio0.ssid": onboard_ssid,
        }

    def test_doubled_suffix_is_collapsed_to_the_firmware_name(self):
        renames = wr.stacked_suffix_renames(
            self._cfg("photonicat2-46a4d8dd-5g-5g"), ["radio0"])
        self.assertEqual(
            renames,
            [("default_radio0", "photonicat2-46a4d8dd-5g-5g",
              "photonicat2-46a4d8dd")])

    def test_a_single_suffix_is_somebody_s_choice_and_is_kept(self):
        self.assertEqual(
            wr.stacked_suffix_renames(self._cfg("my-network-5g"), ["radio0"]),
            [])
        self.assertEqual(
            wr.stacked_suffix_renames(self._cfg("my-network"), ["radio0"]), [])

    def test_pcie_radio_keeps_exactly_one_suffix(self):
        cfg = self._cfg("photonicat2-x")
        cfg["wireless.radio0.path"] = "soc/22000000.pcie/pci0000:00/0000:01:00.0"
        cfg["wireless.default_radio0.ssid"] = "photonicat2-x-5g-5g"
        self.assertEqual(
            wr.stacked_suffix_renames(cfg, ["radio0"]),
            [("default_radio0", "photonicat2-x-5g-5g", "photonicat2-x-5g")])


class _DetectionCase(_ReconcileCase):
    """Drives startup() through a `wifi config` run: successive uci reads
    return successive configs, as if detection (and then our own commit) had
    landed in between."""

    ONBOARD_PATH = "platform/soc/23000000.usb/xhci-hcd.4.auto/usb1/1-1/1-1.3"
    PCIE_PATH = "soc/22000000.pcie/pci0000:00/0000:01:00.0"

    def run_reads(self, configs, intents=None, phys=2):
        reads = list(configs)

        def _show(*a, **k):
            return reads.pop(0) if len(reads) > 1 else reads[0]

        with patch.object(wr, 'uci_show_dict', _show), \
             patch.object(wr, 'get_intents', lambda: dict(intents or {})), \
             patch.object(wr, 'phy_count', lambda: phys), \
             patch.object(wr, 'detect_new_radios', self._detect_ok), \
             patch.object(wr, 'enable_radio',
                          lambda radio, vifs: self.enabled.append(radio) or True):
            return wr.startup()

    def _detect_ok(self):
        self.detect_calls.append(True)
        return True


class DetectionBrandingTests(_DetectionCase):
    """The first-boot path end to end: detection adds a radio, it is given the
    device's own identity, and only then switched on."""

    ONE_RADIO = {
        "wireless.radio0": "wifi-device",
        "wireless.radio0.path": _DetectionCase.ONBOARD_PATH,
        "wireless.default_radio0": "wifi-iface",
        "wireless.default_radio0.device": "radio0",
        "wireless.default_radio0.mode": "ap",
        "wireless.default_radio0.ssid": "photonicat2-4a99abb9",
        "wireless.default_radio0.encryption": "psk2",
        "wireless.default_radio0.key": "photonicat",
    }

    AFTER_DETECT = dict(ONE_RADIO, **{
        "wireless.radio1": "wifi-device",
        "wireless.radio1.path": _DetectionCase.PCIE_PATH,
        "wireless.radio1.band": "5g",
        "wireless.radio1.disabled": "1",
        "wireless.default_radio1": "wifi-iface",
        "wireless.default_radio1.device": "radio1",
        "wireless.default_radio1.mode": "ap",
        "wireless.default_radio1.ssid": "OpenWrt",
        "wireless.default_radio1.disabled": "1",
    })

    AFTER_BRAND = dict(AFTER_DETECT, **{
        "wireless.default_radio1.ssid": "photonicat2-4a99abb9-5g",
        "wireless.default_radio1.encryption": "psk2",
    })

    def test_detected_radio_is_branded_then_enabled(self):
        note = self.run_reads(
            [self.ONE_RADIO, self.AFTER_DETECT, self.AFTER_BRAND])
        self.assertIn("wireless.default_radio1.ssid=photonicat2-4a99abb9-5g",
                      self.sets)
        self.assertIn("wireless.default_radio1.key=photonicat", self.sets)
        self.assertEqual(self.enabled, ["radio1"])
        self.assertIn("detected radio1", note)
        self.assertIn("restored AP identity on radio1", note)

    def test_unbrandable_radio_is_detected_but_left_off(self):
        """Nothing to inherit from: the radio stays off rather than going on
        the air as the stock network, which has no passphrase."""
        stock_only = {k: v for k, v in self.AFTER_DETECT.items()}
        stock_only["wireless.default_radio0.ssid"] = "OpenWrt"
        del stock_only["wireless.default_radio0.encryption"]
        del stock_only["wireless.default_radio0.key"]
        one = {k: v for k, v in stock_only.items()
               if "radio1" not in k}
        note = self.run_reads([one, stock_only, stock_only])
        self.assertEqual(self.enabled, [])
        self.assertIn("left off: no AP identity", note)
        self.assertNotIn("radio1", self.saved)


class OrphanedSectionTests(_DetectionCase):
    """The regression that took the onboard WiFi off the settings page.

    Reboot with the upstream relay parked on the onboard radio and OpenWrt
    prunes that radio's `wifi-device` section, leaving its AP iface pointing at
    a device that no longer exists. Nothing used to notice: the old detection
    trigger counts phys against radio *sections*, and the pruned section took
    its own accounting with it.
    """

    # radio0's device section is gone; default_radio0 still references it.
    PRUNED = {
        "wireless.default_radio0": "wifi-iface",
        "wireless.default_radio0.device": "radio0",
        "wireless.default_radio0.mode": "ap",
        "wireless.default_radio0.ssid": "OpenWrt",
        "wireless.default_radio0.encryption": "none",
        "wireless.default_radio0.disabled": "1",
        "wireless.radio1": "wifi-device",
        "wireless.radio1.path": _DetectionCase.PCIE_PATH,
        "wireless.default_radio1": "wifi-iface",
        "wireless.default_radio1.device": "radio1",
        "wireless.default_radio1.mode": "ap",
        "wireless.default_radio1.ssid": "photonicat2-4a99abb9-5g",
        "wireless.default_radio1.encryption": "psk2",
        "wireless.default_radio1.key": "photonicat",
    }

    RESTORED = dict(PRUNED, **{
        "wireless.radio0": "wifi-device",
        "wireless.radio0.path": _DetectionCase.ONBOARD_PATH,
    })

    BRANDED = dict(RESTORED, **{
        "wireless.default_radio0.ssid": "photonicat2-4a99abb9",
        "wireless.default_radio0.encryption": "psk2",
    })

    def test_orphaned_radios_finds_a_device_with_no_section(self):
        self.assertEqual(wr.orphaned_radios(self.PRUNED), ["radio0"])
        self.assertEqual(wr.orphaned_radios(self.RESTORED), [])

    def test_a_healthy_config_reports_no_orphans(self):
        self.assertEqual(wr.orphaned_radios(both_enabled()), [])

    def test_pruned_section_triggers_detection_even_though_phys_match(self):
        """One phy, one radio section -- the old trigger would not fire."""
        self.run_reads([self.PRUNED, self.RESTORED, self.BRANDED], phys=1)
        self.assertTrue(self.detect_calls)

    def test_pruned_radio_is_recovered_branded_and_switched_back_on(self):
        note = self.run_reads([self.PRUNED, self.RESTORED, self.BRANDED])
        self.assertIn("recovered pruned section for radio0", note)
        self.assertIn("wireless.default_radio0.ssid=photonicat2-4a99abb9",
                      self.sets)
        self.assertEqual(self.enabled, ["radio0"])

    def test_the_customers_own_ssid_is_restored_from_the_relay_backup(self):
        """The relay's snapshot is the only surviving copy of the name and
        passphrase the customer chose; deriving one from the other radio would
        rename their network and force every client to re-pair."""
        backup = {"ssid": "TheSmiths", "encryption": "sae", "key": "hunter2!!"}
        with patch.object(wr, '_relay_ap_backup',
                          lambda radio: backup if radio == "radio0" else None):
            self.run_reads([self.PRUNED, self.RESTORED, self.BRANDED])
        self.assertIn("wireless.default_radio0.ssid=TheSmiths", self.sets)
        self.assertIn("wireless.default_radio0.key=hunter2!!", self.sets)

    def test_the_customers_band_comes_back_with_the_name(self):
        """A regenerated section returns on the firmware's default band. The
        onboard module here came back on 5 GHz where the customer had 2.4 --
        and a 2.4-only module would not come up on it at all, leaving the
        radio just as missing as before."""
        state = {"band": "2g", "htmode": "HT20", "channel": "10"}
        with patch.object(wr, '_relay_radio_backup',
                          lambda radio: state if radio == "radio0" else None):
            note = self.run_reads([self.PRUNED, self.RESTORED, self.BRANDED])
        self.assertIn("wireless.radio0.band=2g", self.sets)
        self.assertIn("wireless.radio0.htmode=HT20", self.sets)
        self.assertIn("wireless.radio0.channel=10", self.sets)
        self.assertIn("restored band on radio0", note)

    def test_a_band_the_customer_chose_is_never_overwritten(self):
        """Only a factory-stock section is retuned; a configured AP's band is
        the customer's business."""
        state = {"band": "2g"}
        with patch.object(wr, '_relay_radio_backup', lambda radio: state):
            wr.restore_regenerated_bands(both_enabled(), ["radio0", "radio1"])
        self.assertEqual(self.sets, [])

    def test_a_repaired_radio_is_not_mistaken_for_a_user_switch_off(self):
        """intent True + reads-as-off used to mean "the user did this, never
        touch it again". For a section the firmware regenerated that verdict
        was permanent and wrong."""
        note = self.run_reads([self.PRUNED, self.RESTORED, self.BRANDED],
                              intents={"radio0": True, "radio1": True})
        self.assertEqual(self.enabled, ["radio0"])
        self.assertNotIn("switched off elsewhere", note or "")
        self.assertIs(self.saved.get("radio0"), True)


class _FakeClock(object):
    """A clock the wait loop cannot outrun, so the suite stays fast."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class WirelessReadyWaitTests(unittest.TestCase):
    """startup() used to read the wireless config once and return None if it
    was not there yet. On the first boot after a flash it is not there yet --
    so the reconcile silently did nothing on exactly the boot it exists for,
    and the radio stayed off until the next one."""

    def _read(self, reads, expected=2, phys=2):
        clock = _FakeClock()
        queue = list(reads)

        def _show(*a, **k):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        with patch.object(wr, 'time', clock), \
             patch.object(wr, 'uci_show_dict', _show), \
             patch.object(wr, 'phy_count', lambda: phys), \
             patch.object(wr, 'declared_radio_count', lambda: expected):
            return wr._read_wireless(), clock

    def test_it_waits_for_a_config_the_boot_has_not_written_yet(self):
        got, clock = self._read([{}, {}, PCIE_DISABLED, PCIE_DISABLED])
        self.assertEqual(got, PCIE_DISABLED)
        self.assertEqual(clock.slept, [wr.WIRELESS_POLL_SECONDS] * 3)

    def test_it_waits_for_a_radio_that_has_no_section_yet(self):
        """The config gets written before the onboard module has enumerated,
        so a read can find one radio where the board declares two."""
        one_radio = {"wireless.radio0": "wifi-device"}
        got, clock = self._read([one_radio, PCIE_DISABLED, PCIE_DISABLED])
        self.assertEqual(got, PCIE_DISABLED)
        self.assertTrue(clock.slept)

    def test_it_waits_for_the_config_to_stop_changing(self):
        """Never act on a config the firmware is halfway through rewriting."""
        half = dict(PCIE_DISABLED)
        half["wireless.default_radio1.ssid"] = "OpenWrt"
        got, clock = self._read([half, PCIE_DISABLED, PCIE_DISABLED])
        self.assertEqual(got, PCIE_DISABLED)

    def test_a_settled_config_costs_one_confirming_read(self):
        got, clock = self._read([PCIE_DISABLED])
        self.assertEqual(got, PCIE_DISABLED)
        self.assertEqual(clock.slept, [wr.WIRELESS_POLL_SECONDS])

    def test_the_wait_is_bounded_so_a_wifi_less_box_still_boots(self):
        got, clock = self._read([{}])
        self.assertEqual(got, {})
        self.assertLessEqual(
            clock.now, wr.WIRELESS_WAIT_SECONDS + wr.WIRELESS_POLL_SECONDS)

    def test_a_dead_second_radio_still_lets_the_boot_finish(self):
        one_radio = {"wireless.radio0": "wifi-device"}
        got, clock = self._read([one_radio], expected=2)
        self.assertEqual(got, one_radio)
        self.assertLessEqual(
            clock.now, wr.WIRELESS_WAIT_SECONDS + wr.WIRELESS_POLL_SECONDS)


class RecheckTests(unittest.TestCase):
    """Waiting for the config to settle narrows the race but cannot close it:
    the firmware rewrites /etc/config/wireless when it pleases, and a single
    pass was observed being overtaken by one of those rewrites moments after
    it correctly found nothing wrong."""

    def test_startup_schedules_a_second_pass(self):
        calls = []
        with patch.object(wr, '_reconcile', lambda: calls.append(True)), \
             patch.object(wr, '_schedule_recheck',
                          lambda: calls.append("scheduled")), \
             patch.object(wr, 'RECHECK_SCHEDULE', (60,)):
            wr.startup()
        self.assertEqual(calls, [True, "scheduled"])

    def test_the_second_pass_can_be_switched_off(self):
        calls = []
        with patch.object(wr, '_reconcile', lambda: None), \
             patch.object(wr, '_schedule_recheck',
                          lambda: calls.append("scheduled")), \
             patch.object(wr, 'RECHECK_SCHEDULE', ()):
            wr.startup()
        self.assertEqual(calls, [])

    def test_the_second_pass_really_runs_the_reconcile(self):
        done = []
        with patch.object(wr, 'RECHECK_SCHEDULE', (0, 0, 0)), \
             patch.object(wr, '_reconcile',
                          lambda: done.append(True) or "enabled radio0"):
            wr._schedule_recheck().join(timeout=5)
        self.assertEqual(len(done), 3,
                         "every scheduled pass must run")

    def test_a_failing_second_pass_never_escapes_the_thread(self):
        """It runs on a daemon thread with nobody to catch it."""
        def boom():
            raise RuntimeError("uci exploded")
        with patch.object(wr, 'RECHECK_SCHEDULE', (0, 0)), \
             patch.object(wr, '_reconcile', boom):
            thread = wr._schedule_recheck()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


class ExpectedRadioCountTests(unittest.TestCase):
    """The count the wait is measured against has to come from something that
    is stable at boot, or the wait ends before the hardware shows up."""

    def _count(self, phys, declared):
        from pcat_util import command_parser
        with patch.object(wr, 'phy_count', lambda: phys), \
             patch.object(command_parser, '_read_board_phy_capabilities',
                          lambda: {"p%d" % i: {} for i in range(declared)}):
            return wr.expected_radio_count()

    def test_the_board_file_wins_while_a_phy_is_still_enumerating(self):
        self.assertEqual(self._count(phys=1, declared=2), 2)

    def test_a_card_the_board_file_does_not_know_about_still_counts(self):
        self.assertEqual(self._count(phys=3, declared=2), 3)

    def test_an_unreadable_board_file_falls_back_to_live_phys(self):
        from pcat_util import command_parser
        with patch.object(wr, 'phy_count', lambda: 2), \
             patch.object(command_parser, '_read_board_phy_capabilities',
                          side_effect=OSError("boom")):
            self.assertEqual(wr.expected_radio_count(), 2)


class StockApSafetyTests(_ReconcileCase):
    """Nothing here may put an unauthenticated network on the air. The
    reconcile was observed doing exactly that: it switched on a radio whose
    section was still factory 'OpenWrt' with `encryption owe`, bridged to the
    LAN."""

    def _stock_pcie(self):
        cfg = both_enabled()
        cfg["wireless.default_radio0.encryption"] = "psk2"
        cfg["wireless.default_radio0.key"] = "photonicat"
        cfg["wireless.default_radio1.ssid"] = "OpenWrt"
        cfg["wireless.default_radio1.encryption"] = "owe"
        return cfg

    def test_a_stock_ap_already_on_air_gets_its_identity_back(self):
        """Switching it off would drop whoever is associated, so it is
        rebranded in place rather than torn down."""
        note = self.run_startup(self._stock_pcie(),
                                {"radio0": True, "radio1": True})
        self.assertIn("wireless.default_radio1.ssid=photonicat2-4a99abb9-5g",
                      self.sets)
        self.assertIn("wireless.default_radio1.key=photonicat", self.sets)
        self.assertIn("restored AP identity on radio1", note)

    def test_a_radio_whose_identity_has_no_passphrase_is_left_off(self):
        cfg = dict(PCIE_DISABLED)
        cfg["wireless.default_radio1.ssid"] = "OpenWrt"
        # radio0 carries a name but no security, so nothing safe to copy.
        note = self.run_startup(cfg, {})
        self.assertEqual(self.enabled, [])
        self.assertIn("no passphrase", note)

    def test_an_unbrandable_disabled_radio_is_never_switched_on(self):
        cfg = dict(PCIE_DISABLED)
        cfg["wireless.default_radio0.ssid"] = "OpenWrt"
        cfg["wireless.default_radio1.ssid"] = "OpenWrt"
        note = self.run_startup(cfg, {})
        self.assertEqual(self.enabled, [])
        self.assertIn("left off: no AP identity", note)


class RelayRadioTests(_DetectionCase):
    """The radio carrying the upstream STA belongs to wifi_wan while the relay
    runs, and everything about it looks like the damage repaired here: its AP
    is parked (disabled), its section may have been regenerated as factory
    stock, and its band is the upstream's rather than the customer's.

    Acting on any of that fights wifi_wan -- and retuning a live relay drops
    the WAN uplink, which is a worse outcome than the missing hotspot the
    reconcile set out to fix."""

    # What wifi_wan snapshotted before taking the radio over. A *secured*
    # identity on purpose: with one, the repair has everything it needs to
    # switch the radio back on, so only the relay guard stops it.
    RELAY_BACKUP = {"ssid": "TheSmiths", "encryption": "psk2",
                    "key": "hunter2"}

    def _relaying(self, ap_ssid="OpenWrt"):
        cfg = dict(both_enabled())
        cfg["wireless.wifinet_sta"] = "wifi-iface"
        cfg["wireless.wifinet_sta.device"] = "radio0"
        cfg["wireless.wifinet_sta.mode"] = "sta"
        # What the relay leaves behind: the AP parked, and (once the firmware
        # has regenerated the section) named factory stock.
        cfg["wireless.default_radio0.disabled"] = "1"
        cfg["wireless.default_radio0.ssid"] = ap_ssid
        return cfg

    def _run(self, cfg=None, intents=None, **backups):
        """Drives one pass with the relay live on radio0.

        The second read carries the identity the pass just wrote, as the real
        re-read after the commit would: with a stale read the AP still looks
        stock, and the reconcile stops at the "no identity" guard before it
        ever reaches the decision under test.
        """
        backups.setdefault("_relay_ap_backup", self.RELAY_BACKUP)
        patches = [patch.object(wr, 'active_relay_radio', lambda: "radio0")]
        for name, value in backups.items():
            patches.append(patch.object(wr, name, lambda radio, v=value: v))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cfg = self._relaying() if cfg is None else cfg
        branded = dict(cfg)
        if wr.is_stock_ap(cfg, "default_radio0"):
            branded["wireless.default_radio0.ssid"] = self.RELAY_BACKUP["ssid"]
            branded["wireless.default_radio0.key"] = self.RELAY_BACKUP["key"]
        return self.run_reads(
            [cfg, branded],
            {"radio0": True} if intents is None else intents)

    def test_the_relays_own_ap_is_not_un_parked(self):
        self._run()
        self.assertEqual(self.enabled, [])

    def test_the_relay_radio_is_not_retuned_off_the_upstreams_band(self):
        """The snapshot holds the *pre-relay* band; writing it back while the
        relay is live moves the radio off the upstream AP's channel."""
        self._run(_relay_radio_backup={"band": "2g", "channel": "10"})
        self.assertNotIn("wireless.radio0.band=2g", self.sets)
        self.assertNotIn("wireless.radio0.channel=10", self.sets)

    def test_a_parked_ap_is_not_recorded_as_a_user_switch_off(self):
        """A parked AP that kept the customer's own name reads exactly like
        one somebody switched off elsewhere. Recorded as that, the intent is
        pinned off and the hotspot stays dark once the relay ends."""
        note = self._run(cfg=self._relaying(ap_ssid="TheSmiths"))
        self.assertIs(self.saved.get("radio0", True), True)
        self.assertNotIn("switched off elsewhere", note or "")

    def test_the_parked_ap_still_gets_the_customers_identity_back(self):
        """Safe while it is disabled, and it is what stops the AP returning as
        an open 'OpenWrt' when the relay is torn down."""
        self._run()
        self.assertIn("wireless.default_radio0.ssid=TheSmiths", self.sets)
        self.assertIn("wireless.default_radio0.key=hunter2", self.sets)

    def test_the_other_radio_is_reconciled_as_usual(self):
        """The guard is one radio wide; a relay running must not stop the
        other radio's hotspot being repaired."""
        cfg = self._relaying()
        cfg["wireless.radio1.disabled"] = "1"
        cfg["wireless.default_radio1.disabled"] = "1"
        self._run(cfg=cfg, intents={})
        self.assertEqual(self.enabled, ["radio1"])


class StalePathTests(unittest.TestCase):
    """The onboard module's USB host-controller index moves between boots
    (xhci-hcd.1 one boot, xhci-hcd.4 the next). A radio whose stored path has
    gone stale is still fitted, and must not be skipped by the reconcile."""

    CFG = {
        "wireless.radio0": "wifi-device",
        "wireless.radio0.path":
            "platform/soc/23000000.usb/xhci-hcd.1.auto/usb1/1-1/1-1.3",
    }

    def test_a_stale_path_falls_back_to_a_live_phy_of_the_same_class(self):
        from pcat_util import command_parser
        live = {"phy1": "soc/23000000.usb/xhci-hcd.4.auto/usb1/1-1/1-1.3"}
        with patch.object(command_parser, 'device_exist', lambda p: False), \
             patch.object(command_parser, 'live_phy_paths', lambda: live):
            self.assertTrue(wr.radio_present(self.CFG, "radio0"))

    def test_a_removed_card_is_still_reported_absent(self):
        from pcat_util import command_parser
        with patch.object(command_parser, 'device_exist', lambda p: False), \
             patch.object(command_parser, 'live_phy_paths', lambda: {}):
            self.assertFalse(wr.radio_present(self.CFG, "radio0"))


class IntentStorageTests(unittest.TestCase):

    def test_record_intent_writes_through_to_user_config(self):
        store = {}
        fake_config = MagicMock()
        fake_config.get_user_config_key.side_effect = \
            lambda key, fallback=None: store.get(key, fallback)
        fake_config.set_user_config_key.side_effect = \
            lambda key, value: store.__setitem__(key, value)

        with patch.dict('sys.modules', {'pcat_config': fake_config}):
            self.assertTrue(wr.record_intent("radio1", False))
            self.assertEqual(store[wr.INTENT_KEY], {"radio1": False})
            # Unchanged value is not rewritten.
            self.assertFalse(wr.record_intent("radio1", False))
            self.assertTrue(wr.record_intent("radio1", True))
            self.assertEqual(store[wr.INTENT_KEY], {"radio1": True})

    def test_corrupt_stored_intent_is_ignored(self):
        fake_config = MagicMock()
        fake_config.get_user_config_key.return_value = "not-a-dict"
        with patch.dict('sys.modules', {'pcat_config': fake_config}):
            self.assertEqual(wr.get_intents(), {})


if __name__ == '__main__':
    unittest.main()
