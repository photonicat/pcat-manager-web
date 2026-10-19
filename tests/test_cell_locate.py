#!/usr/bin/env python3
"""Serving-cell parsing and tower lookup for the /gps map.

The QENG lines here are real RM520N output captured on hardware (SIM-less,
LIMSRV state). The lookup tests pin the one rule that keeps the feature
honest: BeaconDB's IP-geolocation fallback (25 km "accuracy") must count as
a miss, never be drawn as a tower.

    python3 -m unittest tests.test_cell_locate -v
"""

import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app")))

from pcat_util import pcat_logger  # noqa: E402
pcat_logger.logger = MagicMock()

from pcat_util import cell_locate  # noqa: E402

LTE_LINE = ('+QENG: "servingcell","LIMSRV","LTE","TDD",520,04,299F99,345,'
            '41490,41,5,5,1973,-97,-16,-60,8,0,-,30\nOK')
WCDMA_LINE = ('+QENG: "servingcell","LIMSRV","WCDMA",520,05,15E2,9C5A7EB,'
              '2987,78,183,-65,-6,-,-,-,-,-\nOK')
NR_LINE = ('+QENG: "servingcell","NOCONN","NR5G-SA","TDD",520,04,27A2B01,'
           '512,1973,633984,78,12,-88,-11,18,1,40\nOK')


class ParseServingTest(unittest.TestCase):
    def test_lte(self):
        c = cell_locate.parse_serving(LTE_LINE)
        self.assertEqual(c["tech"], "LTE")
        self.assertEqual(c["state"], "LIMSRV")
        self.assertEqual(c["mcc"], 520)
        self.assertEqual(c["mnc"], 4)
        self.assertEqual(c["cid"], 0x299F99)
        self.assertEqual(c["tac"], 0x1973)
        self.assertEqual(c["rsrp"], -97)
        self.assertEqual(c["band"], "B41")

    def test_wcdma(self):
        c = cell_locate.parse_serving(WCDMA_LINE)
        self.assertEqual(c["tech"], "WCDMA")
        self.assertEqual(c["mcc"], 520)
        self.assertEqual(c["mnc"], 5)
        self.assertEqual(c["tac"], 0x15E2)
        self.assertEqual(c["cid"], 0x9C5A7EB)
        self.assertEqual(c["rsrp"], -65)

    def test_nr(self):
        c = cell_locate.parse_serving(NR_LINE)
        self.assertEqual(c["tech"], "NR5G-SA")
        self.assertEqual(c["cid"], 0x27A2B01)
        self.assertEqual(c["tac"], 0x1973)
        self.assertEqual(c["band"], "n78")
        self.assertEqual(c["rsrp"], -88)

    def test_searching_and_garbage_are_none(self):
        self.assertIsNone(cell_locate.parse_serving(
            '+QENG: "servingcell","SEARCH"\nOK'))
        self.assertIsNone(cell_locate.parse_serving("ERROR"))
        self.assertIsNone(cell_locate.parse_serving(""))
        self.assertIsNone(cell_locate.parse_serving(None))


class FakeHttp:
    """Monkeypatched urllib.request.urlopen returning canned JSON bodies."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, req, timeout=0):
        self.urls.append(req if isinstance(req, str) else req.full_url)
        return io.BytesIO(json.dumps(self.responses.pop(0)).encode())


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.cell = cell_locate.parse_serving(LTE_LINE)
        self._orig = cell_locate.urllib.request.urlopen
        self.addCleanup(setattr, cell_locate.urllib.request,
                        "urlopen", self._orig)
        cell_locate._cache.clear()
        self.addCleanup(cell_locate._cache.clear)

    def test_beacondb_cell_hit_is_a_tower(self):
        cell_locate.urllib.request.urlopen = FakeHttp([
            {"location": {"lat": 12.95, "lng": 100.89}, "accuracy": 800}])
        t = cell_locate.resolve(self.cell)
        self.assertEqual(t, {"lat": 12.95, "lon": 100.89,
                             "accuracy": 800, "source": "beacondb"})

    def test_ip_fallback_is_a_miss_not_a_tower(self):
        # The exact failure mode seen live: unknown cell -> ipf guess, 25 km.
        cell_locate.urllib.request.urlopen = FakeHttp([
            {"location": {"lat": 12.93, "lng": 100.88}, "accuracy": 25000,
             "fallback": "ipf"}])
        self.assertIsNone(cell_locate.resolve(self.cell))

    def test_opencellid_tried_first_when_key_set(self):
        fake = FakeHttp([{"lat": "12.951", "lon": "100.891", "range": "500"}])
        cell_locate.urllib.request.urlopen = fake
        t = cell_locate.resolve(self.cell, key="abc123")
        self.assertEqual(t["source"], "opencellid")
        self.assertEqual(t["accuracy"], 500)
        self.assertIn("opencellid.org", fake.urls[0])

    def test_opencellid_miss_falls_back_to_beacondb(self):
        cell_locate.urllib.request.urlopen = FakeHttp([
            {"error": "cell not found"},
            {"location": {"lat": 1.0, "lng": 2.0}, "accuracy": 900}])
        t = cell_locate.resolve(self.cell, key="abc123")
        self.assertEqual(t["source"], "beacondb")

    def test_hits_cache_without_a_second_lookup(self):
        fake = FakeHttp([{"location": {"lat": 1.0, "lng": 2.0}, "accuracy": 900}])
        cell_locate.urllib.request.urlopen = fake
        cell_locate.resolve(self.cell)
        cell_locate.resolve(self.cell)
        self.assertEqual(len(fake.urls), 1)

    def test_incomplete_identity_is_refused(self):
        self.assertIsNone(cell_locate.resolve(None))
        self.assertIsNone(cell_locate.resolve({"mcc": 520, "mnc": 4,
                                               "tac": None, "cid": 1,
                                               "tech": "LTE"}))


class OfflineDbTest(unittest.TestCase):
    """The offline SQLite DB answers first — keyless, no internet."""

    def setUp(self):
        import sqlite3
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.addCleanup(os.unlink, self.tmp.name)
        con = sqlite3.connect(self.tmp.name)
        con.execute("CREATE TABLE cells (radio TEXT, mcc INT, net INT,"
                    " area INT, cell INT, unit INT, lat REAL, lon REAL,"
                    " range INT, samples INT)")
        con.executemany("INSERT INTO cells VALUES (?,?,?,?,?,?,?,?,?,?)", [
            # LTE cell under its 2021 TAC (differs from today's)
            ("LTE", 520, 4, 999, 0x299F99, 345, 12.951, 100.891, 700, 12),
            # WCDMA cell, exact area match, two rows with sample counts
            ("UMTS", 520, 5, 0x15E2, 0x9C5A7EB, 78, 12.940, 100.885, 900, 3),
            ("UMTS", 520, 5, 0x15E2, 0x9C5A7EB, 78, 12.941, 100.886, 800, 25),
            # a far-away tower that a nearby query must not return
            ("GSM", 520, 5, 3131, 42987, None, 13.715, 100.476, 715, 125),
        ])
        con.commit()
        con.close()
        self._orig = cell_locate.celldb_path
        cell_locate.celldb_path = lambda: self.tmp.name
        self.addCleanup(setattr, cell_locate, "celldb_path", self._orig)
        cell_locate._cache.clear()
        self.addCleanup(cell_locate._cache.clear)

    def test_resolves_without_any_network(self):
        def no_net(*a, **kw):
            raise AssertionError("offline lookup must not hit the network")
        orig = cell_locate.urllib.request.urlopen
        cell_locate.urllib.request.urlopen = no_net
        self.addCleanup(setattr, cell_locate.urllib.request, "urlopen", orig)
        t = cell_locate.resolve(cell_locate.parse_serving(WCDMA_LINE))
        self.assertEqual(t["source"], "offline-db")
        # two candidate rows: the most-sampled one wins
        self.assertEqual(t["lat"], 12.941)

    def test_tac_renumbering_still_finds_the_cell(self):
        # Live TAC is 0x1973 but the DB row was surveyed under TAC 999:
        # the stable cell id must still match.
        t = cell_locate.resolve(cell_locate.parse_serving(LTE_LINE))
        self.assertEqual(t["source"], "offline-db")
        self.assertEqual(t["lat"], 12.951)

    def test_lte_sibling_sector_resolves_the_same_mast(self):
        # Serving sector 0x299F99+1 isn't surveyed, but its eNodeB sibling
        # is: same tower, resolved with the enb source tag.
        cell = dict(cell_locate.parse_serving(LTE_LINE), cid=0x299F99 + 1)
        t = cell_locate.resolve(cell)
        self.assertEqual(t["source"], "offline-db-enb")
        self.assertEqual(t["lat"], 12.951)

    def test_nearby_towers_bbox_and_ordering(self):
        towers = cell_locate.nearby_towers(12.949, 100.889, radius_m=3000)
        # the two Pattaya rows, most-sampled first; Bangkok excluded
        self.assertEqual(len(towers), 3)
        self.assertEqual(towers[0]["samples"], 25)
        self.assertEqual(towers[0]["unit"], 78)
        self.assertTrue(all(t["lat"] < 13 for t in towers))

    def test_nearby_without_db_is_none(self):
        cell_locate.celldb_path = lambda: None
        self.assertIsNone(cell_locate.nearby_towers(12.9, 100.9))

    def test_db_miss_falls_through_to_web(self):
        cell = dict(cell_locate.parse_serving(LTE_LINE), cid=12345)
        orig = cell_locate.urllib.request.urlopen
        self.addCleanup(setattr, cell_locate.urllib.request, "urlopen", orig)
        cell_locate.urllib.request.urlopen = FakeHttp([
            {"location": {"lat": 1.0, "lng": 2.0}, "accuracy": 900}])
        t = cell_locate.resolve(cell)
        self.assertEqual(t["source"], "beacondb")


if __name__ == "__main__":
    unittest.main()
