#!/usr/bin/env python3
"""Fix-age tracking behind the map's growing uncertainty circle.

The circle IS the accuracy claim: with a fix, its radius is the error
estimate; when the fix drops, the UI grows it with fix.age_s. That number
must therefore mean "seconds since the last GGA that carried a real fix" —
not since the last sentence of any kind (a fixless receiver chats NMEA
constantly).

    python3 -m unittest tests.test_gps_fix_age -v
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
from pc_gps_client import GpsClient  # noqa: E402


def gga(quality=1, t="032830.00"):
    # GGA fields: head, time, lat, NS, lon, EW, quality, numsat, hdop,
    # alt, M, geoid, M
    return ["GPGGA", t, "1256.95646", "N", "10053.35582", "E",
            str(quality), "6", "2.4", "135.2", "M", "0.0", "M"]


class FixAgeTest(unittest.TestCase):
    def setUp(self):
        self.gps = GpsClient(nmea_port="/dev/null")
        self.mono = 1000.0
        self._orig = pc_gps_client.time.monotonic
        pc_gps_client.time.monotonic = lambda: self.mono
        self.addCleanup(setattr, pc_gps_client.time, "monotonic", self._orig)

    def age(self):
        return self.gps.get_status()["fix"]["age_s"]

    def test_no_fix_ever_is_none(self):
        self.assertIsNone(self.age())
        self.gps._parse_gga(gga(quality=0))   # fixless chatter
        self.assertIsNone(self.age())

    def test_fix_stamps_and_ages(self):
        self.gps._parse_gga(gga())
        self.assertEqual(self.age(), 0.0)
        self.mono += 42.5
        self.assertEqual(self.age(), 42.5)

    def test_fixless_sentences_do_not_reset_the_age(self):
        self.gps._parse_gga(gga())
        self.mono += 30
        self.gps._parse_gga(gga(quality=0))   # still talking, no fix
        self.assertEqual(self.age(), 30.0)

    def test_new_fix_resets(self):
        self.gps._parse_gga(gga())
        self.mono += 30
        self.gps._parse_gga(gga())
        self.assertEqual(self.age(), 0.0)

    def test_session_reset_clears(self):
        self.gps._parse_gga(gga())
        self.gps._reset_fix_state()
        self.assertIsNone(self.age())


if __name__ == "__main__":
    unittest.main()
