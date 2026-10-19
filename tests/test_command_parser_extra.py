#!/usr/bin/env python3
"""Extra unit tests for app/pcat_util/command_parser.py.

test_wireless_be.py already covers _frequency_from_band_htmode, the happy-path
board.json parsing and the STA-relay reporting. This file targets the branches
that suite leaves dark -- the UCI enable/disable logic, hostapd client
counting, /sys device probing, encryption normalization edge cases and the
mock-fixture path -- because those are exactly the paths that run on a real
device where a wrong answer silently shows the wrong Wi-Fi state in the UI.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_command_parser_extra`.
"""

import json
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch, mock_open

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# tests/run_tests.py mocks `pcat_config` in sys.modules, which makes the real
# pcat_util.pcat_logger explode at import (RotatingFileHandler gets a MagicMock
# filename). wifi_wan imports pcat_logger, so graft the same lightweight stub
# test_remember.py uses before touching wifi_wan -- wifi_wan only logs.
import pcat_util  # noqa: E402  -- real package, empty __init__.py
if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_command_parser_extra")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

# Import the real modules (openwrt wrapper falls back to subprocess on a dev
# host, so this is safe both standalone and under tests/run_tests.py).
from pcat_util import command_parser  # noqa: E402
from pcat_util import wifi_wan  # noqa: E402  -- patched in relay tests below


# ---------- _normalize_encryption --------------------------------------------


class NormalizeEncryptionTests(unittest.TestCase):
    """UCI stores cipher-suffixed values ('psk2+ccmp'); the UI dropdown only
    knows the canonical four. A missed mapping means the dropdown renders with
    nothing selected and a save would silently downgrade the user's security."""

    def test_empty_and_none_mean_open_network(self):
        # UCI omits `encryption` entirely on an open AP; the UI needs "none",
        # not "" -- "" would match no <option> at all.
        self.assertEqual(command_parser._normalize_encryption(None), "none")
        self.assertEqual(command_parser._normalize_encryption(""), "none")

    def test_cipher_suffix_is_stripped(self):
        self.assertEqual(command_parser._normalize_encryption("psk2+ccmp"), "psk2")
        self.assertEqual(command_parser._normalize_encryption("psk+tkip"), "psk")
        self.assertEqual(command_parser._normalize_encryption("sae+ccmp"), "sae")

    def test_sae_mixed_maps_to_sae(self):
        # WPA2/WPA3 transition mode must still light up the WPA3 option.
        self.assertEqual(command_parser._normalize_encryption("sae-mixed"), "sae")
        self.assertEqual(command_parser._normalize_encryption("sae-mixed+ccmp"), "sae")

    def test_canonical_values_pass_through(self):
        for v in ("sae", "psk2", "psk", "none"):
            self.assertEqual(command_parser._normalize_encryption(v), v)

    def test_unknown_value_kept_verbatim(self):
        # Enterprise/WEP configs the UI can't edit must survive a round-trip
        # unchanged rather than being coerced into a wrong canonical value.
        self.assertEqual(command_parser._normalize_encryption("wpa2+eap"), "wpa2+eap")
        self.assertEqual(command_parser._normalize_encryption("owe"), "owe")


# ---------- is_wifi_interface_enabled ----------------------------------------


class WifiInterfaceEnabledTests(unittest.TestCase):
    """A radio is only 'on' when neither the device nor its default iface is
    disabled; missing keys mean enabled (OpenWrt default). Getting this wrong
    shows a live AP as off (or worse, hides that it's off) on the screen."""

    def _run(self, dev_disabled, iface_disabled):
        values = {
            "wireless.radio0.disabled": dev_disabled,
            "wireless.default_radio0.disabled": iface_disabled,
        }
        with patch.object(command_parser, "uci_get", side_effect=values.__getitem__):
            return command_parser.is_wifi_interface_enabled("radio0")

    def test_both_keys_absent_means_enabled(self):
        # uci_get returns "" for unset options -- the factory-default config
        # has no `disabled` keys at all, and the radio is on.
        self.assertTrue(self._run("", ""))

    def test_both_explicit_zero_means_enabled(self):
        self.assertTrue(self._run("0", "0"))

    def test_device_disabled_wins(self):
        self.assertFalse(self._run("1", "0"))

    def test_iface_disabled_wins(self):
        # The relay flow disables only default_radioN, leaving the device
        # section alone -- that alone must report the AP as not enabled.
        self.assertFalse(self._run("", "1"))


# ---------- live_ap_ifnames / get_wifi_clients_count -------------------------


def _fake_wireless_env(status, hostapd):
    """ubus_call stub covering network.wireless status + hostapd probes."""
    def fake_ubus(obj, method):
        if obj == "network.wireless":
            return status
        return hostapd.get(obj)
    return fake_ubus


class LiveApIfnamesTests(unittest.TestCase):
    def test_collects_ap_vifs_across_radios_and_skips_sta(self):
        status = {
            "radio0": {"interfaces": [
                {"ifname": "phy2-sta0", "config": {"mode": "sta"}},
            ]},
            "radio1": {"interfaces": [
                {"ifname": "phy0-ap0", "config": {"mode": "ap"}},
                {"ifname": "phy0-ap1", "config": {"mode": "ap"}},
            ]},
        }
        with patch.object(command_parser, "ubus_call",
                          side_effect=_fake_wireless_env(status, {})):
            self.assertEqual(command_parser.live_ap_ifnames(),
                             ["phy0-ap0", "phy0-ap1"])

    def test_unreachable_netifd_is_none_not_empty(self):
        with patch.object(command_parser, "ubus_call", return_value=None):
            self.assertIsNone(command_parser.live_ap_ifnames())


class WifiClientsCountTests(unittest.TestCase):
    def test_upstream_mode_counts_the_ap_on_the_other_radio(self):
        # The bug 5 regression: radio0 relays upstream as a STA, the AP lives
        # on radio1 -- and on this boot its netdev is phy0-ap0, the name the
        # old index-guessed probe attributed to radio0 and skipped.
        status = {
            "radio0": {"interfaces": [
                {"ifname": "phy2-sta0", "config": {"mode": "sta"}},
            ]},
            "radio1": {"interfaces": [
                {"ifname": "phy0-ap0", "config": {"mode": "ap"}},
            ]},
        }
        hostapd = {"hostapd.phy0-ap0": {"clients": {"aa:bb": {}, "cc:dd": {}}}}
        with patch.object(command_parser, "ubus_call",
                          side_effect=_fake_wireless_env(status, hostapd)):
            self.assertEqual(command_parser.get_wifi_clients_count(), 2)

    def test_sums_every_ap_vif_not_first_answer_wins(self):
        status = {
            "radio0": {"interfaces": [
                {"ifname": "phy1-ap0", "config": {"mode": "ap"}},
                {"ifname": "phy1-ap1", "config": {"mode": "ap"}},
            ]},
        }
        hostapd = {
            "hostapd.phy1-ap0": {"clients": {}},
            "hostapd.phy1-ap1": {"clients": {"aa:bb": {}}},
        }
        with patch.object(command_parser, "ubus_call",
                          side_effect=_fake_wireless_env(status, hostapd)):
            self.assertEqual(command_parser.get_wifi_clients_count(), 1)

    def test_a_dead_hostapd_probe_is_unknown_not_zero(self):
        # An AP we cannot see may hold the only client; reporting 0 here is
        # what used to power the device off under people.
        status = {"radio0": {"interfaces": [
            {"ifname": "phy0-ap0", "config": {"mode": "ap"}},
        ]}}
        with patch.object(command_parser, "ubus_call",
                          side_effect=_fake_wireless_env(status, {})):
            self.assertIsNone(command_parser.get_wifi_clients_count())

    def test_no_ap_up_at_all_is_a_truthful_zero(self):
        status = {"radio0": {"interfaces": []}}
        with patch.object(command_parser, "ubus_call",
                          side_effect=_fake_wireless_env(status, {})):
            self.assertEqual(command_parser.get_wifi_clients_count(), 0)

    def test_unreachable_netifd_is_unknown(self):
        with patch.object(command_parser, "ubus_call", return_value=None):
            self.assertIsNone(command_parser.get_wifi_clients_count())


# ---------- device_exist -----------------------------------------------------


class DeviceExistTests(unittest.TestCase):
    """UCI `path` values are written both with and without the `platform/`
    prefix depending on the kernel version; device_exist must accept either,
    else a present radio is dropped from the settings page entirely."""

    DEV = "soc/23000000.usb/xhci-hcd.0.auto/usb1/1-1/1-1.3"

    def test_direct_sys_devices_path(self):
        with patch("os.path.exists",
                   side_effect=lambda p: p == "/sys/devices/" + self.DEV):
            self.assertTrue(command_parser.device_exist(self.DEV))

    def test_platform_prefixed_fallback_path(self):
        with patch("os.path.exists",
                   side_effect=lambda p: p == "/sys/devices/platform/" + self.DEV):
            self.assertTrue(command_parser.device_exist(self.DEV))

    def test_absent_device(self):
        with patch("os.path.exists", return_value=False):
            self.assertFalse(command_parser.device_exist(self.DEV))


# ---------- _read_board_phy_capabilities malformed entries -------------------


class BoardJsonMalformedEntryTests(unittest.TestCase):
    """board.json is vendor-written; a non-dict phy or band entry must be
    skipped, not crash the wireless page for every radio."""

    def test_non_dict_phy_and_band_entries_are_skipped(self):
        board = {
            "wlan": {
                "phy0": "not-a-dict",  # e.g. a stray string from a bad build
                "phy1": {
                    "path": "soc/ok-path",
                    "info": {"bands": {
                        "5G": "bogus",           # skipped
                        "2G": {"ht": True},      # kept
                    }},
                },
            },
        }
        with patch("builtins.open", mock_open(read_data=json.dumps(board))):
            caps = command_parser._read_board_phy_capabilities()
        self.assertEqual(list(caps), ["soc/ok-path"])
        self.assertEqual(list(caps["soc/ok-path"]["bands"]), ["2g"])
        self.assertTrue(caps["soc/ok-path"]["bands"]["2g"]["ht"])


# ---------- parse_uci_show_wireless: uncovered branches ----------------------


class ParseWirelessEmptyOutputTests(unittest.TestCase):
    def test_empty_uci_output_returns_empty_list(self):
        # uci_show returns b"" when the wireless config is missing (fresh
        # flash, wifi package absent). The parser must return [] early instead
        # of handing the UI two phantom radios.
        with patch.object(command_parser, "uci_show", return_value=b""):
            self.assertEqual(
                command_parser.parse_uci_show_wireless(is_mock_data=False), [])


class ParseWirelessMockFixtureTests(unittest.TestCase):
    """The is_mock_data=True path reads app/test/fixture_wireless.txt and is
    what dev-start.sh exercises on x86 -- if it rots, local development of the
    wireless page breaks without anyone noticing until they're on a device."""

    def test_fixture_parses_to_two_radios(self):
        # device_exist / is_wifi_interface_enabled still hit /sys and uci even
        # in mock mode, so pin them for host independence.
        with patch.object(command_parser, "device_exist", return_value=True), \
             patch.object(command_parser, "is_wifi_interface_enabled",
                          return_value=True):
            result = command_parser.parse_uci_show_wireless(is_mock_data=True)
        self.assertEqual(len(result), 2)
        by_dev = {r["device"]: r for r in result}
        r0, r1 = by_dev["radio0"], by_dev["radio1"]
        # Fixture radio0 is the onboard mmc module with a hidden 2g AP.
        self.assertEqual(r0["device_type"], "Onboard")
        self.assertEqual(r0["hidden"], "1")
        self.assertEqual(r0["frequency"], "2g")
        # Fixture radio1 is the pcie Wi-Fi 6 card.
        self.assertEqual(r1["device_type"], "PCIE")
        self.assertEqual(r1["frequency"], "5g_ax")
        # Mock mode must not read /etc/board.json -- empty caps placeholder.
        self.assertEqual(r0["capabilities"], {"is_pcie": False, "bands": {}})


# radio0 on a pcie path, radio1 on an onboard (builtin) path -- the mirror
# image of every other test's layout, so this exercises the radio0-PCIE and
# radio1-Onboard classification branches. Both APs hidden.
UCI_SWAPPED_RADIOS = b"""\
wireless.radio0=wifi-device
wireless.radio0.path='soc/22000000.pcie/pci0000:00/0000:01:00.0'
wireless.radio0.band='2g'
wireless.radio0.htmode='HT20'
wireless.default_radio0=wifi-iface
wireless.default_radio0.ssid='cat-2g'
wireless.default_radio0.encryption='psk2+ccmp'
wireless.default_radio0.key='secret'
wireless.default_radio0.hidden='1'
wireless.radio1=wifi-device
wireless.radio1.path='platform/fe2c0000.mmc/mmc_host/mmc2/mmc2:0001/mmc2:0001:1'
wireless.radio1.band='5g'
wireless.radio1.htmode='VHT40'
wireless.default_radio1=wifi-iface
wireless.default_radio1.ssid='cat-5g'
wireless.default_radio1.encryption='sae-mixed'
wireless.default_radio1.key='secret'
wireless.default_radio1.hidden='1'
"""


class ParseWirelessSwappedRadioTests(unittest.TestCase):
    """Device-type classification must depend on the path, not on which radioN
    slot the driver happened to enumerate first -- USB re-enumeration order is
    not stable across boots."""

    def _run(self):
        with patch.object(command_parser, "uci_show",
                          return_value=UCI_SWAPPED_RADIOS), \
             patch.object(command_parser, "device_exist", return_value=True), \
             patch.object(command_parser, "is_wifi_interface_enabled",
                          return_value=True), \
             patch.object(command_parser, "_read_board_phy_capabilities",
                          return_value={}), \
             patch.object(wifi_wan, "active_sta_radio",
                          side_effect=RuntimeError("ubus down")):
            # active_sta_radio raising must degrade to "no relay radio", not
            # take the whole wireless page down (see the except at the STA
            # probe in parse_uci_show_wireless).
            return command_parser.parse_uci_show_wireless(is_mock_data=False)

    def test_pcie_radio0_and_onboard_radio1(self):
        by_dev = {r["device"]: r for r in self._run()}
        self.assertEqual(by_dev["radio0"]["device_type"], "PCIE")
        self.assertEqual(by_dev["radio1"]["device_type"], "Onboard")

    def test_hidden_flag_parsed_for_both_radios(self):
        by_dev = {r["device"]: r for r in self._run()}
        self.assertEqual(by_dev["radio0"]["hidden"], "1")
        self.assertEqual(by_dev["radio1"]["hidden"], "1")

    def test_encryption_normalized_and_sta_probe_failure_ignored(self):
        by_dev = {r["device"]: r for r in self._run()}
        self.assertEqual(by_dev["radio0"]["encryption"], "psk2")
        self.assertEqual(by_dev["radio1"]["encryption"], "sae")
        # STA probe blew up -> neither radio may be marked as relaying.
        self.assertNotIn("wifi_wan", by_dev["radio0"])
        self.assertNotIn("wifi_wan", by_dev["radio1"])


# The settings page after a reboot with the upstream relay parked on the
# onboard radio: OpenWrt pruned `wifi-device radio0` because it could not match
# the stored path to a phy, leaving default_radio0 pointing at nothing.
UCI_PRUNED_ONBOARD = b"""\
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.mode='ap'
wireless.default_radio0.ssid='photonicat2-4a99c96b'
wireless.default_radio0.encryption='psk2'
wireless.default_radio0.key='photonicat'
wireless.default_radio0.disabled='1'
wireless.radio1=wifi-device
wireless.radio1.path='soc/22000000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0'
wireless.radio1.band='5g'
wireless.radio1.htmode='HE80'
wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
wireless.default_radio1.mode='ap'
wireless.default_radio1.ssid='photonicat2-4a99c96b-5g'
wireless.default_radio1.encryption='psk2'
wireless.default_radio1.key='photonicat'
"""

ONBOARD_LIVE = "soc/23000000.usb/xhci-hcd.4.auto/usb1/1-1/1-1.3"
PCIE_LIVE = "soc/22000000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0"


class NeverHideAFittedRadioTests(unittest.TestCase):
    """A radio that is physically fitted must never drop off the settings
    page. When it does, the customer sees their WiFi simply gone -- which is
    what happened when OpenWrt pruned the onboard radio's device section and
    the parser keyed `exist` off the (now absent) path line."""

    def _run(self, uci_bytes, live, resolvable=()):
        """`resolvable` is the set of configured paths that /sys can still
        find; everything else is treated as stale."""
        with patch.object(command_parser, "uci_show", return_value=uci_bytes), \
             patch.object(command_parser, "device_exist",
                          side_effect=lambda p: p in resolvable), \
             patch.object(command_parser, "is_wifi_interface_enabled",
                          return_value=True), \
             patch.object(command_parser, "_read_board_phy_capabilities",
                          return_value={}), \
             patch.object(command_parser, "live_phy_paths",
                          return_value=live), \
             patch.object(wifi_wan, "active_sta_radio", return_value=None):
            return command_parser.parse_uci_show_wireless(is_mock_data=False)

    def test_a_radio_whose_device_section_was_pruned_is_still_listed(self):
        live = {"phy0": PCIE_LIVE, "phy1": ONBOARD_LIVE}
        result = self._run(UCI_PRUNED_ONBOARD, live, resolvable=(PCIE_LIVE,))
        by_dev = {r["device"]: r for r in result}
        self.assertIn("radio0", by_dev,
                      "the onboard radio vanished from the settings page")
        self.assertTrue(by_dev["radio0"]["exist"])
        # It is matched to the one phy the PCIe radio does not account for,
        # so it is labelled and editable rather than showing as "Unknown".
        self.assertEqual(by_dev["radio0"]["device_type"], "Onboard")
        self.assertEqual(by_dev["radio0"]["ssid"], "photonicat2-4a99c96b")

    def test_radio0_keeps_its_slot_so_the_two_cards_do_not_swap(self):
        live = {"phy0": PCIE_LIVE, "phy1": ONBOARD_LIVE}
        result = self._run(UCI_PRUNED_ONBOARD, live, resolvable=(PCIE_LIVE,))
        self.assertEqual([r["device"] for r in result], ["radio0", "radio1"])

    def test_a_stale_usb_path_does_not_hide_the_onboard_radio(self):
        """The host-controller index moves between boots; the stored path then
        resolves to nothing while the card is sitting right there."""
        stale = UCI_PRUNED_ONBOARD.replace(
            b"wireless.default_radio0=wifi-iface",
            b"wireless.radio0=wifi-device\n"
            b"wireless.radio0.path='platform/soc/23000000.usb/"
            b"xhci-hcd.1.auto/usb1/1-1/1-1.3'\n"
            b"wireless.radio0.band='2g'\n"
            b"wireless.default_radio0=wifi-iface")
        live = {"phy0": PCIE_LIVE, "phy1": ONBOARD_LIVE}
        by_dev = {r["device"]: r
                  for r in self._run(stale, live, resolvable=(PCIE_LIVE,))}
        self.assertIn("radio0", by_dev)
        self.assertEqual(by_dev["radio0"]["device_type"], "Onboard")

    def test_a_card_that_really_is_absent_is_not_offered(self):
        """The other direction still holds: don't show hardware nobody has."""
        live = {"phy0": PCIE_LIVE}
        by_dev = {r["device"]: r
                  for r in self._run(UCI_PRUNED_ONBOARD, live,
                                     resolvable=(PCIE_LIVE,))}
        self.assertNotIn("radio0", by_dev)
        self.assertIn("radio1", by_dev)


class RadioPathHelperTests(unittest.TestCase):

    def test_canonical_path_collapses_the_two_spellings(self):
        self.assertEqual(
            command_parser.canonical_path("platform/soc/x.usb/dev"),
            "soc/x.usb/dev")
        self.assertEqual(command_parser.canonical_path("soc/x.pcie/dev"),
                         "soc/x.pcie/dev")

    def test_device_type_survives_a_drifting_usb_index(self):
        for index in (0, 4, 99):
            path = ("platform/soc/23000000.usb/xhci-hcd.%d.auto/usb1/1-1/1-1.3"
                    % index)
            self.assertEqual(command_parser.device_type_for_path(path),
                             "Onboard")

    def test_device_type_of_pcie_and_of_something_unrecognised(self):
        self.assertEqual(command_parser.device_type_for_path(PCIE_LIVE), "PCIE")
        self.assertEqual(command_parser.device_type_for_path("soc/1.spi/dev"),
                         "Unknown")
        self.assertEqual(command_parser.device_type_for_path(""), "Unknown")

    def test_radio_names_include_a_device_only_an_iface_still_references(self):
        cfg = {
            "wireless.radio1": "wifi-device",
            "wireless.default_radio1": "wifi-iface",
            "wireless.default_radio1.device": "radio1",
            "wireless.default_radio0": "wifi-iface",
            "wireless.default_radio0.device": "radio0",
        }
        self.assertEqual(command_parser.radio_names(cfg), ["radio0", "radio1"])

    def test_a_usb_phy_resolves_to_the_device_uci_records(self):
        """A USB driver binds to the *interface*, so a phy's `device` link is
        one level below the path UCI and board.json carry. Left in, the
        onboard radio matches no capability entry and the settings page offers
        it no bands at all."""
        real = ("/sys/devices/platform/soc/23000000.usb/xhci-hcd.4.auto/usb1/"
                "1-1/1-1.3/1-1.3:1.0")
        with patch.object(command_parser.os, "listdir",
                          return_value=["phy0"]), \
             patch.object(command_parser.os.path, "realpath",
                          return_value=real):
            self.assertEqual(
                command_parser.live_phy_paths(),
                {"phy0": "soc/23000000.usb/xhci-hcd.4.auto/usb1/1-1/1-1.3"})

    def test_an_sdio_function_is_the_path_and_survives_intact(self):
        """It looks like a USB interface ('mmc2:0001/mmc2:0001:1') but *is*
        what builtin_wifi_dev records, so peeling it would break the onboard
        radio on the mmc-attached boards instead."""
        sdio = "platform/fe2c0000.mmc/mmc_host/mmc2/mmc2:0001/mmc2:0001:1"
        self.assertIn(sdio, command_parser.builtin_wifi_dev)
        self.assertEqual(command_parser._strip_usb_interface(sdio), sdio)

    def test_a_pcie_function_is_left_alone_too(self):
        self.assertEqual(command_parser._strip_usb_interface(PCIE_LIVE),
                         PCIE_LIVE)

    def test_radio_names_put_the_two_primary_slots_first(self):
        cfg = {
            "wireless.radio2": "wifi-device",
            "wireless.radio1": "wifi-device",
            "wireless.radio0": "wifi-device",
        }
        self.assertEqual(command_parser.radio_names(cfg),
                         ["radio0", "radio1", "radio2"])


if __name__ == "__main__":
    unittest.main()
