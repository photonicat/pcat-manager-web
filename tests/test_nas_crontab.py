#!/usr/bin/env python3
"""Unit tests for the NAS rsync crontab management + share-link passwords.

sync_crontab edits /etc/crontabs/root, which other subsystems also write
(coremark, photondns on the real device) — the non-negotiable property is
that every foreign line survives byte-for-byte. Also pins the rendered
rsync line (flock + --modify-window=2) and the pbkdf2 link-password
round-trip.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_crontab`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

FOREIGN = ("0 4 * * * /etc/coremark.sh\n"
           "0 4 * * * /usr/bin/photondns-chinalist\n"
           "* * * * * /usr/sbin/pcat-fw4-zone-check\n")

JOB = {"id": "sdeadbeef", "name": "photos", "src": "/mnt/sd/DCIM",
       "dst": "/mnt/nvme/backup", "schedule": "daily", "delete": False,
       "enabled": True}


class CrontabTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager

    def tearDown(self):
        self.module_patch.stop()

    def test_render_line(self):
        line = self.nm.render_cron_line(JOB)
        self.assertTrue(line.startswith("30 3 * * * "))
        self.assertIn("flock -n /tmp/pcat_rsync_sdeadbeef.lock", line)
        self.assertIn("--modify-window=2", line)
        self.assertIn("'/mnt/sd/DCIM/' '/mnt/nvme/backup/'", line)
        self.assertTrue(line.endswith("# pcat-nas:sdeadbeef"))
        self.assertNotIn("--delete", line)
        line = self.nm.render_cron_line(dict(JOB, delete=True,
                                             schedule="hourly"))
        self.assertIn("--delete", line)
        self.assertTrue(line.startswith("15 * * * *"))

    def test_custom_cron_schedule(self):
        line = self.nm.render_cron_line(dict(JOB, schedule="cron",
                                             cron="*/10 * * * *"))
        self.assertTrue(line.startswith("*/10 * * * *"))

    def test_foreign_lines_survive_byte_for_byte(self):
        out = self.nm.sync_crontab([JOB], FOREIGN)
        for line in FOREIGN.strip().splitlines():
            self.assertIn(line, out)
        self.assertIn("# pcat-nas:sdeadbeef", out)
        # Removing the job restores the original text exactly.
        out2 = self.nm.sync_crontab([], out)
        self.assertEqual(out2, FOREIGN)

    def test_idempotent(self):
        once = self.nm.sync_crontab([JOB], FOREIGN)
        twice = self.nm.sync_crontab([JOB], once)
        self.assertEqual(once, twice)

    def test_disabled_job_removed(self):
        out = self.nm.sync_crontab([dict(JOB, enabled=False)], FOREIGN)
        self.assertNotIn("pcat-nas", out)

    def test_empty_crontab(self):
        self.assertEqual(self.nm.sync_crontab([], ""), "")
        out = self.nm.sync_crontab([JOB], "")
        self.assertEqual(len(out.strip().splitlines()), 1)


class LinkPasswordTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager

    def tearDown(self):
        self.module_patch.stop()

    def test_password_roundtrip(self):
        link = {"password_hash": self.nm._hash_link_password("hunter2")}
        self.assertTrue(self.nm.check_link_password(link, "hunter2"))
        self.assertFalse(self.nm.check_link_password(link, "wrong"))
        self.assertFalse(self.nm.check_link_password(link, ""))
        self.assertFalse(self.nm.check_link_password({}, "hunter2"))

    def test_salts_differ(self):
        a = self.nm._hash_link_password("x")
        b = self.nm._hash_link_password("x")
        self.assertNotEqual(a, b)

    def test_find_link_token_validation(self):
        with patch.object(self.nm, 'list_links', return_value=[
                {"token": "abcdefgh1234", "path": "/mnt/x"}]):
            self.assertIsNotNone(self.nm.find_link("abcdefgh1234"))
            self.assertIsNone(self.nm.find_link("nope"))
            self.assertIsNone(self.nm.find_link("../etc/passwd"))
            self.assertIsNone(self.nm.find_link(None))


if __name__ == '__main__':
    unittest.main()
