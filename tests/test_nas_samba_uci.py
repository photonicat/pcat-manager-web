#!/usr/bin/env python3
"""Unit tests for NAS Samba/UCI management (pcat_util/nas_manager).

Pins the /etc/config/samba4 schema our shares write, the pcat_ prefix
discipline (foreign sambashare sections must be invisible and untouched),
username/share-name validation, and the /etc/passwd line management for
share users (uid range, only-ours-deleted).

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_samba_uci`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

# A samba4 config as uci_show_dict returns it: one foreign share (LuCI's),
# one of ours.
SAMBA_CFG = {
    "samba4.@samba[0]": "samba",
    "samba4.@samba[0].interface": "lan",
    "samba4.luci_share": "sambashare",
    "samba4.luci_share.name": "LuCI Share",
    "samba4.luci_share.path": "/mnt/other",
    "samba4.pcat_media": "sambashare",
    "samba4.pcat_media.name": "Media",
    "samba4.pcat_media.path": "/mnt/nvme/media",
    "samba4.pcat_media.guest_ok": "yes",
    "samba4.pcat_media.read_only": "no",
    "samba4.pcat_media.pcat_http_public": "1",
}


class SambaUciTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.set_calls = []
        self.del_calls = []
        self.commits = []
        self.patches = [
            patch.object(self.nm, 'uci_show_dict',
                         return_value=dict(SAMBA_CFG)),
            patch.object(self.nm, 'uci_set', side_effect=self.set_calls.append),
            patch.object(self.nm, 'uci_delete',
                         side_effect=self.del_calls.append),
            patch.object(self.nm, 'uci_commit',
                         side_effect=self.commits.append),
            patch.object(self.nm, '_samba_reload', lambda restart=False: None),
            patch.object(self.nm, 'managed_mount_roots',
                         return_value=["/mnt/nvme", "/mnt/sdcard"]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.module_patch.stop()

    def test_list_shares_sees_only_ours(self):
        with patch.object(self.nm.os.path, 'realpath', side_effect=lambda p: p):
            shares = self.nm.list_shares()
        self.assertEqual([s["id"] for s in shares], ["pcat_media"])
        share = shares[0]
        self.assertEqual(share["mode"], "guest")
        self.assertTrue(share["http_browse"])
        self.assertTrue(share["valid"])

    def test_share_uci_ops_schema(self):
        ops = self.nm.share_uci_ops(
            "pcat_docs", "Docs", "/mnt/nvme/docs", "users",
            ["alice", "bob"], True, False)
        self.assertIn("samba4.pcat_docs=sambashare", ops)
        self.assertIn("samba4.pcat_docs.path=/mnt/nvme/docs", ops)
        self.assertIn("samba4.pcat_docs.read_only=yes", ops)
        self.assertIn("samba4.pcat_docs.guest_ok=no", ops)
        self.assertIn("samba4.pcat_docs.users=alice bob", ops)
        self.assertIn("samba4.pcat_docs.pcat_http_public=0", ops)
        self.assertIn("samba4.pcat_docs.force_root=1", ops)

    def test_upsert_creates_prefixed_section(self):
        with patch.object(self.nm.os.path, 'isdir', return_value=True):
            out = self.nm.upsert_share(None, "Backups", "/mnt/nvme/backups",
                                       mode="guest")
        self.assertTrue(out["ok"])
        self.assertTrue(out["id"].startswith("pcat_"))
        self.assertTrue(any(c.startswith(out["id"].join(("samba4.", "=")))
                            or c.startswith("samba4." + out["id"])
                            for c in self.set_calls))
        self.assertEqual(self.commits, ["samba4"])

    def test_upsert_refuses_foreign_and_unknown_ids(self):
        with patch.object(self.nm.os.path, 'isdir', return_value=True):
            out = self.nm.upsert_share("luci_share", "X", "/mnt/nvme/x")
            self.assertFalse(out["ok"])
            out = self.nm.upsert_share("pcat_nope", "X", "/mnt/nvme/x")
            self.assertFalse(out["ok"])
        self.assertEqual(self.set_calls, [])

    def test_upsert_refuses_path_off_storage(self):
        with patch.object(self.nm.os.path, 'isdir', return_value=True):
            out = self.nm.upsert_share(None, "Evil", "/etc")
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "bad_path")

    def test_upsert_validates_names_and_users(self):
        with patch.object(self.nm.os.path, 'isdir', return_value=True):
            self.assertFalse(self.nm.upsert_share(
                None, "bad;name", "/mnt/nvme/x")["ok"])
            self.assertFalse(self.nm.upsert_share(
                None, "Ok", "/mnt/nvme/x", mode="users",
                users=["Bad User"])["ok"])
            self.assertFalse(self.nm.upsert_share(
                None, "Ok", "/mnt/nvme/x", mode="wat")["ok"])

    def test_delete_share_only_ours(self):
        self.assertFalse(self.nm.delete_share("luci_share")["ok"])
        self.assertEqual(self.del_calls, [])
        self.assertTrue(self.nm.delete_share("pcat_media")["ok"])
        self.assertEqual(self.del_calls, ["samba4.pcat_media"])

    def test_disable_shares_under_mountpoint(self):
        doomed = self.nm._disable_shares_under("/mnt/nvme")
        self.assertEqual(doomed, ["pcat_media"])
        self.assertIn("samba4.pcat_media", self.del_calls)
        # The foreign share under a different tree was untouched.
        self.assertNotIn("samba4.luci_share", self.del_calls)


class PasswdTests(unittest.TestCase):

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

    def test_passwd_line_shape(self):
        self.assertEqual(
            self.nm.passwd_line("alice", 21000),
            "alice:x:21000:65534::/var/empty:/bin/false")

    def test_parse_passwd(self):
        users = self.nm._parse_passwd(
            "root:x:0:0:root:/root:/bin/ash\n"
            "alice:x:21000:65534::/var/empty:/bin/false\n")
        self.assertEqual(users["root"], 0)
        self.assertEqual(users["alice"], 21000)

    def test_username_regex(self):
        ok = ("alice", "bob-2", "_svc", "a" * 32)
        bad = ("Alice", "9start", "with space", "", "a" * 33, "semi;colon")
        for u in ok:
            self.assertTrue(self.nm._USERNAME_RE.match(u), u)
        for u in bad:
            self.assertFalse(self.nm._USERNAME_RE.match(u), u)


if __name__ == '__main__':
    unittest.main()
