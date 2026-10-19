#!/usr/bin/env python3
"""The /gps trip odometer.

Distance travelled since the GNSS session started, integrated from GGA
fixes. The whole difficulty is that a parked receiver is not still: it
wanders by metres every second, and summing raw 1 Hz deltas would show a
car park commute of several km overnight. These tests pin the gates that
stop that, and the ones that stop the opposite failure -- a reacquisition
jump being counted as real travel.

    python3 -m unittest tests.test_gps_trip_odometer -v
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
from pc_gps_client import GpsClient, _dist_m  # noqa: E402

# One degree of latitude is ~111.32 km; this converts a metre offset into the
# latitude delta that produces it, which is how the tests move the receiver.
DEG_PER_M = 1.0 / 111320.0


class TripOdometerTest(unittest.TestCase):
    def setUp(self):
        self.gps = GpsClient(nmea_port="/dev/null")
        self.t = 1_700_000_000.0
        # Deterministic clock: the odometer's speed sanity check is in m/s, so
        # the tests have to own the time between fixes.
        self._real_time = pc_gps_client.time.time
        pc_gps_client.time.time = lambda: self.t
        self.addCleanup(setattr, pc_gps_client.time, "time", self._real_time)

    def step(self, lat, lon, accuracy_m=2.0, dt=1.0, speed_kmh=60.0):
        self.t += dt
        self.gps._accumulate_trip(lat, lon, accuracy_m, speed_kmh)

    def trip(self):
        return self.gps._trip_m

    # ---- the number itself ----

    def test_a_straight_run_measures_its_own_length(self):
        lat, lon = 31.20, 121.45
        legs = 20
        leg_m = 50.0
        for i in range(1, legs + 1):
            self.step(lat + i * leg_m * DEG_PER_M, lon)
        # The first fix only anchors, so the measured run is one leg shorter
        # than the path walked -- the odometer cannot know about travel that
        # happened before it had a reference point.
        self.assertAlmostEqual(self.trip(), (legs - 1) * leg_m, delta=5.0)

    def test_the_first_fix_only_anchors_and_adds_nothing(self):
        self.step(31.20, 121.45)
        self.assertEqual(self.trip(), 0.0)

    # ---- stationary noise, the reason the gates exist ----

    def test_a_parked_receiver_stays_at_zero(self):
        # An hour of 1 Hz jitter, +-3 m, alternating either side of the true
        # position. Ungated this sums to several km.
        lat, lon = 31.20, 121.45
        for i in range(3600):
            self.step(lat + (3.0 if i % 2 else -3.0) * DEG_PER_M, lon,
                      speed_kmh=0.0)
        self.assertEqual(self.trip(), 0.0)

    def test_wide_parked_jitter_does_not_clear_the_distance_floor(self):
        # The real failure the speed gate exists for. Jitter is not bounded by
        # the distance floor: a swing from -6 m to +6 m is a 12 m step, over
        # the 8 m floor, and a receiver parked overnight takes that bet
        # thousands of times. Doppler speed says 0 through all of it.
        lat, lon = 31.20, 121.45
        for i in range(3600):
            self.step(lat + (6.0 if i % 2 else -6.0) * DEG_PER_M, lon,
                      speed_kmh=0.0)
        self.assertEqual(self.trip(), 0.0)

    def test_slow_but_real_movement_is_not_gated_away(self):
        # Walking pace is above the speed gate and must still be measured.
        lat, lon = 31.20, 121.45
        for i in range(1, 41):
            self.step(lat + i * 20.0 * DEG_PER_M, lon, speed_kmh=4.0, dt=18.0)
        self.assertAlmostEqual(self.trip(), 39 * 20.0, delta=10.0)

    def test_movement_still_counts_when_the_receiver_reports_no_speed(self):
        # Neither RMC nor VTG: the distance gates have to carry it alone
        # rather than the odometer silently reading zero forever.
        lat, lon = 31.20, 121.45
        for i in range(1, 11):
            self.step(lat + i * 50.0 * DEG_PER_M, lon, speed_kmh=None)
        self.assertAlmostEqual(self.trip(), 9 * 50.0, delta=5.0)

    def test_a_poor_fix_raises_the_bar(self):
        # accuracy_m 40 means the receiver itself says +-40 m; a 20 m "step"
        # is inside its own error bars and must not count.
        lat, lon = 31.20, 121.45
        self.step(lat, lon, accuracy_m=40.0)
        self.step(lat + 20.0 * DEG_PER_M, lon, accuracy_m=40.0)
        self.assertEqual(self.trip(), 0.0)
        # The same step with a good fix does count.
        self.gps = GpsClient(nmea_port="/dev/null")
        self.step(lat, lon, accuracy_m=2.0)
        self.step(lat + 20.0 * DEG_PER_M, lon, accuracy_m=2.0)
        self.assertAlmostEqual(self.trip(), 20.0, delta=2.0)

    # ---- glitches ----

    def test_a_reacquisition_jump_is_not_counted_as_travel(self):
        lat, lon = 31.20, 121.45
        self.step(lat, lon)
        self.step(lat + 20_000.0 * DEG_PER_M, lon, dt=1.0)   # 20 km in 1 s
        self.assertEqual(self.trip(), 0.0)

    def test_a_rejected_jump_still_moves_the_anchor(self):
        # Otherwise the anchor stays in the old town forever and every later
        # fix is re-rejected against it -- the odometer would freeze for good.
        lat, lon = 31.20, 121.45
        self.step(lat, lon)
        far = lat + 20_000.0 * DEG_PER_M
        self.step(far, lon, dt=1.0)
        self.step(far + 100.0 * DEG_PER_M, lon, dt=10.0)
        self.assertAlmostEqual(self.trip(), 100.0, delta=5.0)

    def test_a_long_gap_allows_a_proportionally_long_step(self):
        # Fix lost in a tunnel for 10 minutes: 12 km at 20 m/s is ordinary
        # motorway travel, not a glitch.
        lat, lon = 31.20, 121.45
        self.step(lat, lon)
        self.step(lat + 12_000.0 * DEG_PER_M, lon, dt=600.0)
        self.assertAlmostEqual(self.trip(), 12_000.0, delta=50.0)

    # ---- session lifecycle ----

    def test_the_odometer_restarts_with_the_gnss_session(self):
        lat, lon = 31.20, 121.45
        self.step(lat, lon)
        self.step(lat + 500.0 * DEG_PER_M, lon, dt=20.0)
        self.assertGreater(self.trip(), 400.0)
        self.gps._reset_fix_state()   # what power_off/restart_modem call
        self.assertEqual(self.trip(), 0.0)
        self.assertIsNone(self.gps._trip_since)

    def test_status_reports_the_trip_outside_the_fix_block(self):
        # Losing the fix blanks speed and course by design. It must not blank
        # the distance already travelled, so trip lives at the top level.
        lat, lon = 31.20, 121.45
        self.step(lat, lon)
        self.step(lat + 300.0 * DEG_PER_M, lon, dt=15.0)
        st = self.gps.get_status()
        self.assertIn("trip", st)
        self.assertNotIn("trip_m", st["fix"])
        self.assertGreater(st["trip"]["distance_m"], 250.0)
        self.assertIsNotNone(st["trip"]["since"])

    def test_status_is_valid_before_any_fix_arrives(self):
        st = self.gps.get_status()
        self.assertEqual(st["trip"]["distance_m"], 0.0)
        self.assertIsNone(st["trip"]["since"])


class GgaWiringTest(unittest.TestCase):
    """The odometer has to actually be fed by the NMEA parser."""

    def setUp(self):
        self.gps = GpsClient(nmea_port="/dev/null")

    def gga(self, lat_thou, quality=1, hdop="0.8"):
        """A GGA at 31 deg 12.<lat_thou/1000> minutes north. One arc-minute of
        latitude is 1852 m, so lat_thou is roughly 1.85 m per unit."""
        # $GPGGA,time,lat,N,lon,E,quality,numsat,hdop,alt,M,geoid,M
        return ["GPGGA", "120000.00", "3112.%05d" % (lat_thou * 100), "N",
                "12127.00000", "E", str(quality), "09", hdop,
                "10.0", "M", "0.0", "M"]

    def test_gga_fixes_drive_the_odometer(self):
        # ~37 m apart: over the 8 m floor and the 4 m accuracy this hdop
        # implies, and under the jump ceiling for two fixes a second apart.
        self.gps._parse_gga(self.gga(0))
        self.gps._parse_gga(self.gga(20))
        self.assertGreater(self.gps._trip_m, 20.0)
        self.assertLess(self.gps._trip_m, 60.0)

    def test_a_no_fix_gga_contributes_nothing(self):
        self.gps._parse_gga(self.gga(0, quality=0))
        self.gps._parse_gga(self.gga(20, quality=0))
        self.assertEqual(self.gps._trip_m, 0.0)
        self.assertIsNone(self.gps._trip_since)


class DistanceHelperTest(unittest.TestCase):
    def test_haversine_matches_a_known_pair(self):
        # Shanghai People's Square -> Lujiazui, ~3.4 km.
        d = _dist_m(31.2304, 121.4737, 31.2397, 121.4998)
        self.assertAlmostEqual(d, 2700.0, delta=400.0)

    def test_zero_distance_is_zero(self):
        self.assertEqual(_dist_m(31.2, 121.4, 31.2, 121.4), 0.0)


if __name__ == "__main__":
    unittest.main()
