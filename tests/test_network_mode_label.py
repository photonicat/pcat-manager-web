#!/usr/bin/env python3
"""Unit tests for get_network_mode_label() in app/pcat_manager_web.py.

The label is what the photonicat mini-display prints verbatim on its WAN row
(dashboard.json -> "network_mode_label"), so it has to name every enabled
uplink: before priority mode was handled here, a device with all three slots
on still read "Eth/5G" because "priority" fell through to the default.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_network_mode_label`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

SLOTS = ("wan", "wifi", "cellular")


def table(*enabled):
    """Priority table in the fixed slot order with `enabled` switched on."""
    return [{"id": slot, "enabled": slot in enabled} for slot in SLOTS]


class NetworkModeLabelTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_config': MagicMock(),
            'pcat_util.user_auth': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.command_parser': MagicMock(),
            'pcat_util.hardware_util': MagicMock(),
            'pcat_util.isp_dict': MagicMock(),
            'pc_socket_client': MagicMock(),
            'pc_modem_client': MagicMock(),
            'pc_sms_client': MagicMock(),
            'pc_screen_client': MagicMock(),
            'app': MagicMock(),
            'flask': MagicMock(),
            'flask_socketio': MagicMock(),
        })
        self.module_patch.start()
        import pcat_manager_web
        self.module = pcat_manager_web

    def tearDown(self):
        self.module_patch.stop()

    def label(self, mode, priority=None, em05=False):
        with patch.object(self.module, 'pc_app') as app, \
             patch.object(self.module, 'wan_manager') as wan, \
             patch.object(self.module, 'pcat_logger'):
            app.modem_client.is_em05.return_value = em05
            wan.get_priority.return_value = priority if priority is not None else table("wan", "cellular")
            return self.module.get_network_mode_label(mode)

    # ── priority mode: the label is the enabled failover chain ───────────────

    def test_all_three_slots_are_all_named(self):
        self.assertEqual(self.label("priority", table("wan", "wifi", "cellular")),
                         "Eth/WiFi/5G")

    def test_chain_follows_the_4g_modem(self):
        self.assertEqual(self.label("priority", table("wan", "wifi", "cellular"), em05=True),
                         "Eth/WiFi/4G")

    def test_disabled_slots_are_left_out(self):
        self.assertEqual(self.label("priority", table("wan", "cellular")), "Eth/5G")
        self.assertEqual(self.label("priority", table("wan", "wifi")), "Eth/WiFi")
        self.assertEqual(self.label("priority", table("wifi", "cellular")), "WiFi/5G")

    def test_single_slot_keeps_the_legacy_only_phrasing(self):
        self.assertEqual(self.label("priority", table("wan")), "Eth Only")
        self.assertEqual(self.label("priority", table("wifi")), "WiFi Only")
        self.assertEqual(self.label("priority", table("cellular")), "Cell Only")

    def test_empty_table_is_all_off(self):
        self.assertEqual(self.label("priority", table()), "All Off")

    def test_unreadable_table_falls_back_instead_of_raising(self):
        with patch.object(self.module, 'pc_app') as app, \
             patch.object(self.module, 'wan_manager') as wan, \
             patch.object(self.module, 'pcat_logger'):
            app.modem_client.is_em05.return_value = False
            wan.get_priority.side_effect = RuntimeError("uci down")
            self.assertEqual(self.module.get_network_mode_label("priority"), "Eth/5G")

    def test_widest_label_still_fits_the_mini_display_column(self):
        """"Eth/WiFi/5G" is the longest chain; page 4 gives the value 106px in
        the 15px Orbitron face (172 screen - 59 x - 7 margin). Longer strings
        get rendered as a scrolling ticker there, which reads badly for a mode.
        Approximated here as a character budget so the intent survives without
        the font: 11 chars measures 103px, 12 would overflow."""
        widest = self.label("priority", table("wan", "wifi", "cellular"))
        self.assertLessEqual(len(widest), 11)

    # ── legacy modes are untouched ──────────────────────────────────────────

    def test_legacy_modes(self):
        self.assertEqual(self.label("auto"), "Eth/5G")
        self.assertEqual(self.label("auto", em05=True), "Eth/4G")
        self.assertEqual(self.label("eth_only"), "Eth Only")
        self.assertEqual(self.label("cell_only"), "Cell Only")
        self.assertEqual(self.label("all_off"), "All Off")
        self.assertEqual(self.label("nonsense"), "Eth/5G")


if __name__ == '__main__':
    unittest.main()
