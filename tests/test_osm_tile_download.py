#!/usr/bin/env python3
"""Region tile download: aria2 input generation, progress accounting, and
the post-pass that keeps non-PNG responses out of the store.

    python3 -m unittest tests.test_osm_tile_download -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app")))

from pcat_util import pcat_logger  # noqa: E402
pcat_logger.logger = MagicMock()

from pcat_util import osm_tiles  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class TileDirHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Pin the write store to the temp dir (the store layer itself is
        # covered by ActiveStoreTest).
        self._orig_active = osm_tiles.active_store
        osm_tiles.active_store = lambda refresh=False: {
            "dir": self.tmp.name, "key": "test", "kind": "internal",
            "cap_mb": 100}
        self.addCleanup(setattr, osm_tiles, "active_store", self._orig_active)
        self._orig_internal = osm_tiles.INTERNAL_DIR
        osm_tiles.INTERNAL_DIR = self.tmp.name
        self.addCleanup(setattr, osm_tiles, "INTERNAL_DIR", self._orig_internal)
        with osm_tiles._dl_lock:
            saved = dict(osm_tiles._dl_state)
        self.addCleanup(lambda: osm_tiles._dl_state.update(saved))

    def put(self, z, x, y, data=PNG, suffix=""):
        p = os.path.join(self.tmp.name, str(z), str(x), f"{y}.png{suffix}")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p


class Aria2InputTest(TileDirHarness):
    def test_every_upstream_is_a_mirror_per_tile(self):
        text = osm_tiles._aria2_input([(12, 3000, 1800)])
        lines = text.strip().split("\n")
        # first line: all upstreams for the tile, tab-separated
        uris = lines[0].split("\t")
        self.assertEqual(len(uris), len(osm_tiles.REMOTE_URLS))
        for u in uris:
            self.assertIn("/12/3000/1800.png", u)
        self.assertEqual(lines[1].strip(),
                         "dir=" + os.path.join(self.tmp.name, "12", "3000"))
        self.assertEqual(lines[2].strip(), "out=1800.png")

    def test_one_entry_per_tile(self):
        tiles = [(10, 1, 2), (10, 1, 3), (11, 4, 5)]
        text = osm_tiles._aria2_input(tiles)
        self.assertEqual(text.count("out="), 3)


class ScanTilesTest(TileDirHarness):
    def test_counts_complete_tiles_and_bytes(self):
        self.put(10, 1, 2)
        self.put(10, 1, 3)
        done, size = osm_tiles._scan_tiles([(10, 1, 2), (10, 1, 3), (10, 1, 4)])
        self.assertEqual(done, 2)
        self.assertEqual(size, 2 * len(PNG))

    def test_in_flight_tiles_are_not_counted(self):
        self.put(10, 1, 2)
        self.put(10, 1, 2, data=b"", suffix=".aria2")   # aria2 control file
        done, size = osm_tiles._scan_tiles([(10, 1, 2)])
        self.assertEqual((done, size), (0, 0))


class ProgressTest(unittest.TestCase):
    def setUp(self):
        with osm_tiles._dl_lock:
            self._saved = dict(osm_tiles._dl_state)
        self.addCleanup(lambda: osm_tiles._dl_state.update(self._saved))

    def test_eta_and_estimate_from_average_tile_size(self):
        # 100 of 400 tiles, 1 MiB so far, 100 KiB/s
        osm_tiles._update_progress(100, 1048576, 102400, 400)
        st = osm_tiles.download_status()
        self.assertEqual(st["done"], 100)
        self.assertEqual(st["bytes"], 1048576)
        self.assertEqual(st["est_bytes"], 4 * 1048576)
        # 300 remaining * ~10.5 KiB avg / 100 KiB/s ~= 31 s
        self.assertAlmostEqual(st["eta_s"], 31, delta=1)

    def test_no_samples_no_estimates(self):
        osm_tiles._update_progress(0, 0, 0, 400)
        st = osm_tiles.download_status()
        self.assertIsNone(st["est_bytes"])
        self.assertIsNone(st["eta_s"])


class NonPngPostPassTest(TileDirHarness):
    def test_error_pages_are_evicted_and_counted_failed(self):
        """A captive portal's 200 text/html must not sit in the store as a
        'tile'. Simulate the aria2 aftermath directly: one good PNG, one HTML
        answer, one missing."""
        tiles = [(10, 1, 2), (10, 1, 3), (10, 1, 4)]
        self.put(10, 1, 2)
        bad = self.put(10, 1, 3, data=b"<html>login required</html>")

        # run only the post-pass: aria2 itself replaced by a no-op process
        class DoneProc:
            def poll(self):
                return 0

            def kill(self):
                pass

        orig_popen = osm_tiles.subprocess.Popen
        osm_tiles.subprocess.Popen = lambda *a, **kw: DoneProc()
        self.addCleanup(setattr, osm_tiles.subprocess, "Popen", orig_popen)
        orig_aria = osm_tiles.ARIA2C
        osm_tiles.ARIA2C = "/usr/bin/aria2c"
        self.addCleanup(setattr, osm_tiles, "ARIA2C", orig_aria)

        osm_tiles._download_via_aria2(tiles)
        st = osm_tiles.download_status()
        self.assertEqual(st["failed"], 2)          # html + missing
        self.assertFalse(os.path.exists(bad))      # evicted
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp.name, "10", "1", "2.png")))


class ActiveStoreTest(unittest.TestCase):
    """Store selection: default-external, explicit picks, unplugged fallback."""

    def setUp(self):
        self.pref = "auto"
        self.options = [
            {"key": "internal", "kind": "internal", "free": 3 * 2**30},
            {"key": "/mnt/mmcblk1p1", "kind": "sd", "free": 64 * 2**30},
            {"key": "/mnt/nvme0n1p1", "kind": "nvme", "free": 200 * 2**30},
        ]
        self._orig = (osm_tiles.storage_options, osm_tiles._store_pref)
        osm_tiles.storage_options = lambda: self.options
        osm_tiles._store_pref = lambda: self.pref
        self.addCleanup(lambda: (
            setattr(osm_tiles, "storage_options", self._orig[0]),
            setattr(osm_tiles, "_store_pref", self._orig[1])))
        osm_tiles._active_cache.update(t=0.0, value=None)
        self.addCleanup(osm_tiles._active_cache.update, t=0.0, value=None)

    def test_auto_prefers_the_biggest_external_mount(self):
        st = osm_tiles.active_store(refresh=True)
        self.assertEqual(st["key"], "/mnt/nvme0n1p1")
        self.assertEqual(st["kind"], "nvme")
        self.assertEqual(st["dir"], "/mnt/nvme0n1p1/pcat-tiles")
        self.assertEqual(st["cap_mb"], osm_tiles.STORE_CAP_EXTERNAL_MB)

    def test_auto_without_external_falls_back_to_internal(self):
        self.options = self.options[:1]
        st = osm_tiles.active_store(refresh=True)
        self.assertEqual(st["key"], "internal")
        self.assertEqual(st["cap_mb"], osm_tiles.STORE_CAP_INTERNAL_MB)

    def test_explicit_internal_sticks(self):
        self.pref = "internal"
        self.assertEqual(osm_tiles.active_store(refresh=True)["key"], "internal")

    def test_explicit_mount_is_honoured(self):
        self.pref = "/mnt/mmcblk1p1"
        st = osm_tiles.active_store(refresh=True)
        self.assertEqual(st["key"], "/mnt/mmcblk1p1")

    def test_unplugged_mount_falls_back_instead_of_erroring(self):
        self.pref = "/mnt/gone"
        st = osm_tiles.active_store(refresh=True)
        self.assertEqual(st["key"], "/mnt/nvme0n1p1")


class CountryPlanTest(unittest.TestCase):
    def test_plans_fit_the_tile_budget(self):
        for code, _en, _cn, _bbox in osm_tiles.COUNTRIES:
            bbox, max_z, tiles = osm_tiles.country_plan(code)
            self.assertLessEqual(tiles, osm_tiles.COUNTRY_TILE_CAP, code)
            self.assertLessEqual(max_z, osm_tiles.COUNTRY_MAX_Z, code)
            self.assertGreater(tiles, 0, code)

    def test_small_countries_reach_deeper_zoom_than_big_ones(self):
        _b, z_sg, _t = osm_tiles.country_plan("sg")   # Singapore
        _b, z_ru, _t = osm_tiles.country_plan("ru")   # Russia
        self.assertGreater(z_sg, z_ru)
        self.assertGreaterEqual(z_sg, 11)

    def test_catalog_carries_estimates(self):
        cat = osm_tiles.country_catalog()
        self.assertEqual(len(cat), len(osm_tiles.COUNTRIES))
        th = next(c for c in cat if c["code"] == "th")
        self.assertGreater(th["est_mb"], 1)
        self.assertEqual(th["cn"], "泰国")

    def test_unknown_country_is_refused(self):
        self.assertIsNone(osm_tiles.country_plan("zz"))
        ok, msg = osm_tiles.start_country_download("zz")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
