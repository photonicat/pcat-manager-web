#!/usr/bin/env python3
"""AGPS (gpsOneXTRA) assist injection.

The value under test is the AT sequence: enable XTRA, upload the file over
the CONNECT binary phase, inject current UTC, activate the file — with the
GNSS session stopped around it and re-armed after, success or not. All AT
traffic is faked; no modem, no network.

    python3 -m unittest tests.test_gps_agps -v
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

from pc_gps_client import GpsClient  # noqa: E402

XTRA_BYTES = b"\x01" * 20000


class AgpsHarness(unittest.TestCase):
    def setUp(self):
        self.gps = GpsClient(nmea_port="/dev/null")
        self.at_log = []          # every AT command, in order
        self.uploads = []         # (cmd, payload) of binary uploads
        self.power_calls = []
        self.responses = {        # prefix -> canned reply
            "AT+QGPSXTRA?": "+QGPSXTRA: 1\nOK",
            "AT+QGPSXTRA=1": "OK",
            "AT+QFDEL": "OK",
            "AT+QGPSXTRATIME": "OK",
            "AT+QGPSXTRADATA=": "OK",
            "AT+QGPSXTRADATA?": '+QGPSXTRADATA: 4320,"2026/08/16,03:30:00"\nOK',
        }

        def fake_send(cmd, timeout=3.0):
            self.at_log.append(cmd)
            for prefix, resp in self.responses.items():
                if cmd.startswith(prefix):
                    return resp
            return "OK"

        def fake_payload(cmd, payload, timeout=30.0):
            self.at_log.append(cmd)
            self.uploads.append((cmd, payload))
            return "+QFUPL: {},1a2b\nOK".format(len(payload))

        self.gps._send_at = fake_send
        self.gps.set_at_payload_sender(fake_payload)
        self.gps._download_xtra = lambda: ("xtra3grcej.bin", XTRA_BYTES)
        self.gps.power_on = lambda: self.power_calls.append("on") or True
        self.gps.power_off = lambda: self.power_calls.append("off") or True

    def cmds(self, prefix):
        return [c for c in self.at_log if c.startswith(prefix)]


class AgpsStatusTest(AgpsHarness):
    def test_parses_enable_and_data_validity(self):
        # The modem's yyyy/mm/dd,hh:mm:ss is normalized to the ISO
        # yyyy-mm-dd form every date in this UI uses.
        st = self.gps.agps_status(refresh=True)
        self.assertEqual(st, {"enabled": True, "valid_min": 4320,
                              "injected_utc": "2026-08-16 03:30:00"})

    def test_cache_answers_without_at_traffic(self):
        self.gps.agps_status(refresh=True)
        before = len(self.at_log)
        self.gps.agps_status()
        self.assertEqual(len(self.at_log), before)

    def test_no_data_reported_as_none(self):
        self.responses["AT+QGPSXTRADATA?"] = "+CME ERROR: 517"
        st = self.gps.agps_status(refresh=True)
        self.assertIsNone(st["valid_min"])


class AgpsUpdateTest(AgpsHarness):
    def test_full_injection_sequence(self):
        st, err = self.gps.agps_update()
        self.assertIsNone(err)
        self.assertEqual(st["valid_min"], 4320)
        # the binary upload carried the whole file to UFS — the one storage
        # the GNSS engine reads assist files from (hardware-verified)
        self.assertEqual(len(self.uploads), 1)
        cmd, payload = self.uploads[0]
        self.assertIn('AT+QFUPL="UFS:xtra3grcej.bin",20000', cmd)
        self.assertEqual(payload, XTRA_BYTES)
        # hardware-verified two-arg XTRATIME form, not the five-arg one
        time_cmds = self.cmds("AT+QGPSXTRATIME=")
        self.assertEqual(len(time_cmds), 1)
        self.assertRegex(time_cmds[0],
                         r'^AT\+QGPSXTRATIME="\d{4}/\d{2}/\d{2},'
                         r'\d{2}:\d{2}:\d{2}",3500$')
        # ordered: time goes in first (or the engine judges the ephemeris
        # against its 1980 RTC), then the file, then activation
        idx = {p: next(i for i, c in enumerate(self.at_log) if c.startswith(p))
               for p in ("AT+QFUPL", "AT+QGPSXTRATIME", 'AT+QGPSXTRADATA="')}
        self.assertLess(idx["AT+QGPSXTRATIME"], idx["AT+QFUPL"])
        self.assertLess(idx["AT+QFUPL"], idx['AT+QGPSXTRADATA="'])

    def test_running_session_is_bounced_around_injection(self):
        self.gps.powered = True
        self.gps.agps_update()
        self.assertEqual(self.power_calls, ["off", "on"])

    def test_idle_receiver_stays_idle(self):
        self.gps.powered = False
        self.gps.agps_update()
        self.assertEqual(self.power_calls, [])

    def test_session_restored_even_when_injection_fails(self):
        self.gps.powered = True
        self.responses["AT+QGPSXTRADATA="] = "+CME ERROR: 516"
        st, err = self.gps.agps_update()
        self.assertIsNone(st)
        self.assertIn("injection refused", err)
        self.assertEqual(self.power_calls, ["off", "on"])

    def test_xtra_gets_enabled_when_off(self):
        self.responses["AT+QGPSXTRA?"] = "+QGPSXTRA: 0\nOK"
        self.gps.agps_update()
        self.assertTrue(self.cmds("AT+QGPSXTRA=1"))

    def test_upload_refusal_is_an_error(self):
        self.gps.set_at_payload_sender(lambda c, p, t: "ERROR")
        st, err = self.gps.agps_update()
        self.assertIsNone(st)
        self.assertIn("upload failed", err)

    def test_download_failure_is_an_error(self):
        self.gps._download_xtra = lambda: (None, "all mirrors down")
        st, err = self.gps.agps_update()
        self.assertIsNone(st)
        self.assertIn("download failed", err)
        self.assertEqual(self.uploads, [])

    def test_no_payload_channel_is_an_error(self):
        self.gps.set_at_payload_sender(None)
        st, err = self.gps.agps_update()
        self.assertIsNone(st)
        self.assertIn("not ready", err)


if __name__ == "__main__":
    unittest.main()
