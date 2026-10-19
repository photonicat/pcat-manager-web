#!/usr/bin/env python3
"""Track store: per-segment stats and the GPX exports.

Stats (distance / top speed) are computed once when a segment is indexed so
the tracks table never decompresses anything; entries written before stats
existed are upgraded lazily. The day export combines every segment of one
UTC day into a single <trk> with one <trkseg> per segment — the gaps between
segments are real (receiver off, reboot) and must not be drawn across.

    python3 -m unittest tests.test_gps_track_export -v
"""

import os
import sys
import json
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app")))
sys.modules.setdefault("serial", MagicMock())

from pcat_util import pcat_logger  # noqa: E402
pcat_logger.logger = MagicMock()

import pc_gps_logger  # noqa: E402
from pc_gps_logger import GpsLogger, FORMATS, HEADER, MAGIC, _full_meta  # noqa: E402

LAT0 = 100000000        # 10.0 deg * 1e7
STEP = 10000            # 0.001 deg ~ 111.32 m of latitude


def blob(fmt, records):
    out = HEADER.pack(MAGIC, FORMATS[fmt]["code"])
    for r in records:
        out += FORMATS[fmt]["rec"].pack(*r)
    return out


def std_records(t0=1_700_000_000, n=3, spd10=(0, 250, 120)):
    # (t, lat7, lon7, alt, speed*10): n points marching north ~111 m apart
    return [(t0 + i * 60, LAT0 + i * STEP, 1000000000, 100, spd10[i % len(spd10)])
            for i in range(n)]


class FullMetaTest(unittest.TestCase):
    def test_distance_and_top_speed(self):
        meta = _full_meta(blob("standard", std_records()))
        self.assertEqual(meta["points"], 3)
        # two ~111.32 m legs
        self.assertAlmostEqual(meta["dist_m"], 223, delta=2)
        self.assertEqual(meta["max_kmh"], 25.0)   # max(spd10)=250 -> 25 km/h
        self.assertEqual(meta["start_t"], 1_700_000_000)
        self.assertEqual(meta["end_t"], 1_700_000_120)

    def test_minimal_format_has_no_speed(self):
        recs = [(1_700_000_000 + i, LAT0 + i * STEP, 0) for i in range(3)]
        meta = _full_meta(blob("minimal", recs))
        self.assertIsNone(meta["max_kmh"])
        self.assertAlmostEqual(meta["dist_m"], 223, delta=2)

    def test_empty_blob_is_none(self):
        self.assertIsNone(_full_meta(b""))
        self.assertIsNone(_full_meta(HEADER.pack(MAGIC, 1)))


class TrackStoreHarness(unittest.TestCase):
    """A logger whose store is a temp dir, with no live sampling thread."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lg = GpsLogger()
        self.lg._dir = lambda: self.tmp.name
        self.lg.flush_pending = lambda: None

    def write_seg(self, seg_id, records, fmt="standard"):
        path = os.path.join(self.tmp.name, f"trk-{seg_id}.bin")
        with open(path, "wb") as f:
            f.write(blob(fmt, records))
        return path


class ListTracksStatsTest(TrackStoreHarness):
    def test_index_miss_computes_stats(self):
        self.write_seg("20260816-030000", std_records())
        rows = self.lg.list_tracks()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["dist_m"], 223, delta=2)
        self.assertEqual(rows[0]["max_kmh"], 25.0)
        # ...and the result was cached into the index for next time.
        with open(os.path.join(self.tmp.name, "index.json")) as f:
            idx = json.load(f)
        self.assertIn("dist_m", idx["20260816-030000"])

    def test_pre_stats_index_entry_is_upgraded(self):
        self.write_seg("20260816-030000", std_records())
        with open(os.path.join(self.tmp.name, "index.json"), "w") as f:
            json.dump({"20260816-030000": {   # entry from before stats existed
                "points": 3, "start_t": 1, "end_t": 2}}, f)
        rows = self.lg.list_tracks()
        self.assertAlmostEqual(rows[0]["dist_m"], 223, delta=2)
        # the true start_t replaced the stale one during the upgrade
        self.assertEqual(rows[0]["start_t"], 1_700_000_000)


class DayExportTest(TrackStoreHarness):
    def setUp(self):
        super().setUp()
        self.write_seg("20260816-030000", std_records(t0=1_700_000_000))
        self.write_seg("20260816-100000", std_records(t0=1_700_020_000, n=2))
        self.write_seg("20260817-010000", std_records(t0=1_700_100_000))

    def test_day_selection_is_a_sorted_prefix_match(self):
        self.assertEqual(self.lg.day_seg_ids("20260816"),
                         ["20260816-030000", "20260816-100000"])
        self.assertEqual(self.lg.day_seg_ids("20260818"), [])
        self.assertEqual(self.lg.day_seg_ids("bogus!"), [])
        self.assertEqual(self.lg.day_seg_ids(None), [])

    def test_day_gpx_is_one_trk_with_a_trkseg_per_segment(self):
        xml = "".join(self.lg.export_day_gpx_iter("20260816"))
        self.assertEqual(xml.count("<trk>"), 1)
        self.assertEqual(xml.count("<trkseg>"), 2)
        self.assertEqual(xml.count("</trkseg>"), 2)
        self.assertEqual(xml.count("<trkpt"), 5)     # 3 + 2 points
        self.assertIn("photonicat 20260816", xml)
        self.assertTrue(xml.rstrip().endswith("</gpx>"))

    def test_day_without_tracks_is_none(self):
        self.assertIsNone(self.lg.export_day_gpx_iter("20260818"))

    def test_single_track_export_still_works(self):
        xml = "".join(self.lg.export_gpx_iter("20260817-010000"))
        self.assertEqual(xml.count("<trkseg>"), 1)
        self.assertEqual(xml.count("<trkpt"), 3)
        self.assertIsNone(self.lg.export_gpx_iter("20991231-000000"))


if __name__ == "__main__":
    unittest.main()
