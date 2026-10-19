#!/usr/bin/env python3
"""Unit tests for how a radio doing upstream-STA duty is reported.

When the relay is running, that radio's own AP is parked (apply_config sets
default_radioN.disabled=1), so the AP's configured SSID names something that
is not on the air. The dashboard used to print it anyway -- "PCIE Wi-Fi
photonicat2-...-5g" for a radio that was really joined to an upstream
hotspot, and still that name when the STA was out of range entirely.

parse_uci_show_wireless() carries the fields the UI needs to say this
correctly (wifi_wan / wifi_wan_associated / wifi_wan_ssid); these tests pin
that contract, plus the label rule the templates apply to it.

Runs anywhere: uci and wifi_wan are stubbed, no device needed.

    python3 -m unittest tests.test_wireless_sta_relay_report -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))

from pcat_util import command_parser


# `uci show wireless` for a device relaying on radio1: radio1's AP is parked
# (disabled=1) and a wifinet_sta section carries the upstream.
UCI_RELAYING = b"""wireless.radio0=wifi-device
wireless.radio0.type='mac80211'
wireless.radio0.path='platform/soc/23000000.usb/xhci-hcd.4.auto/usb1/1-1/1-1.3'
wireless.radio0.band='5g'
wireless.radio0.channel='36'
wireless.radio0.htmode='HE80'
wireless.radio0.disabled='0'
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.mode='ap'
wireless.default_radio0.ssid='photonicat2-46a4d89f'
wireless.default_radio0.encryption='psk2'
wireless.default_radio0.disabled='0'
wireless.radio1=wifi-device
wireless.radio1.type='mac80211'
wireless.radio1.path='soc/22000000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0'
wireless.radio1.band='5g'
wireless.radio1.channel='36'
wireless.radio1.htmode='HE80'
wireless.radio1.disabled='0'
wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
wireless.default_radio1.mode='ap'
wireless.default_radio1.ssid='photonicat2-46a4d89f-5g'
wireless.default_radio1.encryption='psk2'
wireless.default_radio1.disabled='1'
"""


def radio_label(w, standby="Standby"):
    """The rule index.html's wifiRadioLabel() applies.

    Mirrored here so the backend contract and the label logic are checked
    together; the template is not executable from a unit test."""
    if not w:
        return ""
    if w.get("wifi_wan"):
        if w.get("wifi_wan_associated") and w.get("wifi_wan_ssid"):
            return w["wifi_wan_ssid"]
        return standby
    return w.get("ssid")


class StaRelayReportTest(unittest.TestCase):
    def setUp(self):
        self._uci_show = command_parser.uci_show
        self._board = command_parser._read_board_phy_capabilities
        self._enabled = command_parser.is_wifi_interface_enabled
        self._exist = command_parser.device_exist
        command_parser.uci_show = lambda _sec: UCI_RELAYING
        command_parser._read_board_phy_capabilities = lambda: {}
        command_parser.is_wifi_interface_enabled = lambda dev: True
        # device_exist() stats real sysfs paths; both radios are present here.
        command_parser.device_exist = lambda path: True
        # The parser does `from pcat_util import wifi_wan` at call time, which
        # binds the real module whenever anything else has already imported it
        # (the full-suite run does). Patch that module's attributes rather than
        # swapping sys.modules, which only works when this file runs alone.
        from pcat_util import wifi_wan
        self._wifi_wan = wifi_wan
        self._sta_fns = (wifi_wan.active_sta_radio, wifi_wan.get_state)

    def tearDown(self):
        command_parser.uci_show = self._uci_show
        command_parser._read_board_phy_capabilities = self._board
        command_parser.is_wifi_interface_enabled = self._enabled
        command_parser.device_exist = self._exist
        (self._wifi_wan.active_sta_radio,
         self._wifi_wan.get_state) = self._sta_fns

    def _parse_with_sta(self, sta_radio, associated, ssid, get_state=None):
        """Run the parser with wifi_wan stubbed to a given relay state."""
        self._wifi_wan.active_sta_radio = lambda: sta_radio
        self._wifi_wan.get_state = get_state or (
            lambda: {"associated": associated, "ssid": ssid})
        return {w["device"]: w for w in command_parser.parse_uci_show_wireless(False)}

    def test_relay_radio_reports_the_upstream_not_its_parked_ap(self):
        by_dev = self._parse_with_sta("radio1", True, "c2h2_xhome_ubnt_5G")
        r1 = by_dev["radio1"]
        self.assertTrue(r1["wifi_wan"])
        self.assertTrue(r1["wifi_wan_associated"])
        self.assertEqual(r1["wifi_wan_ssid"], "c2h2_xhome_ubnt_5G")
        # The AP name is still present, but must not be what the UI shows.
        self.assertEqual(r1["ssid"], "photonicat2-46a4d89f-5g")
        self.assertEqual(radio_label(r1), "c2h2_xhome_ubnt_5G")

    def test_relay_radio_out_of_range_reports_standby_not_a_stale_name(self):
        # STA enabled but not joined: the configured upstream ssid is stale and
        # must not be presented as a live link.
        by_dev = self._parse_with_sta("radio1", False, "c2h2_xhome_ubnt_5G")
        r1 = by_dev["radio1"]
        self.assertTrue(r1["wifi_wan"])
        self.assertFalse(r1["wifi_wan_associated"])
        self.assertEqual(r1["wifi_wan_ssid"], "",
                         "a disassociated STA must not export an ssid")
        self.assertEqual(radio_label(r1), "Standby")

    def test_relay_radio_is_not_offered_as_an_editable_ap(self):
        # Its AP is parked, so the settings UI must not present it as enabled.
        by_dev = self._parse_with_sta("radio1", True, "c2h2_xhome_ubnt_5G")
        self.assertFalse(by_dev["radio1"]["enabled"])

    def test_non_relay_radio_keeps_its_own_ap_name(self):
        by_dev = self._parse_with_sta("radio1", True, "c2h2_xhome_ubnt_5G")
        r0 = by_dev["radio0"]
        self.assertFalse(r0.get("wifi_wan"))
        self.assertEqual(radio_label(r0), "photonicat2-46a4d89f")

    def test_no_relay_at_all_leaves_both_radios_alone(self):
        by_dev = self._parse_with_sta(None, False, "")
        for dev, ssid in (("radio0", "photonicat2-46a4d89f"),
                          ("radio1", "photonicat2-46a4d89f-5g")):
            self.assertFalse(by_dev[dev].get("wifi_wan"))
            self.assertEqual(radio_label(by_dev[dev]), ssid)

    def test_device_type_is_reported_per_radio(self):
        # The dashboard prefixes the row with this ("PCIE Wi-Fi"), so a wrong
        # value mislabels which radio is relaying.
        by_dev = self._parse_with_sta("radio1", True, "up_5G")
        self.assertEqual(by_dev["radio0"]["device_type"], "Onboard")
        self.assertEqual(by_dev["radio1"]["device_type"], "PCIE")

    def test_get_state_failure_degrades_to_standby(self):
        # get_state() does live ubus probing and is failure-prone; a throw must
        # not un-mark the relay or resurrect the parked AP's name.
        def _boom():
            raise RuntimeError("ubus down")
        by_dev = self._parse_with_sta("radio1", False, "", get_state=_boom)
        r1 = by_dev["radio1"]
        self.assertTrue(r1["wifi_wan"])
        self.assertFalse(r1["wifi_wan_associated"])
        self.assertEqual(radio_label(r1), "Standby")


if __name__ == "__main__":
    unittest.main(verbosity=2)
