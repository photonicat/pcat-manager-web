#!/usr/bin/env python3
"""Setting the system clock from GNSS time.

The chain under test: a valid RMC carries UTC time+date; the parser stamps it
against the monotonic clock; sync_system_time() extrapolates from that stamp
(epoch + measured NMEA report latency + elapsed) and steps the wall clock.
The monotonic anchor is the point — the wall clock being wrong is the very
thing being fixed, so nothing in the computation may depend on it.

The latency constant was measured on hardware (RM520N-GL vs NTP): RMC lands
a median 41 ms after the whole second it names, not the folklore half-second.

    python3 -m unittest tests.test_gps_time_sync -v
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app")))
sys.modules.setdefault("serial", MagicMock())

from pcat_util import pcat_logger  # noqa: E402
pcat_logger.logger = MagicMock()

import pc_gps_client  # noqa: E402
from pc_gps_client import (  # noqa: E402
    GpsClient,
    GPS_TIME_LATENCY_S,
    GPS_TIME_MAX_AGE_S,
    _nmea_epoch,
)

# $GxRMC fields after checksum strip: head, time, status, lat, NS, lon, EW,
# speed_kn, course, date
def rmc(t="032830.00", status="A", date="160826"):
    return ["GPRMC", t, status, "1256.95646", "N", "10053.35582", "E",
            "0.0", "230.4", date]


# 2026-08-16 03:28:30 UTC
EPOCH_032830 = 1786850910.0


class NmeaEpochTest(unittest.TestCase):
    def test_whole_second(self):
        self.assertEqual(_nmea_epoch("032830.00", "160826"), EPOCH_032830)

    def test_fractional_second_is_kept(self):
        # Sub-second precision is the entire point of a time sync.
        self.assertEqual(_nmea_epoch("032830.50", "160826"), EPOCH_032830 + 0.5)

    def test_malformed_fields_are_none(self):
        self.assertIsNone(_nmea_epoch("", "160826"))
        self.assertIsNone(_nmea_epoch("032830.00", ""))
        self.assertIsNone(_nmea_epoch("xx2830.00", "160826"))
        self.assertIsNone(_nmea_epoch("032830.00", "999999"))
        self.assertIsNone(_nmea_epoch("0328", "160826"))

    def test_leap_second_is_accepted(self):
        self.assertEqual(_nmea_epoch("032860.00", "160826"), EPOCH_032830 + 30)


class ClockedGpsTest(unittest.TestCase):
    """Base: GpsClient with a fully owned clock (wall + monotonic + settime)."""

    def setUp(self):
        self.gps = GpsClient(nmea_port="/dev/null")
        self.wall = 1_700_000_000.0
        self.mono = 5_000.0
        self.set_calls = []

        self._orig = (pc_gps_client.time.time, pc_gps_client.time.monotonic,
                      getattr(pc_gps_client.time, "clock_settime", None))
        pc_gps_client.time.time = lambda: self.wall
        pc_gps_client.time.monotonic = lambda: self.mono
        pc_gps_client.time.clock_settime = \
            lambda clk, t: self.set_calls.append((clk, t))

    def tearDown(self):
        pc_gps_client.time.time = self._orig[0]
        pc_gps_client.time.monotonic = self._orig[1]
        if self._orig[2] is not None:
            pc_gps_client.time.clock_settime = self._orig[2]


class TimeRefCaptureTest(ClockedGpsTest):
    def test_valid_rmc_records_the_reference(self):
        self.gps._parse_rmc(rmc())
        ref = self.gps._time_ref
        self.assertIsNotNone(ref)
        self.assertEqual(ref["epoch"], EPOCH_032830)
        self.assertEqual(ref["mono"], self.mono)
        self.assertEqual(ref["wall"], self.wall)

    def test_void_rmc_is_not_trusted(self):
        # A V sentence may carry the receiver's free-running RTC guess.
        self.gps._parse_rmc(rmc(status="V"))
        self.assertIsNone(self.gps._time_ref)

    def test_missing_time_or_date_is_ignored(self):
        self.gps._parse_rmc(rmc(t=""))
        self.gps._parse_rmc(rmc(date=""))
        self.assertIsNone(self.gps._time_ref)

    def test_reference_resets_with_the_session(self):
        self.gps._parse_rmc(rmc())
        self.gps._reset_fix_state()
        self.assertIsNone(self.gps._time_ref)


class SyncSystemTimeTest(ClockedGpsTest):
    def test_no_reference_refuses(self):
        result, err = self.gps.sync_system_time()
        self.assertIsNone(result)
        self.assertIn("No GPS time", err)
        self.assertEqual(self.set_calls, [])

    def test_stale_reference_refuses(self):
        self.gps._parse_rmc(rmc())
        self.mono += GPS_TIME_MAX_AGE_S + 1
        result, err = self.gps.sync_system_time()
        self.assertIsNone(result)
        self.assertIn("stale", err)
        self.assertEqual(self.set_calls, [])

    def test_target_extrapolates_on_the_monotonic_clock(self):
        """Wall clock 500s wrong, 2.5s elapsed since the RMC: the set target
        must be epoch + latency + 2.5, untouched by the wall error."""
        self.gps._parse_rmc(rmc())
        self.wall += 500.0     # wall drifts badly after the stamp
        self.mono += 2.5
        result, err = self.gps.sync_system_time()
        self.assertIsNone(err)
        self.assertEqual(len(self.set_calls), 1)
        _clk, target = self.set_calls[0]
        self.assertAlmostEqual(target, EPOCH_032830 + GPS_TIME_LATENCY_S + 2.5,
                               places=6)
        self.assertAlmostEqual(result["adjusted_s"], target - self.wall, places=3)

    def test_reference_wall_is_restamped_so_offset_reads_zero(self):
        self.gps._parse_rmc(rmc())
        self.wall += 500.0
        self.mono += 1.0
        self.gps.sync_system_time()
        offset = self.gps.get_status()["time_sync"]["offset_s"]
        self.assertAlmostEqual(offset, 0.0, delta=0.01)

    def test_settime_failure_is_reported(self):
        self.gps._parse_rmc(rmc())
        def boom(clk, t):
            raise PermissionError("nope")
        pc_gps_client.time.clock_settime = boom
        result, err = self.gps.sync_system_time()
        self.assertIsNone(result)
        self.assertIn("Setting clock failed", err)


class StatusTimeSyncTest(ClockedGpsTest):
    def test_absent_until_a_valid_rmc(self):
        self.assertIsNone(self.gps.get_status()["time_sync"])

    def test_reports_offset_age_and_usability(self):
        self.gps._parse_rmc(rmc())
        self.wall += 3.0
        self.mono += 3.0
        ts = self.gps.get_status()["time_sync"]
        # system - GPS at receive: wall stamp vs epoch+latency
        self.assertAlmostEqual(
            ts["offset_s"],
            (self.wall - 3.0) - (EPOCH_032830 + GPS_TIME_LATENCY_S), places=3)
        self.assertAlmostEqual(ts["age_s"], 3.0, places=1)
        self.assertTrue(ts["usable"])
        self.mono += GPS_TIME_MAX_AGE_S
        self.assertFalse(self.gps.get_status()["time_sync"]["usable"])


if __name__ == "__main__":
    unittest.main()
