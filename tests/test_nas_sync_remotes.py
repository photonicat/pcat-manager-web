#!/usr/bin/env python3
"""Unit tests for NAS sync jobs, backup preset, remote mounts and links.

Covers the phase 2/3 state machines end to end with pcat_config and
subprocess mocked: job CRUD and its path/schedule validation, the rsync
run-now argv, remote-mount command construction (including that secrets go
via stdin/config file and never into argv or an API response), the
remount keeper's backoff and WAN-recovery reset, and share-link CRUD.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_sync_remotes`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))


class FakeConfig:
    """Stand-in for pcat_config's user-config key/value store."""

    def __init__(self):
        self.store = {}

    def get_user_config_key(self, key, default=None):
        return self.store.get(key, default)

    def set_user_config_key(self, key, value):
        self.store[key] = value


class SyncJobTests(unittest.TestCase):

    def setUp(self):
        self.cfg = FakeConfig()
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
            'pcat_config': self.cfg,
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.patches = [
            patch.object(self.nm, 'managed_mount_roots',
                         return_value=["/mnt/sd", "/mnt/nvme"]),
            patch.object(self.nm, 'apply_cron'),
            patch.object(self.nm.os.path, 'realpath', side_effect=lambda p: p),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.module_patch.stop()

    def _make(self, **over):
        fields = {"name": "photos", "src": "/mnt/sd/DCIM",
                  "dst": "/mnt/nvme/backup", "schedule": "daily"}
        fields.update(over)
        return self.nm.save_sync_job(None, fields)

    def test_create_and_list(self):
        out = self._make()
        self.assertTrue(out["ok"])
        jobs = self.nm.list_sync_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["name"], "photos")
        self.assertTrue(jobs[0]["enabled"])
        self.assertTrue(jobs[0]["id"].startswith("s"))

    def test_update_existing(self):
        job_id = self._make()["id"]
        self.nm.save_sync_job(job_id, {
            "name": "renamed", "src": "/mnt/sd/DCIM",
            "dst": "/mnt/nvme/backup", "schedule": "weekly"})
        jobs = self.nm.list_sync_jobs()
        self.assertEqual(len(jobs), 1)          # updated, not appended
        self.assertEqual(jobs[0]["name"], "renamed")
        self.assertEqual(jobs[0]["schedule"], "weekly")

    def test_paths_must_be_on_managed_storage(self):
        out = self._make(src="/etc")
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "bad_path")
        out = self._make(dst="/root/stuff")
        self.assertEqual(out["code"], "bad_path")

    def test_schedule_validation(self):
        self.assertEqual(self._make(schedule="whenever")["code"],
                         "bad_schedule")
        self.assertEqual(
            self._make(schedule="cron", cron="not a cron")["code"], "bad_cron")
        self.assertEqual(
            self._make(schedule="cron", cron="* * * *")["code"], "bad_cron")
        self.assertTrue(
            self._make(schedule="cron", cron="*/10 * * * *")["ok"])

    def test_delete(self):
        job_id = self._make()["id"]
        with patch.object(self.nm.os, 'unlink'):
            self.nm.delete_sync_job(job_id)
        self.assertEqual(self.nm.list_sync_jobs(), [])

    def test_run_rsync_now_argv(self):
        seen = {}

        class Handle:
            def check_cancel(self): pass
            def set_progress(self, **kw): pass

            def watch_subprocess(self, argv, line_cb=None, **kw):
                seen["argv"] = argv
                if line_cb:
                    line_cb(" 42%  1.2MB/s")
                return 0
        job = {"id": "sdeadbeef", "src": "/mnt/sd/DCIM",
               "dst": "/mnt/nvme/backup", "delete": False}
        self.nm.run_rsync_now(Handle(), job)
        argv = seen["argv"]
        self.assertEqual(argv[:2], ["flock", "-n"])
        self.assertIn("--info=progress2", argv)
        self.assertIn("--modify-window=2", argv)
        self.assertNotIn("--delete", argv)
        self.assertTrue(argv[-2].endswith("/DCIM/"))
        self.assertTrue(argv[-1].endswith("/backup/"))

    def test_run_rsync_now_failure_raises(self):
        class Handle:
            def check_cancel(self): pass
            def set_progress(self, **kw): pass
            def watch_subprocess(self, argv, **kw): return 23
        with self.assertRaises(RuntimeError):
            self.nm.run_rsync_now(Handle(), {"id": "s1", "src": "/a",
                                             "dst": "/b"})

    def test_sync_log_tail_rejects_bad_id(self):
        self.assertEqual(self.nm.sync_log_tail("../../etc/passwd"), "")
        self.assertEqual(self.nm.sync_log_tail("nope"), "")

    def test_backup_status_pairs_sd_and_nvme(self):
        disks = [
            {"bus": "sd", "partitions": [{"mounted": True,
                                          "mountpoint": "/mnt/sd"}]},
            {"bus": "nvme", "partitions": [{"mounted": True,
                                            "mountpoint": "/mnt/nvme"}]},
        ]
        with patch.object(self.nm, 'list_disks', return_value=disks):
            st = self.nm.backup_status()
        self.assertTrue(st["ready"])
        self.assertEqual(st["dest"], "/mnt/nvme/photonicat-sd-backup")

    def test_backup_status_not_ready_without_nvme(self):
        disks = [{"bus": "sd",
                  "partitions": [{"mounted": True, "mountpoint": "/mnt/sd"}]}]
        with patch.object(self.nm, 'list_disks', return_value=disks):
            st = self.nm.backup_status()
        self.assertFalse(st["ready"])
        self.assertIsNone(st["dest"])


class RemoteTests(unittest.TestCase):

    def setUp(self):
        self.cfg = FakeConfig()
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
            'pcat_config': self.cfg,
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.nm._remote_backoff.clear()

    def tearDown(self):
        self.nm._remote_backoff.clear()
        self.module_patch.stop()

    def _make_ssh(self, **over):
        fields = {"type": "sshfs", "name": "nas1", "host": "10.0.0.5",
                  "user": "bob", "password": "s3cret", "path": "/data"}
        fields.update(over)
        return self.nm.save_remote(None, fields)

    def test_create_and_mountpoint(self):
        out = self._make_ssh()
        self.assertTrue(out["ok"])
        remote = self.nm.list_remotes()[0]
        self.assertEqual(remote["mountpoint"], "/mnt/remote/nas1")

    def test_name_and_type_validation(self):
        self.assertEqual(self._make_ssh(name="bad name")["code"], "bad_name")
        self.assertEqual(self._make_ssh(name="")["code"], "bad_name")
        self.assertEqual(self._make_ssh(type="ftp")["code"], "bad_type")

    def test_public_view_hides_secrets(self):
        self._make_ssh()
        pub = self.nm._remote_public(self.nm.list_remotes()[0])
        self.assertNotIn("password", pub)
        self.assertNotIn("secret_key", pub)
        self.assertTrue(pub["has_secret"])

    def test_sshfs_password_via_stdin_not_argv(self):
        self._make_ssh()
        remote = self.nm.list_remotes()[0]
        seen = {}

        def fake_run(argv, timeout=10, stdin_data=None):
            seen["argv"], seen["stdin"] = argv, stdin_data
            return 0, ""
        with patch.object(self.nm.os, 'makedirs'), \
             patch.object(self.nm, '_remote_alive', return_value=False), \
             patch.object(self.nm, '_wait_mounted', return_value=True), \
             patch.object(self.nm, '_run', side_effect=fake_run):
            out = self.nm.mount_remote(remote)
        self.assertTrue(out["ok"])
        self.assertEqual(seen["argv"][0], "sshfs")
        self.assertNotIn("s3cret", " ".join(seen["argv"]))
        self.assertEqual(seen["stdin"], "s3cret\n")
        self.assertIn("reconnect", " ".join(seen["argv"]))

    def test_s3_mount_uses_rclone_config(self):
        self.nm.save_remote(None, {
            "type": "s3", "name": "backblaze", "endpoint": "https://s3.x",
            "bucket": "mybucket", "access_key": "AK", "secret_key": "SK"})
        remote = self.nm.list_remotes()[0]
        seen = {}
        with patch.object(self.nm.os, 'makedirs'), \
             patch.object(self.nm, '_remote_alive', return_value=False), \
             patch.object(self.nm, '_wait_mounted', return_value=True), \
             patch.object(self.nm, '_write_rclone_remote',
                          return_value="pcat_x"), \
             patch.object(self.nm, '_run',
                          side_effect=lambda a, **k: (seen.update(argv=a),
                                                      (0, ""))[1]):
            out = self.nm.mount_remote(remote)
        self.assertTrue(out["ok"])
        self.assertEqual(seen["argv"][0], "rclone")
        self.assertNotIn("SK", " ".join(seen["argv"]))
        self.assertIn("--config", seen["argv"])

    def test_mount_failure_surfaces_message(self):
        self._make_ssh()
        remote = self.nm.list_remotes()[0]
        with patch.object(self.nm.os, 'makedirs'), \
             patch.object(self.nm, '_remote_alive', return_value=False), \
             patch.object(self.nm, '_run',
                          return_value=(1, "Permission denied")):
            out = self.nm.mount_remote(remote)
        self.assertFalse(out["ok"])
        self.assertIn("Permission denied", out["message"])

    def test_delete_unmounts_and_forgets(self):
        self._make_ssh()
        rid = self.nm.list_remotes()[0]["id"]
        with patch.object(self.nm, 'unmount_remote') as um:
            self.nm.delete_remote(rid)
        self.assertTrue(um.called)
        self.assertEqual(self.nm.list_remotes(), [])

    def test_keeper_backoff_doubles_then_wan_recovery_resets(self):
        self._make_ssh()
        remote = self.nm.list_remotes()[0]
        with patch.object(self.nm, '_remote_alive', return_value=False), \
             patch.object(self.nm, 'mount_remote',
                          return_value={"ok": False, "message": "no route"}), \
             patch.dict('sys.modules', {'app': MagicMock(
                 get_active_egress=lambda: "wan")}):
            self.nm.reconcile_remotes()
            first = self.nm._remote_backoff[remote["id"]]["delay"]
            # Still inside the window: no retry, delay unchanged.
            self.nm.reconcile_remotes()
            self.assertEqual(
                self.nm._remote_backoff[remote["id"]]["delay"], first)
            # Force the window open: the next failure doubles the delay.
            self.nm._remote_backoff[remote["id"]]["until"] = 0
            self.nm.reconcile_remotes()
            self.assertEqual(
                self.nm._remote_backoff[remote["id"]]["delay"], first * 2)

        # WAN goes down then comes back: every backoff is cleared so the
        # remount happens on the very next tick.
        with patch.object(self.nm, '_remote_alive', return_value=False), \
             patch.object(self.nm, 'mount_remote',
                          return_value={"ok": False}), \
             patch.dict('sys.modules', {'app': MagicMock(
                 get_active_egress=lambda: None)}):
            self.nm.reconcile_remotes()
        with patch.object(self.nm, '_remote_alive', return_value=True), \
             patch.dict('sys.modules', {'app': MagicMock(
                 get_active_egress=lambda: "wan")}):
            out = self.nm.reconcile_remotes()
        self.assertIn("nas1:ok", out)
        self.assertNotIn(remote["id"], self.nm._remote_backoff)

    def test_keeper_idle_without_remotes(self):
        self.assertEqual(self.nm.reconcile_remotes(), "no_remotes")

    def test_remote_mount_roots_only_live(self):
        self._make_ssh()
        with patch.object(self.nm, '_remote_alive', return_value=True):
            self.assertEqual(self.nm.remote_mount_roots(),
                             ["/mnt/remote/nas1"])
        with patch.object(self.nm, '_remote_alive', return_value=False):
            self.assertEqual(self.nm.remote_mount_roots(), [])


class ShareLinkTests(unittest.TestCase):

    def setUp(self):
        self.cfg = FakeConfig()
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
            'pcat_config': self.cfg,
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.patches = [
            patch.object(self.nm, 'managed_mount_roots',
                         return_value=["/mnt/nvme"]),
            patch.object(self.nm, 'remote_mount_roots', return_value=[]),
            patch.object(self.nm.os.path, 'realpath', side_effect=lambda p: p),
            patch.object(self.nm.os.path, 'exists', return_value=True),
            patch.object(self.nm.os.path, 'isdir', return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.module_patch.stop()

    def test_create_and_find(self):
        out = self.nm.create_link("/mnt/nvme/photos")
        self.assertTrue(out["ok"])
        token = out["link"]["token"]
        self.assertEqual(out["link"]["type"], "dir")
        self.assertEqual(out["link"]["name"], "photos")
        self.assertFalse(out["link"]["has_password"])
        self.assertIsNotNone(self.nm.find_link(token))

    def test_create_rejects_path_off_storage(self):
        out = self.nm.create_link("/etc/shadow")
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "bad_path")

    def test_password_link_never_exposes_hash(self):
        out = self.nm.create_link("/mnt/nvme/secret", password="hunter2")
        self.assertTrue(out["link"]["has_password"])
        self.assertNotIn("password_hash", out["link"])
        stored = self.nm.list_links()[0]
        self.assertTrue(self.nm.check_link_password(stored, "hunter2"))
        self.assertFalse(self.nm.check_link_password(stored, "nope"))

    def test_short_password_rejected(self):
        self.assertEqual(
            self.nm.create_link("/mnt/nvme/x", password="ab")["code"],
            "bad_password")

    def test_tokens_are_unique(self):
        a = self.nm.create_link("/mnt/nvme/a")["link"]["token"]
        b = self.nm.create_link("/mnt/nvme/b")["link"]["token"]
        self.assertNotEqual(a, b)

    def test_delete(self):
        token = self.nm.create_link("/mnt/nvme/x")["link"]["token"]
        self.nm.delete_link(token)
        self.assertIsNone(self.nm.find_link(token))
        self.assertEqual(self.nm.list_links(), [])

    def test_file_link_type(self):
        with patch.object(self.nm.os.path, 'isdir', return_value=False):
            out = self.nm.create_link("/mnt/nvme/a.txt")
        self.assertEqual(out["link"]["type"], "file")


if __name__ == '__main__':
    unittest.main()
