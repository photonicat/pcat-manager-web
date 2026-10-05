#!/usr/bin/env python3
"""Unit tests for the Quectel AT response parsers in app/pc_modem_client.py.

The fixtures are real responses from an RM520N-GL (FW RM520NGLAAR03A03M4G) on
a 5G NSA (EN-DC) network: LTE B3 anchor + NR n78 secondary cell group, and the
same cell with NSA disabled (LTE only). The SA fixture is the example from the
handler docstrings.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_modem_nsa_parsing`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

for _name in ("serial", "requests", "database", "pcat_config", "app",
              "pcat_util", "pcat_util.hardware_util", "pcat_util.pcat_logger"):
    sys.modules.setdefault(_name, MagicMock())

import pc_modem_client  # noqa: E402


NSA_QNWINFO = (
    b'AT+QNWINFO\r\r\n'
    b'+QNWINFO: "FDD LTE","40177","LTE BAND 3",1652\r\n'
    b'+QNWINFO: "TDD NR5G","40177","NR5G BAND 78",643322\r\n'
    b'\r\nOK\r\n'
)
NSA_QENG = (
    b'AT+QENG="servingcell"\r\r\n'
    b'+QENG: "servingcell","NOCONN"\r\n'
    b'+QENG: "LTE","FDD",401,77,1A3E90C,354,1652,3,5,5,271E,-84,-9,-54,18,9,-50,-\r\n'
    b'+QENG: "NR5G-NSA",401,77,828,-92,15,-11,643322,78,12,1\r\n'
    b'\r\nOK\r\n'
)
NSA_QCAINFO = (
    b'AT+QCAINFO\r\r\n'
    b'+QCAINFO: "PCC",1652,100,"LTE BAND 3",1,354,-84,-9,-54,16\r\n'
    b'+QCAINFO: "SCC",1850,100,"LTE BAND 3",1,354,-87,-10,-69,17,0,-,-\r\n'
    b'+QCAINFO: "SCC",500,100,"LTE BAND 1",1,354,-93,-11,-74,15,0,-,-\r\n'
    b'+QCAINFO: "SCC",125,75,"LTE BAND 1",1,354,-91,-9,-75,14,0,-,-\r\n'
    b'+QCAINFO: "SCC",643322,12,"NR5G BAND 78",828\r\n'
    b'\r\nOK\r\n'
)

LTE_QNWINFO = b'+QNWINFO: "FDD LTE","40177","LTE BAND 3",1652\r\n\r\nOK\r\n'
LTE_QENG = (
    b'+QENG: "servingcell","NOCONN","LTE","FDD",401,77,1A3E90C,354,1652,3,5,5,271E,'
    b'-93,-7,-66,15,8,130,-\r\n\r\nOK\r\n'
)
LTE_QCAINFO = (
    b'+QCAINFO: "PCC",1652,100,"LTE BAND 3",1,354,-93,-7,-67,11\r\n'
    b'+QCAINFO: "SCC",1850,100,"LTE BAND 3",1,354,-95,-9,-78,13,1,100,19850\r\n'
    b'\r\nOK\r\n'
)

SA_QNWINFO = b'+QNWINFO: "TDD NR5G","46011","NR5G BAND 78",627264\r\nOK\r\n'
SA_QENG = (
    b'+QENG: "servingcell","NOCONN","NR5G-SA","TDD",460,11,592E2E004,474,590903,'
    b'627264,78,12,-72,-10,13,1,55\r\n'
)
SA_QCAINFO = b'+QCAINFO: "PCC",627264,12,"NR5G BAND 78",593\r\nOK\r\n'


def make_client():
    client = pc_modem_client.PcModemClient.__new__(pc_modem_client.PcModemClient)
    client.basic = {}
    client.cmd_mark_failure = MagicMock()
    client.cmd_mark_success = MagicMock()
    client.is_em05 = lambda: False
    client.is_fm350 = lambda: False
    return client


def parse(qnwinfo, qeng, qcainfo):
    client = make_client()
    client.handle_qnwinfo(qnwinfo)
    client.handle_qeng_servingcell(qeng)
    client.handle_qcainfo(qcainfo)
    return client.basic


class NsaParsingTests(unittest.TestCase):
    def setUp(self):
        self.basic = parse(NSA_QNWINFO, NSA_QENG, NSA_QCAINFO)

    def test_qnwinfo_reports_5g(self):
        self.assertEqual(self.basic["cell_tech"], "5G")
        self.assertTrue(self.basic["cell_nr5g_nsa"])
        self.assertEqual(self.basic["cell_network_mode"], "EN-DC (NSA)")
        self.assertEqual(self.basic["network_info"], "FDD LTE BAND 3 + TDD NR5G BAND 78")
        self.assertEqual(self.basic["cell_band"], "LTE BAND 3 + NR5G BAND 78")

    def test_servingcell_is_populated(self):
        self.assertEqual(self.basic["serving_rat"], "NR5G-NSA")
        self.assertEqual(self.basic["serving_pci"], 828)
        self.assertEqual(self.basic["serving_band"], 78)
        self.assertEqual(self.basic["serving_bw_mhz"], "100 MHz")
        self.assertEqual(self.basic["serving_frequency"], "3649.830 MHz")
        self.assertEqual(self.basic["serving_rsrp"], "-92 dBm")
        self.assertEqual(self.basic["serving_cellid"], "1A3E90C")
        self.assertEqual(self.basic["serving_tac"], "271E")
        self.assertEqual(self.basic["serving_lte_anchor"]["pci"], 354)
        self.assertEqual(
            self.basic["modem_serving_info"],
            "EN-DC: LTE B3 PCI 354 + NR n78 PCI 828, Cell ID = 1A3E90C, TAC = 271E",
        )
        self.assertTrue(self.basic["modem_serving_quality"].startswith("NR: 15 dB, -11 dB, -92 dBm"))

    def test_qcainfo_reads_all_carriers(self):
        self.assertEqual(self.basic["ca_pci"], "354")
        self.assertEqual(self.basic["ca_tech"], "4G")
        self.assertEqual(len(self.basic["ca_scc_list"]), 4)
        self.assertEqual(self.basic["ca_scc_list"][-1]["tech"], "5G")
        self.assertEqual(
            self.basic["cell_carrier_info"],
            "Primary CC, 1850 MHz @ 20 MHz BW | +3 LTE SCC, + NR5G BAND 78 3650 MHz @ 100 MHz",
        )

    def test_repeated_polls_do_not_accumulate(self):
        client = make_client()
        for _ in range(3):
            client.handle_qcainfo(NSA_QCAINFO)
        self.assertEqual(len(client.basic["ca_scc_list"]), 4)
        self.assertEqual(client.basic["cell_carrier_info"].count("|"), 1)


class LteOnlyParsingTests(unittest.TestCase):
    def setUp(self):
        self.basic = parse(LTE_QNWINFO, LTE_QENG, LTE_QCAINFO)

    def test_qnwinfo(self):
        self.assertEqual(self.basic["cell_tech"], "4G")
        self.assertFalse(self.basic["cell_nr5g_nsa"])
        self.assertEqual(self.basic["network_info"], "FDD LTE BAND 3")

    def test_servingcell_uses_lte_layout(self):
        self.assertEqual(self.basic["serving_rat"], "LTE")
        self.assertEqual(self.basic["serving_pci"], 354)
        self.assertEqual(self.basic["serving_arfcn"], 1652)
        self.assertEqual(self.basic["serving_band"], 3)
        self.assertEqual(self.basic["serving_tac"], "271E")
        self.assertEqual(self.basic["serving_bw_mhz"], "20 MHz")

    def test_qcainfo(self):
        self.assertEqual(self.basic["ca_pci"], "354")
        self.assertEqual(self.basic["cell_carrier_info"], "Primary CC, 1850 MHz @ 20 MHz BW | +1 LTE SCC")


class SaParsingTests(unittest.TestCase):
    def setUp(self):
        self.basic = parse(SA_QNWINFO, SA_QENG, SA_QCAINFO)

    def test_sa(self):
        self.assertEqual(self.basic["cell_tech"], "5G")
        self.assertEqual(self.basic["network_info"], "TDD NR5G BAND 78")
        self.assertEqual(self.basic["serving_rat"], "NR5G-SA")
        self.assertEqual(self.basic["serving_pci"], 474)
        self.assertEqual(self.basic["serving_tac"], "590903")
        self.assertEqual(self.basic["serving_frequency"], "3408.960 MHz")
        self.assertEqual(self.basic["ca_pci"], "593")
        self.assertEqual(self.basic["ca_tech"], "5G")
        self.assertEqual(self.basic["cell_carrier_info"], "Primary CC, 3409 MHz @ 100 MHz BW")


if __name__ == "__main__":
    unittest.main()
