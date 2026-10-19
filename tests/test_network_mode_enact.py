#!/usr/bin/env python3
"""Unit tests for the boot-time network mode enact (app.config_ethernet_wwan).

This is the one call that pushes the saved network mode back onto the uplink
interfaces at every boot. In priority mode it must NOT run: the priority table
owns network.wan/wan6 and the modem interfaces, and set_network_mode_config()
deliberately reports both uplinks as enabled for 'priority' (the daemon must
not stop the modem when the table ranks cellular above ethernet). Enacting that
re-enabled a slot the user had turned off, silently, on every reboot -- a
cellular-only box came back routing out ethernet again.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_network_mode_enact`.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

import app as pc_app


class ConfigEthernetWwanTests(unittest.TestCase):

    def setUp(self):
        self.wan = patch.object(pc_app, "enable_wan_interface",
                                return_value=False).start()
        self.wwan = patch.object(pc_app, "enable_wwan_interface",
                                 return_value=False).start()
        self.addCleanup(patch.stopall)

    def test_priority_mode_leaves_the_table_alone(self):
        """The regression: the table owns these interfaces. Touching them here
        re-enables a slot the user disabled, and races the App thread's
        wan_manager.startup() apply()."""
        pc_app.config_ethernet_wwan("priority", at_boot=True)

        self.wan.assert_not_called()
        self.wwan.assert_not_called()

    def test_priority_mode_is_skipped_outside_boot_too(self):
        """Same ownership rule on any later call, not just the boot path."""
        pc_app.config_ethernet_wwan("priority", at_boot=False)

        self.wan.assert_not_called()
        self.wwan.assert_not_called()

    def test_legacy_modes_still_enact_their_uplinks(self):
        """A device that has not migrated yet must keep behaving exactly as
        before -- the guard is for 'priority' only."""
        for mode, wan_on, wwan_on in (
                ("auto", True, True),
                ("eth_only", True, False),
                ("cell_only", False, True),
                ("smart_wan", True, True),
                ("all_off", False, False),
        ):
            with self.subTest(mode=mode):
                self.wan.reset_mock()
                self.wwan.reset_mock()

                pc_app.config_ethernet_wwan(mode, at_boot=True)

                self.wan.assert_called_once_with(wan_on, restart=False)
                self.wwan.assert_called_once_with(wwan_on)

    def test_at_boot_suppresses_the_network_restart(self):
        """OpenWrt's init has just brought the network up; restarting it here
        tears down an upstream that was already coming up."""
        pc_app.config_ethernet_wwan("auto", at_boot=False)
        self.wan.assert_called_once_with(True, restart=True)


if __name__ == "__main__":
    unittest.main()
