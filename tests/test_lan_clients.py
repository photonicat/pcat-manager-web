#!/usr/bin/env python3
"""Unit tests for app/pcat_util/lan_clients.py -- the /stats device table.

The table used to show only Wi-Fi stations, which is empty on a wired-only
deployment (78 DHCP clients, no radio). It now lists every DHCP lease with
its Wi-Fi association. These tests pin the lease-file parsing (IPv6/duid
lines must not become rows) and the merge/sort rules.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_lan_clients`.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

from pcat_util import lan_clients  # noqa: E402

LEASES = """\
1788539453 6e:c6:a8:03:33:d5 192.168.31.175 * 01:6e:c6:a8:03:33:d5
1788539248 a8:fe:9d:42:79:a0 192.168.34.163 meichonendeip13 01:a8:fe:9d:42:79:a0
1788539199 1c:1b:0d:2a:f9:d3 192.168.8.20 SC-201803291549 01:1c:1b:0d:2a:f9:d3
1788539200 12345678 fd00::1234 v6host 00:01:00:01:aa:bb
duid 00:01:00:01:2a:5b:3c:4d:00:11:22:33:44:55
garbage line
"""


class ParseLeasesTests(unittest.TestCase):
    def test_ipv4_leases_parsed(self):
        leases = lan_clients.parse_dhcp_leases(LEASES)
        self.assertEqual([l["ip"] for l in leases],
                         ["192.168.31.175", "192.168.34.163", "192.168.8.20"])
        self.assertEqual(leases[1]["name"], "meichonendeip13")
        self.assertEqual(leases[1]["expiry"], 1788539248)

    def test_star_hostname_becomes_empty(self):
        leases = lan_clients.parse_dhcp_leases(LEASES)
        self.assertEqual(leases[0]["name"], "")

    def test_ipv6_duid_and_garbage_lines_skipped(self):
        # A DHCPv6 lease has an IAID where the MAC goes; the trailing duid
        # line has two fields. Neither may surface as a device row.
        leases = lan_clients.parse_dhcp_leases(LEASES)
        self.assertEqual(len(leases), 3)
        self.assertNotIn("fd00::1234", [l["ip"] for l in leases])

    def test_mac_lowercased_and_bad_expiry_tolerated(self):
        leases = lan_clients.parse_dhcp_leases("x AA:BB:CC:DD:EE:FF 10.0.0.2 host *\n")
        self.assertEqual(leases, [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.2",
                                   "name": "host", "expiry": 0}])

    def test_empty_file(self):
        self.assertEqual(lan_clients.parse_dhcp_leases(""), [])


class MergeClientsTests(unittest.TestCase):
    def setUp(self):
        self.leases = lan_clients.parse_dhcp_leases(LEASES)

    def test_rows_sorted_by_ip_numerically(self):
        rows = lan_clients.merge_clients(self.leases, [])
        # 192.168.8.20 sorts before 192.168.31.175 numerically (a string sort
        # would put "192.168.31" first).
        self.assertEqual([r["ip"] for r in rows],
                         ["192.168.8.20", "192.168.31.175", "192.168.34.163"])

    def test_wifi_flag_from_station_list_case_insensitive(self):
        rows = lan_clients.merge_clients(self.leases, ["A8:FE:9D:42:79:A0"])
        by_mac = {r["mac"]: r for r in rows}
        self.assertTrue(by_mac["a8:fe:9d:42:79:a0"]["wifi"])
        self.assertFalse(by_mac["6e:c6:a8:03:33:d5"]["wifi"])

    def test_station_without_lease_appended_last_with_empty_ip(self):
        rows = lan_clients.merge_clients(self.leases, ["de:ad:be:ef:00:01"])
        self.assertEqual(rows[-1], {"mac": "de:ad:be:ef:00:01", "ip": "", "name": "",
                                    "expiry": 0, "wifi": True})
        self.assertEqual(len(rows), 4)

    def test_duplicate_mac_keeps_newest_lease(self):
        leases = [
            {"mac": "aa:aa:aa:aa:aa:aa", "ip": "10.0.0.5", "name": "old", "expiry": 100},
            {"mac": "aa:aa:aa:aa:aa:aa", "ip": "10.0.0.9", "name": "new", "expiry": 200},
            {"mac": "aa:aa:aa:aa:aa:aa", "ip": "10.0.0.1", "name": "older", "expiry": 50},
        ]
        rows = lan_clients.merge_clients(leases, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip"], "10.0.0.9")
        self.assertEqual(rows[0]["name"], "new")

    def test_nothing_at_all(self):
        self.assertEqual(lan_clients.merge_clients([], []), [])


if __name__ == "__main__":
    unittest.main()
