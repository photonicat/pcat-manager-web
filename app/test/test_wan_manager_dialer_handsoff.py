#!/usr/bin/env python3
"""The r7897 cellular-timing report: a cellular family the user re-pointed at
a netifd dialer proto (mbim/qmi) must be left alone -- no disabled rewrite, no
up/down nudge, no metric management -- or netifd's dialer fights the vendor
pcat-manager/quectel-cm chain for the modem's /dev/cdc-wdm node (MBIM
timeouts, PIN_FAILED, 15s retry loop, QMI session dead for minutes). These
tests pin the hands-off filter.

Runs anywhere: uci is stubbed, no device needed.

    cd app && python3 test/test_wan_manager_dialer_handsoff.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pcat_util import wan_manager


def _stub_uci(values):
    def uci_get(key):
        return values.get(key, "")
    return uci_get


class DialerHandsOffTest(unittest.TestCase):
    def setUp(self):
        self._orig = wan_manager.uci_get
        self.addCleanup(lambda: setattr(wan_manager, "uci_get", self._orig))
        self.addCleanup(wan_manager._handsoff_logged.clear)
        reset = {"time": 0.0, "pairs": None}
        wan_manager._dialer_cache.update(reset)
        self.addCleanup(lambda: wan_manager._dialer_cache.update(dict(reset)))

    def _protos(self, mapping):
        vals = {"network.{}.proto".format(k): v for k, v in mapping.items()}
        wan_manager.uci_get = _stub_uci(vals)

    def test_stock_protos_all_managed(self):
        self._protos({"wwan_5g": "dhcp", "wwan_lte": "dhcp",
                      "wwan_fm350": "static"})
        self.assertEqual(wan_manager._cellular_iface_pairs(),
                         list(wan_manager.SLOT_IFACES["cellular"]))

    def test_mbim_family_hands_off(self):
        self._protos({"wwan_5g": "mbim", "wwan_lte": "dhcp"})
        pairs = wan_manager._cellular_iface_pairs()
        self.assertNotIn(("wwan_5g", "wwan_5g_v6"), pairs)
        self.assertIn(("wwan_lte", "wwan_lte_v6"), pairs)

    def test_qmi_family_hands_off(self):
        self._protos({"wwan_lte": "qmi"})
        self.assertNotIn(("wwan_lte", "wwan_lte_v6"),
                         wan_manager._cellular_iface_pairs())

    def test_v6_twin_proto_also_hands_off(self):
        self._protos({"wwan_5g_v6": "mbim"})
        self.assertNotIn(("wwan_5g", "wwan_5g_v6"),
                         wan_manager._cellular_iface_pairs())

    def test_metrics_skip_hands_off_family(self):
        self._protos({"wwan_5g": "mbim"})
        priority = [{"id": "cellular", "enabled": True}]
        metrics = wan_manager.slot_metrics("cellular", priority)
        self.assertNotIn("wwan_5g", metrics)
        self.assertNotIn("wwan_5g_v6", metrics)
        self.assertIn("wwan_lte", metrics)

    def test_non_cellular_slots_unfiltered(self):
        self._protos({"wwan_5g": "mbim"})
        self.assertEqual(wan_manager._slot_iface_pairs("wan"),
                         wan_manager.SLOT_IFACES["wan"])

    def test_cache_refreshes_after_ttl(self):
        self._protos({"wwan_5g": "dhcp"})
        self.assertIn(("wwan_5g", "wwan_5g_v6"),
                      wan_manager._cellular_iface_pairs())
        self._protos({"wwan_5g": "mbim"})
        # Cached verdict holds inside the TTL...
        self.assertIn(("wwan_5g", "wwan_5g_v6"),
                      wan_manager._cellular_iface_pairs())
        # ...and the proto edit lands once it expires.
        wan_manager._dialer_cache["time"] = 0.0
        self.assertNotIn(("wwan_5g", "wwan_5g_v6"),
                         wan_manager._cellular_iface_pairs())


if __name__ == "__main__":
    unittest.main()
