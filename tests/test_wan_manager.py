#!/usr/bin/env python3
"""Unit tests for the metric-based multi-WAN priority manager (wan_manager).

This module decides which uplink actually carries the customer's traffic: it
writes the route-metric ladder, demotes dead uplinks, heals tables an older
build corrupted, and keeps dialers from silently stealing egress. A wrong
metric write here does not crash anything -- it strands a router on 5G, or
blackholes all LAN egress after an ethernet unplug, with the UI still looking
green. These tests pin the ladder values, the failover/restore transitions,
and every repair path against an in-memory UCI tree so they run anywhere
(no device, no ubus, no radios).

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_wan_manager`.
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

from pcat_util import wan_manager as wm
from pcat_util import wifi_wan


def table(*on):
    """Priority table in the fixed slot order with `on` slots enabled."""
    return [{"id": s, "enabled": s in on} for s in wm.SLOTS]


def network_tree():
    """UCI network config with every ladder interface defined (stock device)."""
    values = {}
    for pairs in wm.SLOT_IFACES.values():
        for v4, v6 in pairs:
            values["network.{}".format(v4)] = "interface"
            values["network.{}".format(v6)] = "interface"
    return values


class FakeUci(object):
    """In-memory stand-in for the device's UCI tree (flat 'pkg.sec.opt' keys)."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.commits = []

    def get(self, key):
        return self.values.get(key, "")

    def set(self, assignment):
        key, _, val = assignment.partition("=")
        self.values[key] = val
        return True

    def commit(self, pkg=None):
        self.commits.append(pkg)
        return True

    def add_list(self, assignment):
        key, _, val = assignment.partition("=")
        cur = self.values.get(key, "")
        self.values[key] = (cur + " " + val).strip()
        return True

    def delete(self, key):
        for k in [k for k in self.values if k == key or k.startswith(key + ".")]:
            del self.values[k]
        return True

    def show_dict(self, pkg):
        return {k: v for k, v in self.values.items()
                if k.startswith(pkg + ".")}


class FakeConfig(object):
    """Stand-in for pcat_config (imported lazily inside wan_manager)."""

    def __init__(self):
        self.keys = {}
        self.network_mode = "priority"
        self.wifi_wan_radio = "onboard"

    def get_user_config_key(self, key, fallback=None):
        return self.keys.get(key, fallback)

    def set_user_config_key(self, key, value):
        self.keys[key] = value

    def get_network_mode(self):
        return self.network_mode

    def set_network_mode(self, mode):
        self.network_mode = mode

    def get_wifi_wan_radio(self):
        return self.wifi_wan_radio


class FakeApp(object):
    """Stand-in for the top-level app module (`import app as _app`)."""

    def __init__(self):
        self.invalidations = 0
        self.active_egress = None

    def invalidate_active_egress_cache(self):
        self.invalidations += 1

    def get_active_egress(self):
        if isinstance(self.active_egress, Exception):
            raise self.active_egress
        return self.active_egress


class LegacyApp(object):
    """An app build that predates invalidate_active_egress_cache()."""

    def __init__(self):
        self._active_egress_cache = {"time": 123.0}


class WanManagerTestBase(unittest.TestCase):

    def setUp(self):
        self.uci = FakeUci(network_tree())
        self.cfg = FakeConfig()
        self.app_mod = FakeApp()
        self.commands = []        # every subprocess.run invocation
        self.ubus_calls = []      # every ubus call (path, method)
        self.iface_status = {}    # iface -> ubus status dict (None = hiccup)
        self.network_reload_result = {}
        self.nft_output = ""      # "" -> fw4 not loaded, zone check no-ops
        self.route_json = ""
        self.fail_cmds = set()    # progs whose subprocess.run raises
        self.present_paths = set()
        self.carrier = True       # what the (patched) carrier check reports

        # Originals for the few functions we both patch and test directly.
        self._orig_carrier = wm._netdev_carrier

        self._saved_modules = {}
        for name, mod in (("pcat_config", self.cfg), ("app", self.app_mod)):
            self._saved_modules[name] = sys.modules.get(name)
            sys.modules[name] = mod

        self._reset_module_state()

        self.patches = [
            patch.object(wm, "uci_get", self.uci.get),
            patch.object(wm, "uci_set", self.uci.set),
            patch.object(wm, "uci_commit", self.uci.commit),
            patch.object(wm, "uci_add_list", self.uci.add_list),
            patch.object(wm, "uci_delete", self.uci.delete),
            patch.object(wm, "uci_show_dict", self.uci.show_dict),
            patch.object(wm, "ubus_call", self.fake_ubus),
            patch.object(wm, "ubus_call_strict", self.fake_ubus),
            patch.object(wm, "pcat_logger", MagicMock()),
            patch.object(wm.subprocess, "run", self.fake_run),
            patch.object(wm.os.path, "exists", self.fake_exists),
            patch.object(wm, "_netdev_carrier", lambda dev: self.carrier),
        ]
        for p in self.patches:
            p.start()

        # wan_manager must never cascade into real wifi_wan logic: stub every
        # function it reaches for and record the calls.
        self.ww = {}
        for name, ret in (
                ("set_sta_enabled", None),
                ("set_sta_radio", None),
                ("is_configured", True),
                ("is_available", True),
                ("is_sta_enabled", False),
                ("connectivity_state", "online"),
                ("promote_if_connected", False),
                ("nudge_rejoin", None),
                ("_phy_for_radio", None),
                ("_probe_connectivity", "online"),
                ("portal_url", ""),
        ):
            m = MagicMock(return_value=ret)
            p = patch.object(wifi_wan, name, m)
            p.start()
            self.patches.append(p)
            self.ww[name] = m

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for name, orig in self._saved_modules.items():
            if orig is not None:
                sys.modules[name] = orig
            else:
                sys.modules.pop(name, None)
        self._reset_module_state()

    @staticmethod
    def _reset_module_state():
        # Shared module globals: other suites import the same module object.
        wm._health.clear()
        wm._portal_login.update({"slot": "", "until": 0.0, "started": 0.0})
        wm._mwan3_handled = False
        wm._zone_ruleset_last_reload = 0.0

    # ── fakes ────────────────────────────────────────────────────────────────

    def fake_run(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        if cmd[0] in self.fail_cmds:
            raise OSError("boom: {}".format(cmd[0]))
        proc = MagicMock()
        proc.returncode = 0
        if cmd[0] == "nft":
            if isinstance(self.nft_output, Exception):
                raise self.nft_output
            proc.stdout = self.nft_output.encode()
        elif cmd[0] == "ip":
            proc.stdout = self.route_json
        else:
            proc.stdout = b""
        return proc

    def fake_ubus(self, path, method, *args, **kwargs):
        self.ubus_calls.append((path, method))
        if path == "network" and method == "reload":
            return self.network_reload_result
        if path.startswith("network.interface.") and method == "status":
            return self.iface_status.get(path.replace("network.interface.", ""))
        return {}

    def fake_exists(self, path):
        return path in self.present_paths

    # ── helpers ──────────────────────────────────────────────────────────────

    def seed_ladder(self, priority=None):
        """Write the intended metric ladder into the fake UCI tree so
        reassert_metrics() sees no drift."""
        pri = priority if priority is not None else wm.get_priority()
        for iface, metric in wm.metrics_for(pri).items():
            self.uci.values["network.{}.metric".format(iface)] = str(metric)

    def metric(self, iface):
        return self.uci.values.get("network.{}.metric".format(iface))


# ── the priority table ───────────────────────────────────────────────────────

class PriorityTableTests(WanManagerTestBase):

    def test_fresh_install_enables_the_two_builtin_uplinks(self):
        """Out of the box, ethernet and cellular carry traffic; the WiFi
        upstream stays off until the user joins an upstream network."""
        self.assertEqual(wm.default_priority(), table("wan", "cellular"))

    def test_legacy_mode_migration_keeps_the_old_behaviour(self):
        """A device upgrading from a fixed mode must come up routing exactly
        as it did before the upgrade -- eth_only stays cellular-free, the
        retired smart_wan keeps its eth>wifi>cell chain."""
        self.assertEqual(wm.priority_from_mode("auto"), table("wan", "cellular"))
        self.assertEqual(wm.priority_from_mode("eth_only"), table("wan"))
        self.assertEqual(wm.priority_from_mode("cell_only"), table("cellular"))
        self.assertEqual(wm.priority_from_mode("smart_wan"),
                         table("wan", "wifi", "cellular"))
        self.assertEqual(wm.priority_from_mode("all_off"), table())

    def test_stored_table_is_snapped_back_to_the_fixed_order(self):
        """A hand-edited or corrupted stored table cannot reorder failover:
        only the enable flags survive normalization, bogus slots are dropped,
        duplicates keep the first flag, missing slots come out disabled."""
        self.cfg.keys["wan_priority"] = [
            {"id": "cellular", "enabled": True},
            {"id": "bogus", "enabled": True},
            {"id": "cellular", "enabled": False},   # duplicate: first wins
            None,                                    # junk entry
        ]
        self.assertEqual(wm.get_priority(), table("cellular"))

    def test_unsaved_smart_wan_device_still_gets_its_wifi_slot(self):
        """A legacy smart_wan device that never saved the new table must keep
        its WiFi uplink after the upgrade (seeded from the raw stored mode)."""
        self.cfg.keys["network_mode"] = "smart_wan"
        self.assertEqual(wm.get_priority(), table("wan", "wifi", "cellular"))

    def test_is_slot_enabled(self):
        self.cfg.keys["wan_priority"] = table("wan")
        self.assertTrue(wm.is_slot_enabled("wan"))
        self.assertFalse(wm.is_slot_enabled("cellular"))


# ── retiring the legacy fixed modes ──────────────────────────────────────────

class LegacyModeMigrationTests(WanManagerTestBase):

    def test_auto_device_migrates_onto_the_table(self):
        """The regression this exists for: migration used to be gated on the
        mode already reading as 'priority', which only caught smart_wan boxes.
        A device on the default 'auto' never migrated -- it ran the legacy path
        forever, with no ladder and no failover keeper."""
        self.cfg.keys["network_mode"] = "auto"
        self.cfg.network_mode = "auto"

        order = wm.migrate_legacy_mode()

        self.assertEqual(order, "wan>wifi(off)>cellular")
        self.assertEqual(self.cfg.keys["wan_priority"], table("wan", "cellular"))
        self.assertEqual(self.cfg.network_mode, "priority")
        # The ladder is actually laid down, not just recorded.
        self.assertEqual(self.metric("wan"), "10")
        self.assertEqual(self.metric("wwan_lte"), "32")

    def test_every_legacy_mode_keeps_routing_the_way_it_did(self):
        """Migration must not change where traffic goes: each retired mode
        lands on the table that routes identically."""
        for mode, expected in (
                ("auto", table("wan", "cellular")),
                ("eth_only", table("wan")),
                ("cell_only", table("cellular")),
                ("smart_wan", table("wan", "wifi", "cellular")),
                ("all_off", table()),
        ):
            with self.subTest(mode=mode):
                self.cfg.keys.pop("wan_priority", None)  # un-migrated again
                self.cfg.keys["network_mode"] = mode
                self.cfg.network_mode = mode

                wm.migrate_legacy_mode()

                self.assertEqual(self.cfg.keys["wan_priority"], expected)
                self.assertEqual(self.cfg.network_mode, "priority")

    def test_cell_only_box_comes_up_with_ethernet_still_off(self):
        """A cell-only device must not silently regain an ethernet uplink on
        the upgrade -- the wan interfaces stay disabled through the migration."""
        self.cfg.keys["network_mode"] = "cell_only"
        self.cfg.network_mode = "cell_only"

        wm.migrate_legacy_mode()

        self.assertEqual(self.uci.get("network.wan.disabled"), "1")
        self.assertEqual(self.uci.get("network.wan6.disabled"), "1")
        self.assertEqual(self.uci.get("network.wwan_lte.disabled"), "0")

    def test_a_stored_table_is_never_overwritten(self):
        """The stored table is the marker. Once the user has a table -- edited
        or migrated -- a later boot must not reset it from the stale mode."""
        self.cfg.keys["network_mode"] = "auto"
        self.cfg.keys["wan_priority"] = table("cellular")

        self.assertIsNone(wm.migrate_legacy_mode())
        self.assertEqual(self.cfg.keys["wan_priority"], table("cellular"))

    def test_migration_is_a_no_op_on_the_second_boot(self):
        """Idempotent: the reboot after the upgrade must not re-commit or
        bounce interfaces, because apply() persisted the table the first time."""
        self.cfg.keys["network_mode"] = "auto"
        wm.migrate_legacy_mode()
        self.uci.commits.clear()
        self.ubus_calls.clear()

        self.assertIsNone(wm.migrate_legacy_mode())
        self.assertNotIn("network", self.uci.commits)
        self.assertNotIn(("network", "reload"), self.ubus_calls)


# ── the metric scheme ────────────────────────────────────────────────────────

class MetricSchemeTests(WanManagerTestBase):

    def test_ladder_values_follow_the_fixed_slot_order(self):
        """The kernel picks the lowest metric, so these exact numbers ARE the
        failover policy: wan 10/11 beats wifi 20/21 beats cellular 30..35."""
        pri = table("wan", "wifi", "cellular")
        self.assertEqual(wm.slot_metrics("wan", pri), {"wan": 10, "wan6": 11})
        self.assertEqual(wm.slot_metrics("wifi", pri),
                         {"wan_wifi": 20, "wan_wifi_v6": 21})
        self.assertEqual(wm.slot_metrics("cellular", pri),
                         {"wwan_5g": 30, "wwan_5g_v6": 31,
                          "wwan_lte": 32, "wwan_lte_v6": 33,
                          "wwan_fm350": 34, "wwan_fm350_v6": 35})

    def test_demoted_ladder_sits_below_every_healthy_slot(self):
        """+1000 keeps a dead uplink's routes installed (so restore is
        instant) while every healthy slot outranks it."""
        pri = table("wan", "cellular")
        self.assertEqual(wm.slot_metrics("wan", pri, demoted=True),
                         {"wan": 1010, "wan6": 1011})

    def test_unknown_slot_yields_no_metrics(self):
        self.assertEqual(wm.slot_metrics("wan", []), {})

    def test_metrics_for_covers_every_slot_interface(self):
        metrics = wm.metrics_for(table("wan", "wifi", "cellular"))
        self.assertEqual(len(metrics), 10)
        self.assertEqual(metrics["wan"], 10)
        self.assertEqual(metrics["wwan_fm350_v6"], 35)

    def test_wifi_metrics_keep_the_historic_values_in_legacy_modes(self):
        """Upgraded devices still in auto/smart_wan must keep the proven
        5/6/50 wifi metrics until they opt into the table."""
        self.cfg.network_mode = "auto"
        self.assertEqual(wm.wifi_metrics(), (5, 6, 50))

    def test_wifi_metrics_follow_the_table_in_priority_mode(self):
        self.cfg.network_mode = "priority"
        self.assertEqual(wm.wifi_metrics(), (20, 21, 1020))


# ── mwan3 removal and DNS resilience ─────────────────────────────────────────

class Mwan3AndDnsTests(WanManagerTestBase):

    def test_mwan3_absent_is_a_quiet_no_op(self):
        self.assertFalse(wm.disable_mwan3())
        self.assertEqual(self.commands, [])

    def test_mwan3_present_is_stopped_and_disabled_once(self):
        """mwan3's fwmark rules blackhole ALL egress when its only tracked
        interface dies, so it must be taken out of the datapath -- but only
        once per process, not on every apply()."""
        self.present_paths.add("/etc/init.d/mwan3")
        self.assertTrue(wm.disable_mwan3())
        self.assertIn(["/etc/init.d/mwan3", "stop"], self.commands)
        self.assertIn(["/etc/init.d/mwan3", "disable"], self.commands)
        self.commands.clear()
        self.assertFalse(wm.disable_mwan3())
        self.assertEqual(self.commands, [])

    def test_mwan3_stop_failure_does_not_crash_the_caller(self):
        self.present_paths.add("/etc/init.d/mwan3")
        self.fail_cmds.add("/etc/init.d/mwan3")
        self.assertFalse(wm.disable_mwan3())

    def test_dns_all_servers_is_enabled_and_dnsmasq_reloaded(self):
        """Without all_servers a resolver learned from a dead uplink stalls
        every DNS lookup after failover."""
        self.uci.values["dhcp.@dnsmasq[0]"] = "dnsmasq"
        self.assertTrue(wm.ensure_dns_resilience())
        self.assertEqual(self.uci.get("dhcp.@dnsmasq[0].all_servers"), "1")
        self.assertIn("dhcp", self.uci.commits)
        self.assertIn(["/etc/init.d/dnsmasq", "reload"], self.commands)

    def test_dns_already_resilient_is_not_rewritten(self):
        self.uci.values["dhcp.@dnsmasq[0]"] = "dnsmasq"
        self.uci.values["dhcp.@dnsmasq[0].all_servers"] = "1"
        self.assertFalse(wm.ensure_dns_resilience())
        self.assertEqual(self.uci.commits, [])

    def test_no_dnsmasq_section_is_a_no_op(self):
        self.assertFalse(wm.ensure_dns_resilience())

    def test_dnsmasq_reload_failure_still_commits_the_option(self):
        self.uci.values["dhcp.@dnsmasq[0]"] = "dnsmasq"
        self.fail_cmds.add("/etc/init.d/dnsmasq")
        self.assertTrue(wm.ensure_dns_resilience())
        self.assertEqual(self.uci.get("dhcp.@dnsmasq[0].all_servers"), "1")


# ── UCI plumbing ─────────────────────────────────────────────────────────────

class UciPlumbingTests(WanManagerTestBase):

    def test_network_reload_prefers_ubus(self):
        wm._network_reload()
        self.assertIn(("network", "reload"), self.ubus_calls)
        self.assertEqual(self.commands, [])

    def test_network_reload_falls_back_to_init_script_when_ubus_is_down(self):
        """A metric change must still reach netifd when ubus is unreachable,
        or the saved table silently never takes effect."""
        self.network_reload_result = None
        wm._network_reload()
        self.assertIn(["/etc/init.d/network", "reload"], self.commands)

    def test_network_reload_fallback_failure_is_swallowed(self):
        self.network_reload_result = None
        self.fail_cmds.add("/etc/init.d/network")
        wm._network_reload()  # must not raise

    def test_ifup_failure_is_swallowed(self):
        self.fail_cmds.add("ifup")
        wm._ifup("wan")  # must not raise

    def test_wan_zone_lookup(self):
        self.uci.values["firewall.@zone[0].name"] = "lan"
        self.uci.values["firewall.@zone[1].name"] = "wan"
        self.assertEqual(wm._wan_zone_index(), 1)

    def test_wan_zone_missing_returns_none(self):
        self.uci.values["firewall.@zone[0].name"] = "lan"
        self.assertIsNone(wm._wan_zone_index())

    def test_wan_zone_search_gives_up_after_twenty_zones(self):
        for i in range(25):
            self.uci.values["firewall.@zone[{}].name".format(i)] = "guest"
        self.assertIsNone(wm._wan_zone_index())

    def test_disabling_a_nonexistent_iface_is_a_no_op(self):
        self.assertFalse(wm._set_iface_disabled("wan_ghost", True))

    def test_new_uplink_networks_are_added_to_the_wan_zone(self):
        """An uplink outside the wan zone is never masqueraded: clients get
        DHCP and DNS but no internet, so the zone add must happen and the
        firewall must be reloaded."""
        self.uci.values["firewall.@zone[0].name"] = "wan"
        self.uci.values["firewall.@zone[0].network"] = "wan wan6"
        self.assertTrue(wm._ensure_firewall_wan_zone(["wan_wifi", "wan_wifi_v6"]))
        self.assertEqual(self.uci.get("firewall.@zone[0].network"),
                         "wan wan6 wan_wifi wan_wifi_v6")
        self.assertIn("firewall", self.uci.commits)
        self.assertIn(["/etc/init.d/firewall", "reload"], self.commands)
        # Second call: already present, no commit/reload churn.
        self.uci.commits.clear()
        self.commands.clear()
        self.assertTrue(wm._ensure_firewall_wan_zone(["wan_wifi"]))
        self.assertEqual(self.uci.commits, [])
        self.assertEqual(self.commands, [])

    def test_missing_wan_zone_fails_loudly_but_safely(self):
        self.assertFalse(wm._ensure_firewall_wan_zone(["wan_wifi"]))

    def test_zone_add_survives_a_failing_firewall_reload(self):
        """The zone change is committed even if the reload errors; the next
        reload (or reboot) picks it up rather than losing the config."""
        self.uci.values["firewall.@zone[0].name"] = "wan"
        self.uci.values["firewall.@zone[0].network"] = "wan"
        self.fail_cmds.add("/etc/init.d/firewall")
        self.assertTrue(wm._ensure_firewall_wan_zone(["wan_wifi"]))
        self.assertEqual(self.uci.get("firewall.@zone[0].network"),
                         "wan wan_wifi")
        self.assertIn("firewall", self.uci.commits)

    def test_zone_ruleset_repair_survives_a_failing_firewall_reload(self):
        """If the reload itself errors the keeper must report the drift and
        carry on -- crashing here would kill the whole health thread."""
        self.uci.values["firewall.@zone[0].name"] = "wan"
        self.uci.values["firewall.@zone[0].network"] = "wan_wifi"
        self.iface_status["wan_wifi"] = {"up": True, "l3_device": "phy1-sta0"}
        self.nft_output = 'table inet fw4 {\n\tiifname "eth0" jump x\n}'
        self.fail_cmds.add("/etc/init.d/firewall")
        self.assertEqual(wm.check_zone_ruleset(), ["wan_wifi(phy1-sta0)"])


# ── legacy eth1-as-upstream cleanup ──────────────────────────────────────────

class LanUpstreamCleanupTests(WanManagerTestBase):

    def test_legacy_wan_lan_config_is_fully_unwound(self):
        """A device that once used the retired LAN-as-uplink feature must get
        eth1 back in its LAN bridge, or a whole switch port stays dead."""
        self.uci.values["network.wan_lan"] = "interface"
        self.uci.values["network.wan_lan_v6"] = "interface"
        self.uci.values["firewall.@zone[0].name"] = "wan"
        self.uci.values["firewall.@zone[0].network"] = "wan wan_lan wan_lan_v6"
        self.uci.values["network.@device[0]"] = "device"
        self.uci.values["network.@device[0].name"] = "br-lan"
        self.uci.values["network.@device[0].ports"] = "lan1"
        self.cfg.keys[wm._LAN_PORTS_BACKUP_KEY] = "lan1 eth1"

        self.assertTrue(wm._cleanup_lan_upstream())

        self.assertNotIn("network.wan_lan", self.uci.values)
        self.assertNotIn("network.wan_lan_v6", self.uci.values)
        self.assertIn(["uci", "del_list", "firewall.@zone[0].network=wan_lan"],
                      self.commands)
        self.assertIn("firewall", self.uci.commits)
        self.assertEqual(self.uci.get("network.@device[0].ports"), "lan1 eth1")
        self.assertIsNone(self.cfg.keys[wm._LAN_PORTS_BACKUP_KEY])

    def test_clean_device_is_untouched(self):
        self.assertFalse(wm._cleanup_lan_upstream())
        self.assertEqual(self.uci.commits, [])

    def test_ports_backup_without_br_lan_is_consumed_not_looped(self):
        """A backup that can no longer be applied must still be cleared, or
        every future apply() retries it forever."""
        self.cfg.keys[wm._LAN_PORTS_BACKUP_KEY] = "lan1 eth1"
        self.assertFalse(wm._cleanup_lan_upstream())
        self.assertIsNone(self.cfg.keys[wm._LAN_PORTS_BACKUP_KEY])

    def test_br_lan_device_index(self):
        self.uci.values["network.@device[0]"] = "device"
        self.uci.values["network.@device[0].name"] = "br-wan"
        self.uci.values["network.@device[1]"] = "device"
        self.uci.values["network.@device[1].name"] = "br-lan"
        self.assertEqual(wm._br_lan_device_index(), 1)
        self.uci.values["network.@device[1].name"] = "br-guest"
        self.assertIsNone(wm._br_lan_device_index())
        # And with ten devices none of which is br-lan, the search gives up.
        for i in range(10):
            self.uci.values["network.@device[{}]".format(i)] = "device"
            self.uci.values["network.@device[{}].name".format(i)] = "br-x"
        self.assertIsNone(wm._br_lan_device_index())

    def test_del_list_failure_does_not_abort_the_cleanup(self):
        self.uci.values["network.wan_lan"] = "interface"
        self.uci.values["firewall.@zone[0].name"] = "wan"
        self.uci.values["firewall.@zone[0].network"] = "wan wan_lan"
        self.fail_cmds.add("uci")
        self.assertTrue(wm._cleanup_lan_upstream())
        self.assertNotIn("network.wan_lan", self.uci.values)


# ── apply() ──────────────────────────────────────────────────────────────────

class ApplyTests(WanManagerTestBase):

    def test_default_apply_writes_the_full_ladder(self):
        """A fresh save must leave the box routing eth first, cellular second:
        ladder metrics in UCI, both slots enabled, netifd reloaded, and the
        modem interfaces nudged up (they have auto=0 and won't start on a
        bare reload)."""
        order = wm.apply()

        self.assertEqual(order, "wan>wifi(off)>cellular")
        self.assertEqual(self.metric("wan"), "10")
        self.assertEqual(self.metric("wan6"), "11")
        self.assertEqual(self.metric("wwan_5g"), "30")
        self.assertEqual(self.metric("wwan_fm350_v6"), "35")
        # wifi_wan owns wan_wifi's metric end to end; apply must not touch it.
        self.assertIsNone(self.metric("wan_wifi"))
        for iface in ("wan", "wan6", "wwan_5g", "wwan_lte", "wwan_fm350"):
            self.assertEqual(
                self.uci.get("network.{}.disabled".format(iface)), "0")
        self.assertIn("network", self.uci.commits)
        self.assertIn(("network", "reload"), self.ubus_calls)
        self.assertIn(("network.interface.wwan_5g", "up"), self.ubus_calls)
        self.assertIn(("network.interface.wwan_fm350_v6", "up"), self.ubus_calls)
        self.ww["set_sta_enabled"].assert_called_once_with(False)
        self.assertEqual(self.cfg.network_mode, "priority")

    def test_apply_is_idempotent(self):
        """Saving the same table twice must not bounce interfaces: netifd
        restarts every interface whose config changed, so a no-op save that
        still commits would drop the uplink for nothing."""
        wm.apply()
        self.uci.commits.clear()
        self.ubus_calls.clear()
        wm.apply()
        self.assertNotIn("network", self.uci.commits)
        self.assertNotIn(("network", "reload"), self.ubus_calls)

    def test_posted_table_is_persisted_normalized(self):
        wm.apply([{"id": "cellular", "enabled": True},
                  {"id": "junk", "enabled": True}])
        self.assertEqual(self.cfg.keys["wan_priority"], table("cellular"))

    def test_disabling_cellular_downs_the_modem_interfaces(self):
        """Turning the cellular slot off must actually stop the modem paying
        for data: interfaces disabled in UCI and ifdown'd via ubus."""
        wm.apply(table("wan"))
        for iface in ("wwan_5g", "wwan_lte", "wwan_fm350"):
            self.assertEqual(
                self.uci.get("network.{}.disabled".format(iface)), "1")
        self.assertIn(("network.interface.wwan_5g", "down"), self.ubus_calls)
        self.assertIn(("network.interface.wwan_lte_v6", "down"), self.ubus_calls)

    def test_wifi_slot_waits_for_two_radios_without_forgetting_the_user(self):
        """At boot the radios register after this process starts; the enable
        must be persisted anyway and the STA left alone -- the old build
        persisted the forced 'off' and erased the user's choice every boot."""
        wm.apply(table("wan", "wifi", "cellular"))  # no live radios in fake
        self.ww["set_sta_enabled"].assert_not_called()
        wifi = [e for e in self.cfg.keys["wan_priority"] if e["id"] == "wifi"][0]
        self.assertTrue(wifi["enabled"], "user's wifi enable was not persisted")

    def test_wifi_slot_enables_the_sta_when_both_radios_are_live(self):
        with patch.object(wm, "relay_radios_ready", return_value=True):
            wm.apply(table("wan", "wifi", "cellular"))
        self.ww["set_sta_enabled"].assert_called_once_with(True)

    def test_wifi_toggle_failure_does_not_abort_the_save(self):
        self.ww["set_sta_enabled"].side_effect = RuntimeError("hostapd gone")
        order = wm.apply(table("wan", "cellular"))
        self.assertEqual(order, "wan>wifi(off)>cellular")
        self.assertEqual(self.metric("wan"), "10")

    def test_radio_switch_is_forwarded_and_its_failure_contained(self):
        wm.apply(wifi_radio="pcie")
        self.ww["set_sta_radio"].assert_called_once_with("pcie")
        self.ww["set_sta_radio"].side_effect = RuntimeError("no phy")
        wm.apply(wifi_radio="onboard")  # must not raise

    def test_unchanged_slot_keeps_its_demotion_across_a_save(self):
        """Toggling one slot must not forgive another slot's dead-uplink
        verdict: a known-dead cellular link would otherwise get its preferred
        metric back and carry traffic for up to two probe rounds."""
        self.cfg.keys["wan_priority"] = table("wan", "cellular")
        wm._health["cellular"] = {"result": "offline", "bad_streak": 2,
                                  "demoted": True, "time": 0.0}
        wm.apply(table("wan", "cellular"))  # same enable flags
        self.assertEqual(self.metric("wwan_5g"), "1030")
        self.assertEqual(self.metric("wwan_fm350_v6"), "1035")
        self.assertIn("cellular", wm._health)

    def test_toggled_slot_sheds_its_stale_verdict(self):
        """Re-enabling a slot the user just cycled must start from a clean
        slate: its interface is coming up fresh, so the old verdict is
        meaningless and its preferred metric must be written."""
        self.cfg.keys["wan_priority"] = table("wan", "cellular")
        wm._health["cellular"] = {"result": "offline", "bad_streak": 2,
                                  "demoted": True, "time": 0.0}
        wm.apply(table("wan"))  # cellular flag changed
        self.assertNotIn("cellular", wm._health)
        self.assertEqual(self.metric("wwan_5g"), "30")


class SetSlotEnabledTests(WanManagerTestBase):

    def test_flipping_a_slot_reapplies_the_table(self):
        """Joining an upstream WiFi from the panel must turn the wifi slot on
        and enact it in one step."""
        with patch.object(wm, "apply") as apply_mock:
            self.assertTrue(wm.set_slot_enabled("wifi", True))
        apply_mock.assert_called_once_with(table("wan", "wifi", "cellular"))

    def test_no_op_flip_skips_the_apply(self):
        with patch.object(wm, "apply") as apply_mock:
            self.assertFalse(wm.set_slot_enabled("wan", True))
        apply_mock.assert_not_called()


# ── the one-shot wifi-slot heal + startup ────────────────────────────────────

class RepairWifiSlotTests(WanManagerTestBase):

    def _damaged_table(self):
        self.cfg.keys["wan_priority"] = table("wan", "cellular")  # wifi off

    def test_guard_damage_signature_re_enables_the_wifi_slot(self):
        """The exact upgrade scenario: priority mode, stored table with wifi
        off, upstream STA still configured -- the old boot guard did that, the
        user did not, so the slot comes back."""
        self._damaged_table()
        self.assertTrue(wm._repair_wifi_slot_disable())
        wifi = [e for e in self.cfg.keys["wan_priority"] if e["id"] == "wifi"][0]
        self.assertTrue(wifi["enabled"])
        self.assertTrue(self.cfg.keys[wm._WIFI_GUARD_REPAIR_MARKER])

    def test_repair_runs_once_ever(self):
        """After the marker is set a wifi slot turned off by the user must
        stay off forever -- re-running the heal would override them."""
        self._damaged_table()
        self.assertTrue(wm._repair_wifi_slot_disable())
        self.cfg.keys["wan_priority"] = table("wan", "cellular")  # user turns off
        self.assertFalse(wm._repair_wifi_slot_disable())
        wifi = [e for e in self.cfg.keys["wan_priority"] if e["id"] == "wifi"][0]
        self.assertFalse(wifi["enabled"])

    def test_no_upstream_configured_means_a_deliberate_disable(self):
        """A real 'forget upstream' deletes the STA section; without it the
        off flag is the user's choice and must be honoured."""
        self._damaged_table()
        self.ww["is_configured"].return_value = False
        self.assertFalse(wm._repair_wifi_slot_disable())

    def test_non_priority_device_is_left_alone_but_marked(self):
        self.cfg.network_mode = "auto"
        self._damaged_table()
        self.assertFalse(wm._repair_wifi_slot_disable())
        self.assertTrue(self.cfg.keys[wm._WIFI_GUARD_REPAIR_MARKER])

    def test_no_stored_table_means_nothing_to_heal(self):
        self.assertFalse(wm._repair_wifi_slot_disable())

    def test_wifi_already_enabled_needs_no_heal(self):
        self.cfg.keys["wan_priority"] = table("wan", "wifi", "cellular")
        self.assertFalse(wm._repair_wifi_slot_disable())


class StartupTests(WanManagerTestBase):

    def test_priority_device_reasserts_its_table_at_boot(self):
        with patch.object(wm, "apply") as apply_mock:
            wm.startup()
        apply_mock.assert_called_once_with()

    def test_legacy_device_boot_does_not_enact_the_table(self):
        """A device still in auto mode must not have priority metrics forced
        onto it at boot -- only mwan3/DNS hardening applies everywhere."""
        self.cfg.network_mode = "auto"
        self.uci.values["dhcp.@dnsmasq[0]"] = "dnsmasq"
        with patch.object(wm, "apply") as apply_mock:
            wm.startup()
        apply_mock.assert_not_called()
        self.assertEqual(self.uci.get("dhcp.@dnsmasq[0].all_servers"), "1")

    def test_repair_crash_does_not_stop_the_boot_apply(self):
        """The heal is best-effort; a bug in it must not leave the ladder
        unapplied (the box would boot with stale metrics)."""
        with patch.object(wm, "_repair_wifi_slot_disable",
                          side_effect=RuntimeError("bad json")), \
             patch.object(wm, "apply") as apply_mock:
            wm.startup()
        apply_mock.assert_called_once_with()

    def test_apply_crash_at_boot_is_contained(self):
        with patch.object(wm, "apply", side_effect=RuntimeError("uci down")):
            wm.startup()  # must not raise


# ── health primitives ────────────────────────────────────────────────────────

class HealthPrimitiveTests(WanManagerTestBase):

    def test_carrier_file_reads(self):
        with patch("builtins.open", mock_open(read_data="1\n")):
            self.assertTrue(self._orig_carrier("eth0"))
        with patch("builtins.open", mock_open(read_data="0\n")):
            self.assertFalse(self._orig_carrier("eth0"))
        # Sysfs read errors happen right as a cable is yanked: treat as down.
        with patch("builtins.open", side_effect=OSError("gone")):
            self.assertFalse(self._orig_carrier("eth0"))

    def test_slot_link_up(self):
        self.assertFalse(wm._slot_link_up("wan"))  # no eth0 at all
        self.present_paths.add("/sys/class/net/eth0")
        self.carrier = True
        self.assertTrue(wm._slot_link_up("wan"))
        self.carrier = False
        self.assertFalse(wm._slot_link_up("wan"))
        # Slots without a fixed netdev can't be judged here: assume up.
        self.assertTrue(wm._slot_link_up("cellular"))

    def test_slot_l3dev_reports_the_live_netdev(self):
        self.iface_status = {"wwan_5g": None,  # ubus hiccup on one member
                             "wwan_lte": {"up": True, "l3_device": "usb0"},
                             "wwan_fm350": {"up": False}}
        self.assertEqual(wm._slot_l3dev("cellular"), "usb0")

    def test_slot_l3dev_distinguishes_no_l3_from_ubus_down(self):
        """False (ubus unreachable) must never be conflated with None (no L3):
        demoting a live uplink on a ubus hiccup is a self-inflicted outage."""
        self.iface_status = {"wwan_5g": {"up": False},
                             "wwan_lte": {"up": False},
                             "wwan_fm350": {"up": False}}
        self.assertIsNone(wm._slot_l3dev("cellular"))
        self.iface_status = {}  # every status read fails
        self.assertIs(wm._slot_l3dev("cellular"), False)

    def test_demote_writes_metrics_bounces_ifaces_and_drops_the_ui_cache(self):
        pri = table("wan", "cellular")
        self.seed_ladder(pri)
        self.assertTrue(wm._set_slot_demoted("cellular", True, pri))
        self.assertEqual(self.metric("wwan_5g"), "1030")
        self.assertEqual(self.metric("wwan_fm350_v6"), "1035")
        self.assertIn("network", self.uci.commits)
        self.assertIn(["ifup", "wwan_5g"], self.commands)
        self.assertEqual(self.app_mod.invalidations, 1,
                         "dashboard egress cache kept the stale green dot")

    def test_demote_is_a_no_op_when_already_demoted(self):
        pri = table("wan", "cellular")
        self.seed_ladder(pri)
        wm._set_slot_demoted("cellular", True, pri)
        self.uci.commits.clear()
        self.commands.clear()
        self.assertFalse(wm._set_slot_demoted("cellular", True, pri))
        self.assertEqual(self.uci.commits, [])

    def test_demote_of_a_slot_missing_from_the_table_is_refused(self):
        self.assertFalse(wm._set_slot_demoted("cellular", True, []))

    def test_demote_supports_the_legacy_app_cache_shape(self):
        legacy = LegacyApp()
        sys.modules["app"] = legacy
        pri = table("wan", "cellular")
        self.seed_ladder(pri)
        wm._set_slot_demoted("wan", True, pri)
        self.assertEqual(legacy._active_egress_cache["time"], 0.0)

    def test_demote_survives_a_broken_app_module(self):
        sys.modules["app"] = None  # import app -> ImportError
        pri = table("wan", "cellular")
        self.seed_ladder(pri)
        self.assertTrue(wm._set_slot_demoted("wan", True, pri))

    def test_probe_delegates_to_wifi_wan(self):
        self.assertEqual(wm._probe("eth0"), "online")
        self.ww["_probe_connectivity"].assert_called_once_with("eth0", None)

    def test_active_slot_maps_the_kernel_egress(self):
        self.app_mod.active_egress = "wan"
        self.assertEqual(wm._active_slot(), "wan")
        self.app_mod.active_egress = "mobile"
        self.assertEqual(wm._active_slot(), "cellular")
        self.app_mod.active_egress = "wifi"
        self.assertIsNone(wm._active_slot())
        self.app_mod.active_egress = RuntimeError("no route cache")
        self.assertIsNone(wm._active_slot())


class HandoffToWifiTests(WanManagerTestBase):

    def test_connected_sta_is_promoted_over_cellular(self):
        """Cable out with the STA still associated: the user expects WiFi to
        take over, not a silent jump to metered 5G."""
        self.cfg.keys["wan_priority"] = table("wan", "wifi", "cellular")
        self.ww["promote_if_connected"].return_value = True
        wm._handoff_to_wifi("no_carrier")
        self.ww["promote_if_connected"].assert_called_once_with()
        self.ww["nudge_rejoin"].assert_not_called()

    def test_disconnected_sta_gets_an_eager_rejoin(self):
        self.cfg.keys["wan_priority"] = table("wan", "wifi", "cellular")
        self.ww["promote_if_connected"].return_value = False
        wm._handoff_to_wifi("no_carrier")
        self.ww["nudge_rejoin"].assert_called_once_with()

    def test_disabled_wifi_slot_is_never_touched(self):
        self.cfg.keys["wan_priority"] = table("wan", "cellular")
        wm._handoff_to_wifi("no_carrier")
        self.ww["promote_if_connected"].assert_not_called()
        self.ww["nudge_rejoin"].assert_not_called()

    def test_handoff_failure_is_contained(self):
        self.cfg.keys["wan_priority"] = table("wan", "wifi", "cellular")
        self.ww["promote_if_connected"].side_effect = RuntimeError("iw died")
        wm._handoff_to_wifi("no_carrier")  # must not raise


# ── metric drift correction ──────────────────────────────────────────────────

class ReassertMetricsTests(WanManagerTestBase):

    def setUp(self):
        super(ReassertMetricsTests, self).setUp()
        self.cfg.keys["wan_priority"] = table("wan", "wifi", "cellular")
        self.seed_ladder()

    def test_clean_ladder_is_left_alone(self):
        self.assertEqual(wm.reassert_metrics(), [])
        self.assertEqual(self.uci.commits, [])

    def test_dialer_rewrite_is_corrected_and_the_iface_bounced(self):
        """The legacy fm350-mm rewrite (metric=14) put 5G below WiFi's 20 and
        silently moved all egress onto cellular; the keeper must snap it back
        and ifup so the live route follows."""
        self.uci.values["network.wwan_fm350.metric"] = "14"
        self.assertEqual(wm.reassert_metrics(), ["wwan_fm350"])
        self.assertEqual(self.metric("wwan_fm350"), "34")
        self.assertIn(["ifup", "wwan_fm350"], self.commands)
        self.assertEqual(self.app_mod.invalidations, 1)

    def test_correction_respects_our_own_demotion(self):
        """Drift on a slot we hold demoted must be corrected to the demoted
        value -- restoring preferred would resurrect a dead uplink."""
        wm._health["cellular"] = {"result": "offline", "bad_streak": 2,
                                  "demoted": True, "time": 0.0}
        self.uci.values["network.wwan_fm350.metric"] = "14"
        self.assertEqual(wm.reassert_metrics(), ["wwan_fm350"])
        self.assertEqual(self.metric("wwan_fm350"), "1034")

    def test_demoted_value_is_not_drift(self):
        self.uci.values["network.wan.metric"] = "1010"
        self.assertEqual(wm.reassert_metrics(), [])

    def test_wifi_metric_belongs_to_wifi_wan(self):
        """wan_wifi's metric encodes wifi_wan's portal demotion; correcting it
        here would bounce egress back onto a captive-portal-locked WiFi."""
        self.uci.values["network.wan_wifi.metric"] = "999"
        self.assertEqual(wm.reassert_metrics(), [])

    def test_disabled_slot_is_not_corrected(self):
        self.cfg.keys["wan_priority"] = table("wan", "wifi")
        self.uci.values["network.wwan_fm350.metric"] = "14"
        self.assertEqual(wm.reassert_metrics(), [])

    def test_legacy_app_cache_shape_is_supported_here_too(self):
        legacy = LegacyApp()
        sys.modules["app"] = legacy
        self.uci.values["network.wwan_5g.metric"] = "7"
        wm.reassert_metrics()
        self.assertEqual(legacy._active_egress_cache["time"], 0.0)

    def test_missing_interface_is_skipped_not_created(self):
        """A board without the LTE modem variant must not have wwan_lte
        conjured into its config by the drift corrector."""
        self.uci.delete("network.wwan_lte")
        self.uci.delete("network.wwan_lte.metric")
        self.assertEqual(wm.reassert_metrics(), [])
        self.assertNotIn("network.wwan_lte", self.uci.values)
        self.assertNotIn("network.wwan_lte.metric", self.uci.values)

    def test_correction_survives_a_broken_app_module(self):
        sys.modules["app"] = None  # import app -> ImportError
        self.uci.values["network.wwan_5g.metric"] = "7"
        self.assertEqual(wm.reassert_metrics(), ["wwan_5g"])
        self.assertEqual(self.metric("wwan_5g"), "30")


# ── the fast carrier pass ────────────────────────────────────────────────────

class ReconcileLinkStateTests(WanManagerTestBase):

    def setUp(self):
        super(ReconcileLinkStateTests, self).setUp()
        self.cfg.keys["wan_priority"] = table("wan", "cellular")
        self.seed_ladder()
        self.present_paths.add("/sys/class/net/eth0")
        self.carrier = True

    def test_legacy_mode_is_untouched(self):
        self.cfg.network_mode = "auto"
        self.assertEqual(wm.reconcile_link_state(), "not_priority")

    def test_healthy_link_is_idle(self):
        self.assertEqual(wm.reconcile_link_state(), "idle")
        self.assertEqual(self.metric("wan"), "10")

    def test_cable_pull_demotes_eth_within_one_pass(self):
        """Unplugging the WAN cable must move egress off the stale eth route
        immediately -- waiting for two 30s probe rounds means up to a minute
        of blackhole."""
        self.carrier = False
        self.assertEqual(wm.reconcile_link_state(), "wan:no_carrier")
        self.assertEqual(self.metric("wan"), "1010")
        self.assertEqual(self.metric("wan6"), "1011")
        self.assertTrue(wm._health["wan"]["demoted"])

    def test_transition_reports_once_not_every_tick(self):
        self.carrier = False
        wm.reconcile_link_state()
        self.assertEqual(wm.reconcile_link_state(), "idle",
                         "cable-out spammed the log on every tick")

    def test_carrier_return_alone_does_not_restore(self):
        """Plugging the cable back must NOT restore preferred metrics: link-up
        with no internet (dead ISP port) would steal egress from a working
        slot. Restore belongs to the probe keeper."""
        self.carrier = False
        wm.reconcile_link_state()
        self.carrier = True
        wm.reconcile_link_state()
        self.assertEqual(self.metric("wan"), "1010")

    def test_metric_drift_is_reported_from_the_fast_pass(self):
        self.uci.values["network.wwan_fm350.metric"] = "14"
        self.assertEqual(wm.reconcile_link_state(),
                         "metric_drift:wwan_fm350")

    def test_cable_pull_and_drift_are_both_reported(self):
        self.carrier = False
        self.uci.values["network.wwan_fm350.metric"] = "14"
        summary = wm.reconcile_link_state()
        self.assertIn("wan:no_carrier", summary)
        self.assertIn("metric_drift:wwan_fm350", summary)


# ── the probe keeper ─────────────────────────────────────────────────────────

class ReconcileTests(WanManagerTestBase):

    def setUp(self):
        super(ReconcileTests, self).setUp()
        self.cfg.keys["wan_priority"] = table("wan", "cellular")
        self.seed_ladder()
        self.present_paths.add("/sys/class/net/eth0")
        self.carrier = True
        self.iface_status = {
            "wan": {"up": True, "l3_device": "eth0"},
            "wwan_5g": {"up": True, "l3_device": "usb0"},
            "wwan_lte": {"up": False},
            "wwan_fm350": {"up": False},
        }
        self.probe = patch.object(wm, "_probe", return_value="online")
        self.probe_mock = self.probe.start()
        self.patches.append(self.probe)

    def assert_probed(self, *netdevs):
        """Exactly these netdevs were probed, in order. _probe takes a second
        argument (the per-slot captive-portal URL record) that no test cares
        about, so compare on the netdev alone."""
        self.assertEqual([c.args[0] for c in self.probe_mock.call_args_list],
                         list(netdevs))

    def test_legacy_mode_is_untouched(self):
        self.cfg.network_mode = "auto"
        self.assertEqual(wm.reconcile(), "not_priority")

    def test_overlapping_round_is_skipped(self):
        """Two threads probing at once would race on _health and double the
        probe traffic; the second caller must bail out immediately."""
        self.assertTrue(wm._reconcile_active.acquire(blocking=False))
        try:
            self.assertEqual(wm.reconcile(), "busy")
        finally:
            wm._reconcile_active.release()

    def test_healthy_slots_probe_online(self):
        self.assertEqual(wm.reconcile(), "wan:online cellular:online")
        self.assertFalse(wm._health["wan"]["demoted"])
        self.assertEqual(self.metric("wan"), "10")

    def test_disabling_a_slot_clears_its_verdict(self):
        wm._health["cellular"] = {"result": "offline", "bad_streak": 2,
                                  "demoted": True, "time": 0.0}
        self.cfg.keys["wan_priority"] = table("wan")
        self.seed_ladder()
        wm.reconcile()
        self.assertNotIn("cellular", wm._health)

    def test_dead_carrier_is_down_without_probing(self):
        self.carrier = False
        self.assertEqual(wm.reconcile(), "wan:down cellular:online")
        self.assertEqual(self.metric("wan"), "1010")
        # The probe ran only for cellular; a dead link is never probed.
        self.assert_probed("usb0")

    def test_ubus_outage_changes_nothing(self):
        """When every status read fails the slot's state is unknown; demoting
        it would blackhole a live uplink over an IPC hiccup."""
        self.iface_status = {}
        summary = wm.reconcile()
        self.assertIn("wan:ubus_unavailable", summary)
        self.assertEqual(self.metric("wan"), "10")
        self.probe_mock.assert_not_called()

    def test_eth_without_l3_is_demoted(self):
        """eth0 with carrier but no address can hold a stale default route
        that blackholes traffic; it must be demoted, not probed."""
        self.iface_status["wan"] = {"up": False}
        summary = wm.reconcile()
        self.assertIn("wan:down", summary)
        self.assertEqual(self.metric("wan"), "1010")

    def test_undialed_modem_keeps_preferred_metrics(self):
        """A cellular slot with no L3 (modem not dialed yet) must keep its
        preferred metrics so the route is ready the moment it dials -- and a
        stale demotion from a previous life must be lifted."""
        wm._health["cellular"] = {"result": "offline", "bad_streak": 2,
                                  "demoted": True, "time": 0.0}
        wm._set_slot_demoted("cellular", True, wm.get_priority())
        self.iface_status["wwan_5g"] = {"up": False}
        summary = wm.reconcile()
        self.assertIn("cellular:down", summary)
        self.assertEqual(self.metric("wwan_5g"), "30")
        self.assertFalse(wm._health["cellular"]["demoted"])

    def test_one_bad_probe_does_not_move_egress(self):
        """Flap damping: a single dropped probe on a healthy slot must not
        bounce every LAN client onto the next uplink and back."""
        wm._health["wan"] = {"result": "online", "bad_streak": 0,
                             "demoted": False, "time": time.time()}
        self.app_mod.active_egress = "wan"
        self.probe_mock.return_value = "offline"
        summary = wm.reconcile()
        self.assertIn("wan:online", summary, "one bad probe already flapped")
        self.assertEqual(self.metric("wan"), "10")

    def test_sustained_failure_demotes_and_hands_off_to_wifi(self):
        self.cfg.keys["wan_priority"] = table("wan", "wifi", "cellular")
        self.seed_ladder()
        wm._health["wan"] = {"result": "online", "bad_streak": 1,
                             "demoted": False, "time": time.time()}
        self.app_mod.active_egress = "wan"
        self.ww["promote_if_connected"].return_value = True
        self.probe_mock.return_value = "offline"
        summary = wm.reconcile()
        self.assertIn("wan:offline", summary)
        self.assertEqual(self.metric("wan"), "1010")
        self.ww["promote_if_connected"].assert_called_once_with()

    def test_recovered_uplink_is_restored_to_preferred(self):
        wm._health["wan"] = {"result": "offline", "bad_streak": 2,
                             "demoted": True, "time": 0.0}
        self.uci.values["network.wan.metric"] = "1010"
        self.uci.values["network.wan6.metric"] = "1011"
        summary = wm.reconcile()
        self.assertIn("wan:online", summary)
        self.assertEqual(self.metric("wan"), "10")
        self.assertFalse(wm._health["wan"]["demoted"])

    def test_healthy_standby_is_not_probed_every_round(self):
        """A healthy non-active cellular slot only refreshes a badge; probing
        it every 30s wakes the modem radio for nothing. Its metrics stay at
        preferred, so skipping the probe cannot strand egress."""
        self.app_mod.active_egress = "wan"
        now = time.time()
        wm._health["wan"] = {"result": "online", "bad_streak": 0,
                             "demoted": False, "time": now}
        wm._health["cellular"] = {"result": "online", "bad_streak": 0,
                                  "demoted": False, "time": now}
        summary = wm.reconcile()
        self.assertEqual(summary, "wan:online cellular:online")
        self.assert_probed("eth0")  # active slot only

    def test_stale_standby_badge_is_refreshed(self):
        self.app_mod.active_egress = "wan"
        now = time.time()
        wm._health["wan"] = {"result": "online", "bad_streak": 0,
                             "demoted": False, "time": now}
        wm._health["cellular"] = {
            "result": "online", "bad_streak": 0, "demoted": False,
            "time": now - wm._STANDBY_PROBE_INTERVAL - 1}
        wm.reconcile()
        self.assertEqual(self.probe_mock.call_count, 2)

    def test_slot_toggled_mid_probe_drops_the_stale_verdict(self):
        """apply() can disable a slot while its probe is in flight; writing
        the stale verdict back would resurrect an orphaned state and pin the
        wrong metric."""
        def probe_and_toggle(l3dev, record=None):
            if l3dev == "eth0":
                wm._health.pop("wan", None)
                self.cfg.keys["wan_priority"] = table("cellular")
            return "online"
        self.probe_mock.side_effect = probe_and_toggle
        summary = wm.reconcile()
        self.assertIn("wan:skipped", summary)
        self.assertNotIn("wan", wm._health)

    def test_carrier_lost_mid_probe_keeps_the_demotion(self):
        """A probe that started before the cable was pulled must not report
        'online' into a link the fast pass just demoted."""
        wm._health["wan"] = {"result": "offline", "bad_streak": 2,
                             "demoted": True, "time": 0.0}
        self.uci.values["network.wan.metric"] = "1010"
        self.uci.values["network.wan6.metric"] = "1011"

        def probe_and_yank(l3dev, record=None):
            if l3dev == "eth0":
                self.carrier = False
            return "online"
        self.probe_mock.side_effect = probe_and_yank
        summary = wm.reconcile()
        self.assertIn("wan:down", summary)
        self.assertEqual(self.metric("wan"), "1010",
                         "stale online verdict restored a dead link")


# ── the captive-portal login window ──────────────────────────────────────────

class PortalLoginWindowTests(WanManagerTestBase):
    """The hotel deadlock: the WAN port sits behind a login page, so the keeper
    demotes it and egress moves to the already-signed-in upstream WiFi -- which
    is exactly why the login page can no longer be reached, since every request
    now leaves through the WiFi. Nobody can ever sign in.

    The window breaks the loop by pinning the portal-locked slot up for a
    bounded time. What these tests pin is the other half: it must come back
    down again -- on cancel, on timeout, when the cable dies -- because a
    window that fails to close strands the router on an uplink with no
    internet."""

    def setUp(self):
        super(PortalLoginWindowTests, self).setUp()
        self.cfg.keys["wan_priority"] = table("wan", "cellular")
        self.seed_ladder()
        self.present_paths.add("/sys/class/net/eth0")
        self.iface_status = {
            "wan": {"up": True, "l3_device": "eth0"},
            "wwan_5g": {"up": True, "l3_device": "usb0"},
        }
        self.probe = patch.object(wm, "_probe", return_value="online")
        self.probe_mock = self.probe.start()
        self.patches.append(self.probe)

    def portal_locked_wan(self):
        """The state the keeper leaves a portal-locked WAN port in."""
        wm._health["wan"] = {"result": "portal", "bad_streak": 2,
                             "demoted": True, "time": time.time()}
        self.uci.values["network.wan.metric"] = "1010"
        self.uci.values["network.wan6.metric"] = "1011"

    def probes(self, **verdicts):
        """Per-netdev probe verdicts; a 'portal' one also fills in the login
        URL the way the real probe does."""
        def probe(l3dev, record=None):
            verdict = verdicts.get(l3dev, "online")
            if verdict == "portal" and record is not None:
                record["url"] = "http://portal.hotel/login"
            return verdict
        self.probe_mock.side_effect = probe

    # ── opening ──────────────────────────────────────────────────────────────

    def test_start_hands_the_slot_back_its_preferred_metric(self):
        self.portal_locked_wan()
        state = wm.start_portal_login("wan")
        self.assertEqual(self.metric("wan"), "10")
        self.assertEqual(self.metric("wan6"), "11")
        self.assertFalse(wm._health["wan"]["demoted"])
        self.assertEqual(state["slot"], "wan")
        self.assertEqual(state["window"], 900)  # 15 minutes
        self.assertTrue(wm.portal_login_hold("wan"))
        self.assertFalse(wm.portal_login_hold("cellular"))

    def test_start_refuses_slots_with_nothing_to_log_in_to(self):
        self.assertRaises(ValueError, wm.start_portal_login, "nope")
        self.assertRaises(ValueError, wm.start_portal_login, "wifi")  # disabled
        self.present_paths.clear()                                   # no eth0
        self.assertRaises(ValueError, wm.start_portal_login, "wan")
        self.assertEqual(wm.portal_login_state()["slot"], "")

    def test_duration_is_clamped_to_a_sane_range(self):
        self.portal_locked_wan()
        self.assertEqual(wm.start_portal_login("wan", 99999)["window"],
                         wm._PORTAL_LOGIN_MAX)
        self.assertEqual(wm.start_portal_login("wan", 1)["window"],
                         wm._PORTAL_LOGIN_MIN)

    def test_a_second_window_supersedes_the_first(self):
        self.portal_locked_wan()
        self.present_paths.add("/sys/class/net/usb0")
        wm._health["cellular"] = {"result": "portal", "bad_streak": 2,
                                  "demoted": True, "time": time.time()}
        wm.start_portal_login("wan")
        wm.start_portal_login("cellular")
        self.assertFalse(wm.portal_login_hold("wan"))
        self.assertTrue(wm.portal_login_hold("cellular"))
        self.assertEqual(self.metric("wan"), "1010",
                         "the superseded slot must go back down")

    # ── holding ──────────────────────────────────────────────────────────────

    def test_keeper_leaves_a_held_slot_primary_while_it_still_fails(self):
        """The whole point: the probe still says 'portal' every round, and the
        slot must NOT be demoted for it -- otherwise the login page goes out of
        reach again mid-login."""
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        self.probes(eth0="portal")
        summary = wm.reconcile()
        self.assertIn("wan:portal", summary)
        self.assertEqual(self.metric("wan"), "10")
        self.assertFalse(wm._health["wan"]["demoted"])
        self.assertTrue(wm.portal_login_hold("wan"))

    def test_the_login_url_is_captured_per_slot(self):
        """The redirect the probe follows on eth0 is the WAN's login page; the
        panel offers it as a shortcut, so it must not be filed under the WiFi
        upstream's own portal episode."""
        self.portal_locked_wan()
        self.probes(eth0="portal")
        wm.reconcile()
        self.assertEqual(wm._health["wan"]["portal_url"],
                         "http://portal.hotel/login")
        rows = {r["id"]: r for r in wm.status()["priority"]}
        self.assertEqual(rows["wan"]["portal_url"], "http://portal.hotel/login")

    def test_status_carries_the_countdown(self):
        self.portal_locked_wan()
        wm.start_portal_login("wan", 600)
        state = wm.status()["portal_login"]
        self.assertEqual(state["slot"], "wan")
        self.assertTrue(0 < state["remaining"] <= 600)

    # ── closing ──────────────────────────────────────────────────────────────

    def test_a_successful_login_closes_the_window_and_keeps_the_uplink(self):
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        self.probes(eth0="online")
        wm.reconcile()
        self.assertEqual(wm.portal_login_state()["slot"], "")
        self.assertEqual(self.metric("wan"), "10")
        self.assertFalse(wm._health["wan"]["demoted"])

    def test_cancel_puts_the_uplink_back_down_at_once(self):
        """Giving up must not leave the LAN on a portal-locked uplink until the
        next keeper round."""
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        self.assertEqual(wm.end_portal_login("cancelled by user"), "wan")
        self.assertEqual(self.metric("wan"), "1010")
        self.assertTrue(wm._health["wan"]["demoted"])
        self.assertFalse(wm.portal_login_hold("wan"))

    def test_cancelling_a_slot_that_recovered_leaves_it_alone(self):
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        wm._health["wan"].update({"result": "online", "demoted": False})
        wm.end_portal_login("cancelled by user")
        self.assertEqual(self.metric("wan"), "10")

    def test_cancel_without_a_window_is_a_no_op(self):
        self.seed_ladder()
        self.assertEqual(wm.end_portal_login("nothing to do"), "")
        self.assertEqual(self.metric("wan"), "10")

    def test_timeout_reverts_on_the_fast_pass(self):
        """The 30s probe keeper can sit inside a dead uplink's timeouts, so the
        promise 'it switches back on its own' rides on the 5s pass instead."""
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        self.assertEqual(wm.reconcile_link_state(), "idle")  # still running
        wm._portal_login["until"] = time.time() - 0.1
        summary = wm.reconcile_link_state()
        self.assertIn("portal_login_expired:wan", summary)
        self.assertEqual(self.metric("wan"), "1010")
        self.assertFalse(wm.portal_login_hold("wan"))

    def test_expiry_alone_stops_the_hold_even_if_nothing_runs(self):
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        wm._portal_login["until"] = time.time() - 1
        self.assertFalse(wm.portal_login_hold("wan"))

    def test_a_pulled_cable_closes_the_window(self):
        """Nothing left to sign in to, and the demotion must not be held off by
        a window nobody can use."""
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        self.carrier = False
        wm.reconcile_link_state()
        self.assertFalse(wm.portal_login_hold("wan"))
        self.assertEqual(self.metric("wan"), "1010")
        self.assertEqual(wm._health["wan"]["result"], "down")

    def test_switching_the_slot_off_closes_the_window(self):
        self.portal_locked_wan()
        wm.start_portal_login("wan")
        wm.apply(table("cellular"))
        self.assertFalse(wm.portal_login_hold("wan"))


# ── radios / status for the UI ───────────────────────────────────────────────

class RadioClassTests(WanManagerTestBase):

    def _wireless(self, radio0_path=None, radio1_path=None):
        if radio0_path:
            self.uci.values["wireless.radio0.path"] = radio0_path
        if radio1_path:
            self.uci.values["wireless.radio1.path"] = radio1_path

    def test_no_radio_sections_means_no_relay(self):
        self.assertEqual(wm._radio_classes(), {"onboard": False, "pcie": False})
        self.assertFalse(wm.relay_radios_ready())

    def test_two_live_radios_enable_the_relay(self):
        self._wireless("platform/soc/sdio", "pci0000:00/pcie/0000:01:00.0")
        self.ww["_phy_for_radio"].side_effect = lambda r: "phy_" + r
        self.assertEqual(wm._radio_classes(), {"onboard": True, "pcie": True})
        self.assertTrue(wm.relay_radios_ready())

    def test_pulled_pcie_card_is_not_offered(self):
        """wireless.radio1 outlives the hardware; a section whose phy is gone
        must not count, or the relay parks the only real AP."""
        self._wireless("platform/soc/sdio", "pci0000:00/pcie/0000:01:00.0")
        self.ww["_phy_for_radio"].side_effect = (
            lambda r: "phy0" if r == "radio0" else None)
        self.assertEqual(wm._radio_classes(), {"onboard": True, "pcie": False})
        self.assertFalse(wm.relay_radios_ready())


class DefaultRouteMetricsTests(WanManagerTestBase):

    def test_lowest_metric_per_device_wins(self):
        self.route_json = json.dumps([
            {"dev": "eth0", "metric": 10},
            {"dev": "eth0", "metric": 3},       # lower duplicate wins
            {"dev": "", "metric": 1},           # no device: dropped
            {"dev": "usb0", "metric": "junk"},  # unparsable: treated as 0
            {"dev": "phy1-sta0"},               # missing: treated as 0
        ])
        self.assertEqual(wm._default_route_metrics(),
                         {"eth0": 3, "usb0": 0, "phy1-sta0": 0})

    def test_unparsable_ip_output_is_empty(self):
        self.route_json = "not json"
        self.assertEqual(wm._default_route_metrics(), {})

    def test_no_default_route_is_empty(self):
        self.route_json = ""
        self.assertEqual(wm._default_route_metrics(), {})


class StatusTests(WanManagerTestBase):

    def setUp(self):
        super(StatusTests, self).setUp()
        self.cfg.keys["wan_priority"] = table("wan", "wifi", "cellular")
        self.seed_ladder()
        self.present_paths.add("/sys/class/net/eth0")
        self.iface_status = {
            "wan": {"up": True, "l3_device": "eth0",
                    "ipv4-address": [{"address": "192.0.2.10"}]},
            "wan_wifi": {"up": True, "l3_device": "phy1-sta0",
                         "ipv4-address": [{"address": "10.0.0.9"}]},
        }
        self.route_json = json.dumps([{"dev": "eth0", "metric": 10}])

    def _rows(self):
        return {r["id"]: r for r in wm.status()["priority"]}

    def test_status_reflects_the_live_table(self):
        """The /api/v1/wan_priority.json panel is the user's only view into
        failover; it must show real IPs, real metrics and real health."""
        wm._health["wan"] = {"result": "online", "bad_streak": 0,
                             "demoted": False, "time": 0.0}
        self.ww["is_sta_enabled"].return_value = True
        result = wm.status()
        rows = {r["id"]: r for r in result["priority"]}

        self.assertEqual(result["mode"], "priority")
        self.assertEqual(result["wifi_radio"], "onboard")
        wan = rows["wan"]
        self.assertTrue(wan["up"])
        self.assertEqual(wan["ip"], "192.0.2.10")
        self.assertEqual(wan["device"], "eth0")
        self.assertEqual(wan["metric"], 10)
        self.assertEqual(wan["metric_uci"], 10)
        self.assertEqual(wan["metric_route"], 10)
        self.assertEqual(wan["health"], "online")
        self.assertTrue(wan["exists"])
        wifi = rows["wifi"]
        self.assertEqual(wifi["health"], "online")  # from wifi_wan's keeper
        self.assertTrue(wifi["exists"])  # wifi_wan.is_available()
        cell = rows["cellular"]
        self.assertFalse(cell["up"])
        self.assertEqual(cell["health"], "down")  # enabled, no state, not up

    def test_demoted_slot_reports_the_metric_actually_routing(self):
        """demoted:true next to metric 10 reads as a contradiction; the panel
        must show the +1000 the kernel is really using."""
        wm._health["wan"] = {"result": "offline", "bad_streak": 2,
                             "demoted": True, "time": 0.0}
        wan = self._rows()["wan"]
        self.assertTrue(wan["demoted"])
        self.assertEqual(wan["metric"], 1010)

    def test_wifi_demotion_is_read_back_from_uci(self):
        """wifi_wan demotes wan_wifi without touching _health; the panel must
        still show it demoted by recognizing the +1000 metric."""
        self.uci.values["network.wan_wifi.metric"] = "1020"
        self.ww["is_sta_enabled"].return_value = True
        wifi = self._rows()["wifi"]
        self.assertTrue(wifi["demoted"])
        self.assertEqual(wifi["metric"], 1020)

    def test_enabled_wifi_slot_with_sta_off_reads_down(self):
        self.ww["is_sta_enabled"].return_value = False
        self.assertEqual(self._rows()["wifi"]["health"], "down")

    def test_disabled_slots_read_unknown(self):
        self.cfg.keys["wan_priority"] = table("wan")
        self.seed_ladder()
        rows = self._rows()
        self.assertEqual(rows["wifi"]["health"], "unknown")
        self.assertEqual(rows["cellular"]["health"], "unknown")

    def test_status_survives_an_unreadable_network_config(self):
        def boom(pkg):
            if pkg == "network":
                raise RuntimeError("uci down")
            return self.uci.show_dict(pkg)
        with patch.object(wm, "uci_show_dict", boom):
            result = wm.status()
        self.assertEqual(len(result["priority"]), 3)

    def test_debug_rows_flag_dialer_drift(self):
        """The debug panel exists to catch a dialer stealing egress without
        SSH: a metric that is neither preferred nor demoted must be flagged."""
        self.uci.values["network.wwan_fm350.metric"] = "14"
        rows = {r["iface"]: r for r in wm.status()["iface_metrics"]}
        self.assertTrue(rows["wwan_fm350"]["drifted"])
        self.assertEqual(rows["wwan_fm350"]["preferred"], 34)
        self.assertEqual(rows["wwan_fm350"]["demoted_metric"], 1034)
        self.assertFalse(rows["wan"]["drifted"])
        self.assertEqual(rows["wan"]["device"], "eth0")
        self.assertEqual(rows["wan"]["route_metric"], 10)

    def test_debug_rows_omit_interfaces_this_board_does_not_have(self):
        self.uci.delete("network.wwan_lte")
        self.uci.delete("network.wwan_lte.metric")
        rows = {r["iface"] for r in wm.status()["iface_metrics"]}
        self.assertNotIn("wwan_lte", rows)
        self.assertIn("wwan_5g", rows)

    def test_non_numeric_uci_metric_is_passed_through_verbatim(self):
        """A hand-mangled metric string must show up as-is in the panel so it
        can be diagnosed, not be masked by an int() crash or a silent None."""
        self.uci.values["network.wan.metric"] = "junk"
        wan = self._rows()["wan"]
        self.assertEqual(wan["metric_uci"], "junk")

    def test_debug_rows_strip_alias_devices(self):
        """A '@wan_wifi' alias is not a netdev; matching it against the route
        table would be garbage, so it must be blanked."""
        self.uci.values["network.wan_wifi_v6.device"] = "@wan_wifi"
        rows = {r["iface"]: r for r in wm.status()["iface_metrics"]}
        self.assertEqual(rows["wan_wifi_v6"]["device"], "")
        self.assertIsNone(rows["wan_wifi_v6"]["route_metric"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
