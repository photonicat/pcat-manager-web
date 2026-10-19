#!/usr/bin/env python3
"""Unit tests for battery_history -- the sqlite store behind the /stats
battery chart and the power-event timeline.

Everything runs against a temp database; /etc is never touched.

    python3 -m unittest tests.test_battery_history -v
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))

import battery_history as bh


class BatteryHistoryTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = bh.BATTERY_DB_PATH
        bh.BATTERY_DB_PATH = os.path.join(self.tmpdir.name, "bh.db")
        bh.init_db()

    def tearDown(self):
        bh.BATTERY_DB_PATH = self._orig_path
        self.tmpdir.cleanup()

    def backdate(self, seconds):
        """Shift every record into the past, to simulate age."""
        conn = sqlite3.connect(bh.BATTERY_DB_PATH)
        conn.execute("UPDATE battery_history SET ts = ts - ?", (seconds,))
        conn.commit()
        conn.close()

    def test_record_and_read_back(self):
        bh.record(87, 8.123, -450.0, charging=False, temperature=31.5)
        rows = bh.get_history(hours=1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["soc"], 87)
        self.assertEqual(row["voltage"], 8.123)
        self.assertEqual(row["current"], -450.0)
        self.assertEqual(row["charging"], 0)
        self.assertEqual(row["temperature"], 31.5)

    def test_charging_flag_stored_as_int(self):
        bh.record(50, 8.0, 900.0, charging=True)
        self.assertEqual(bh.get_history(hours=1)[0]["charging"], 1)

    def test_history_window_filters_old_records(self):
        bh.record(80, 8.0, -400.0)
        self.backdate(3 * 3600)  # now 3h old
        bh.record(78, 7.9, -400.0)  # fresh
        self.assertEqual(len(bh.get_history(hours=1)), 1)
        self.assertEqual(len(bh.get_history(hours=24)), 2)

    def test_history_is_oldest_first(self):
        """The chart draws left-to-right from this ordering."""
        bh.record(80, 8.0, -400.0)
        self.backdate(60)
        bh.record(79, 7.9, -400.0)
        rows = bh.get_history(hours=1)
        self.assertEqual([r["soc"] for r in rows], [80, 79])
        self.assertLessEqual(rows[0]["ts"], rows[1]["ts"])

    def test_cleanup_drops_only_expired_records(self):
        bh.record(80, 8.0, -400.0)
        self.backdate((bh.MAX_AGE_DAYS + 1) * 86400)  # past retention
        bh.record(75, 7.8, -400.0)
        bh.cleanup()
        self.assertEqual(bh.get_record_count(), 1)
        self.assertEqual(bh.get_history(hours=24)[0]["soc"], 75)

    def test_init_db_is_idempotent(self):
        bh.record(80, 8.0, -400.0)
        bh.init_db()  # boot again
        self.assertEqual(bh.get_record_count(), 1)

    def test_migration_adds_temperature_to_old_db(self):
        """A database created before the temperature column existed must be
        readable after upgrade, with NULL temperature on old rows."""
        os.remove(bh.BATTERY_DB_PATH)
        conn = sqlite3.connect(bh.BATTERY_DB_PATH)
        conn.execute("""CREATE TABLE battery_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            soc REAL, voltage REAL, current REAL, charging INTEGER DEFAULT 0)""")
        conn.execute("INSERT INTO battery_history (ts, soc, voltage, current)"
                     " VALUES (strftime('%s','now'), 60, 7.5, -300)")
        conn.commit()
        conn.close()

        bh.init_db()  # runs SQL_MIGRATE
        rows = bh.get_history(hours=1)
        self.assertEqual(rows[0]["soc"], 60)
        self.assertIsNone(rows[0]["temperature"])

    def test_full_capacity_kwarg_is_accepted(self):
        """API-compat parameter for callers that know pack capacity."""
        bh.record(80, 8.0, -400.0, full_capacity_mah=8000)
        self.assertEqual(bh.get_record_count(), 1)

    def test_read_of_missing_db_returns_empty_not_raise(self):
        os.remove(bh.BATTERY_DB_PATH)
        bh.BATTERY_DB_PATH = "/nonexistent/dir/bh.db"
        self.assertEqual(bh.get_history(), [])
        self.assertEqual(bh.get_record_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
