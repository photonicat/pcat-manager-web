#!/usr/bin/env python3
"""Unit tests for the Smart WAN keeper's scan cadence and link reporting.

These cover the two regressions behind "the device does not pick up my other
WiFi when I move between sites":

  * the scan backoff used to escalate to a 300s tier, so a device that had
    been away from every saved network (a commute) sat not-scanning for
    minutes after arriving somewhere known;
  * get_state() reported `connected` from interface-up + IP alone, which
    netifd keeps true on an unexpired DHCP lease after the AP is gone, so a
    dead link still read as "connected <stale ssid> <stale dBm>".

Runs anywhere: no device, no radio, no network. ubus/uci are stubbed.

    python3 -m unittest tests.test_wifi_wan_roaming -v
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))

from pcat_util import wifi_wan


class ScanBackoffTest(unittest.TestCase):
    """The backoff may never park the radio for longer than the cap.

    The site-to-site case is all fruitless scans (in transit), so any tier
    that grows past the cap is exactly what stops the device rejoining on
    arrival."""

    def setUp(self):
        wifi_wan._scan_backoff_reset()

    def tearDown(self):
        wifi_wan._scan_backoff_reset()

    def _delay_after(self, n):
        """Delay the keeper would wait after n consecutive fruitless scans."""
        wifi_wan._scan_backoff_reset()
        for _ in range(n):
            wifi_wan._scan_backoff_hit()
        return wifi_wan._scan_backoff["next_ok"] - time.time()

    def test_cap_is_60s(self):
        self.assertEqual(wifi_wan._SCAN_BACKOFF_CAP, 60)

    def test_never_exceeds_cap_even_after_many_failures(self):
        # 50 fruitless scans is a long trip; the delay must not grow past the
        # cap at any point along the way.
        for n in range(1, 51):
            self.assertLessEqual(
                self._delay_after(n), wifi_wan._SCAN_BACKOFF_CAP + 1,
                "after {} fruitless scans the keeper waits longer than the "
                "cap; arriving at a known network would not be noticed".format(n))

    def test_no_long_tier_constants_remain(self):
        # The escalation is the actual defect; assert it cannot come back
        # silently via a leftover constant.
        self.assertFalse(hasattr(wifi_wan, "_SCAN_BACKOFF_CAP_LONG"))
        self.assertFalse(hasattr(wifi_wan, "_SCAN_BACKOFF_ESCALATE_AFTER"))

    def test_reset_clears_the_pending_delay(self):
        self._delay_after(5)
        wifi_wan._scan_backoff_reset()
        self.assertEqual(wifi_wan._scan_backoff["fails"], 0)
        self.assertLessEqual(wifi_wan._scan_backoff["next_ok"], time.time())


class GetStateConnectedTest(unittest.TestCase):
    """`connected` must imply `associated`.

    Walking out of range leaves netifd holding the lease for a while: up=True
    and an IP, with no association. That must read as standby, not as a live
    link with the last-sampled signal."""

    STA = "wireless." + wifi_wan.STA_SECTION

    def setUp(self):
        self._orig = {
            "cfg": wifi_wan._wireless_cfg,
            "ubus": wifi_wan.ubus_call,
            "radio": wifi_wan.get_sta_radio,
            "ifname": wifi_wan._sta_ifname,
            "conn": wifi_wan.connectivity_state,
            "iw": wifi_wan._iw_link_signal,
        }
        wifi_wan.get_sta_radio = lambda: "radio1"
        wifi_wan._sta_ifname = lambda radio: "phy1-sta0"
        wifi_wan.connectivity_state = lambda: "unknown"
        wifi_wan._iw_link_signal = lambda dev: None
        wifi_wan._wireless_cfg = lambda: {
            self.STA: "wifi-iface",
            self.STA + ".ssid": "office_5G",
            self.STA + ".device": "radio1",
            self.STA + ".encryption": "psk2",
            self.STA + ".disabled": "0",
            "wireless.radio1.band": "5g",
        }

    def tearDown(self):
        wifi_wan._wireless_cfg = self._orig["cfg"]
        wifi_wan.ubus_call = self._orig["ubus"]
        wifi_wan.get_sta_radio = self._orig["radio"]
        wifi_wan._sta_ifname = self._orig["ifname"]
        wifi_wan.connectivity_state = self._orig["conn"]
        wifi_wan._iw_link_signal = self._orig["iw"]

    def _install_ubus(self, associated, ip="172.16.10.123", up=True):
        """Stub ubus so the STA is up with a lease, associated or not."""
        def _call(obj, method, args=None):
            if obj.startswith("network.interface"):
                if obj.endswith(wifi_wan.NET_IFACE_V6):
                    return {"up": up, "ipv6-address": []}
                return {
                    "up": up,
                    "uptime": 1234,
                    "l3_device": "phy1-sta0",
                    "ipv4-address": ([{"address": ip}] if ip else []),
                }
            if obj == "iwinfo" and method == "info":
                # An out-of-range STA reports no ssid; iwinfo carries no
                # signal for it either.
                return ({"ssid": "office_5G", "signal": -54, "bitrate": 866000}
                        if associated else {})
            return {}
        wifi_wan.ubus_call = _call

    def test_associated_with_lease_is_connected(self):
        self._install_ubus(associated=True)
        st = wifi_wan.get_state()
        self.assertTrue(st["associated"])
        self.assertTrue(st["connected"])
        self.assertEqual(st["signal"], -54)

    def test_stale_lease_without_association_is_not_connected(self):
        # The reported bug: out of range, lease still held.
        self._install_ubus(associated=False)
        st = wifi_wan.get_state()
        self.assertFalse(st["associated"])
        self.assertFalse(
            st["connected"],
            "an unexpired DHCP lease must not make a disassociated STA look "
            "connected -- that is what showed a stale ssid and dBm")
        # And no stale signal is carried over into the standby view.
        self.assertIsNone(st["signal"])
        # The IP is still reported (it is genuinely still assigned); only the
        # connected verdict changes.
        self.assertEqual(st["ip"], "172.16.10.123")

    def test_connected_implies_associated_for_every_ui_consumer(self):
        # Both dashboard and device_control branch on `connected` before their
        # standby check, so this implication is the contract they rely on.
        for assoc in (True, False):
            self._install_ubus(associated=assoc)
            st = wifi_wan.get_state()
            if st["connected"]:
                self.assertTrue(st["associated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
