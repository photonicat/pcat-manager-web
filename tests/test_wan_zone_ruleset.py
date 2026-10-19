#!/usr/bin/env python3
"""Unit tests for the firewall zone-ruleset drift guard in wan_manager.

fw4 resolves zone networks to device names when it loads, so a Wi-Fi STA that
associates (or roams) after the load leaves wan_wifi in no zone: LAN clients
keep DNS but lose all routed egress. check_zone_ruleset() is the keeper-side
repair; these tests pin down when it must reload the firewall and, just as
important, when it must not (healthy ruleset, unreadable nft, ubus hiccup,
reload cooldown).

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_wan_zone_ruleset`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

# A ruleset as `nft list table inet fw4` prints it: device names appear quoted.
RULESET_WITHOUT_STA = '''table inet fw4 {
	chain forward {
		iifname "br-lan" jump forward_lan
		iifname { "eth0", "eth2" } jump forward_wan
	}
}'''
RULESET_WITH_STA = RULESET_WITHOUT_STA.replace('"eth2"', '"eth2", "phy1-sta0"')

# zone -> networks, mirroring the stock photonicat firewall config.
ZONES = {0: ("lan", "lan"), 1: ("wan", "wan wan_wifi wwan_fm350")}

IFACE_STATUS = {
    "lan": {"up": True, "l3_device": "br-lan"},
    "wan": {"up": False},                              # cable out: no device
    "wan_wifi": {"up": True, "l3_device": "phy1-sta0"},
    "wwan_fm350": {"up": True, "l3_device": "eth2"},
}


def fake_uci_get(key):
    for idx, (name, networks) in ZONES.items():
        if key == "firewall.@zone[{}].name".format(idx):
            return name
        if key == "firewall.@zone[{}].network".format(idx):
            return networks
    return ""


def fake_ubus(path, method):
    iface = path.replace("network.interface.", "")
    return IFACE_STATUS.get(iface, {})


def nft_result(text, returncode=0):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = text.encode()
    return proc


class ZoneRulesetTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import wan_manager
        self.wm = wan_manager
        self.wm._zone_ruleset_last_reload = 0.0
        # ruleset text (or Exception) returned for the `nft list` call; the
        # firewall reload call is recorded instead of executed.
        self.ruleset = RULESET_WITH_STA
        self.reloads = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "nft":
                if isinstance(self.ruleset, Exception):
                    raise self.ruleset
                return nft_result(self.ruleset)
            self.reloads.append(cmd)
            return MagicMock(returncode=0)

        self.patches = [
            patch.object(self.wm, 'uci_get', side_effect=fake_uci_get),
            patch.object(self.wm, 'ubus_call_strict', side_effect=fake_ubus),
            patch.object(self.wm.subprocess, 'run', side_effect=fake_run),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.module_patch.stop()

    def test_healthy_ruleset_no_reload(self):
        self.assertEqual(self.wm.check_zone_ruleset(), [])
        self.assertEqual(self.reloads, [])

    def test_missing_sta_device_reloads(self):
        self.ruleset = RULESET_WITHOUT_STA
        missing = self.wm.check_zone_ruleset()
        self.assertEqual(missing, ["wan_wifi(phy1-sta0)"])
        self.assertEqual(len(self.reloads), 1)
        self.assertEqual(self.reloads[0], ["/etc/init.d/firewall", "reload"])

    def test_down_interface_is_not_missing(self):
        # `wan` is down (cable out): its absent device must not trigger drift.
        self.ruleset = RULESET_WITH_STA.replace('"eth0", ', '"')
        self.assertNotIn("wan(eth0)", self.wm._zone_ruleset_missing())

    def test_ubus_hiccup_skips_interface(self):
        self.ruleset = RULESET_WITHOUT_STA
        with patch.object(self.wm, 'ubus_call_strict', return_value=None):
            self.assertEqual(self.wm.check_zone_ruleset(), [])
        self.assertEqual(self.reloads, [])

    def test_nft_unavailable_is_silent(self):
        # Dev machine / nft missing: never crash, never reload.
        self.ruleset = FileNotFoundError("nft")
        self.assertEqual(self.wm.check_zone_ruleset(), [])
        self.assertEqual(self.reloads, [])

    def test_empty_ruleset_means_fw4_not_loaded(self):
        # Mid-reload or firewall stopped: repairing would race the real load.
        self.ruleset = ""
        self.assertEqual(self.wm.check_zone_ruleset(), [])
        self.assertEqual(self.reloads, [])

    def test_reload_cooldown(self):
        self.ruleset = RULESET_WITHOUT_STA
        self.wm.check_zone_ruleset()
        # Drift persists (reload "didn't take"): the cooldown must keep the
        # keeper from turning into a firewall reload loop, while the drift is
        # still reported to the caller.
        missing = self.wm.check_zone_ruleset()
        self.assertEqual(missing, ["wan_wifi(phy1-sta0)"])
        self.assertEqual(len(self.reloads), 1)


if __name__ == '__main__':
    unittest.main()
