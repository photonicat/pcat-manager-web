#!/usr/bin/env python3
"""Unit tests for the remaining nas_manager surface: status/keeper
aggregation, disk-list caching, SMART parsing, the destructive mklabel and
delete builders, partition_table parsing and the cron/passwd writers.

build_mklabel in particular wipes a whole disk, so its argv (wipefs first,
then the requested table type, and a hard refusal of anything else) is
pinned here rather than trusted.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_status`.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

LSBLK_JSON = """{"blockdevices":[
 {"name":"nvme0n1","type":"disk","size":128000000000,"rm":false,
  "model":"SK hynix","serial":"S1","children":[
   {"name":"nvme0n1p1","type":"part","size":64000000000,"fstype":"ext4",
    "uuid":"u1","label":"data","mountpoint":"/mnt/data"}]}]}"""

SMART_JSON = """{"smart_status":{"passed":true},
 "temperature":{"current":41},"power_on_time":{"hours":1234},
 "ata_smart_attributes":{"table":[
   {"id":5,"name":"Reallocated_Sector_Ct","value":100,
    "raw":{"string":"0"}}]}}"""


class DiskListTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.nm.invalidate_disk_cache()
        self.nm._smart_cache.clear()

    def tearDown(self):
        self.nm.invalidate_disk_cache()
        self.nm._smart_cache.clear()
        self.module_patch.stop()

    @staticmethod
    def _lsblk_calls(calls):
        """Only the lsblk reads. build_disk_model also shells out to blkid
        (the squashfs system-disk check), which is not what these tests are
        about -- counting every _run made the assertion break whenever an
        unrelated probe was added, without the cache being wrong."""
        return [c for c in calls if c and c[0] == "lsblk"]

    def test_list_disks_caches_between_calls(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return 0, LSBLK_JSON
        with patch.object(self.nm, '_run', side_effect=fake_run), \
             patch.object(self.nm, 'automount_uuids', return_value=set()), \
             patch.object(self.nm, 'rootfs_disks', return_value=set()), \
             patch.object(self.nm, '_statvfs_usage', return_value=(1, 2)):
            first = self.nm.list_disks()
            second = self.nm.list_disks()
        # second call served cached -- exactly one lsblk read
        self.assertEqual(len(self._lsblk_calls(calls)), 1)
        self.assertIs(first, second)
        self.assertEqual(first[0]["name"], "nvme0n1")

    def test_invalidate_disk_cache_forces_reread(self):
        calls = []
        with patch.object(self.nm, '_run',
                          side_effect=lambda a, **k: (calls.append(a),
                                                      (0, LSBLK_JSON))[1]), \
             patch.object(self.nm, 'automount_uuids', return_value=set()), \
             patch.object(self.nm, 'rootfs_disks', return_value=set()), \
             patch.object(self.nm, '_statvfs_usage', return_value=(1, 2)):
            self.nm.list_disks()
            self.nm.invalidate_disk_cache()
            self.nm.list_disks()
        self.assertEqual(len(self._lsblk_calls(calls)), 2)

    def test_list_disks_survives_lsblk_failure(self):
        with patch.object(self.nm, '_run', return_value=(1, "")):
            self.assertEqual(self.nm.list_disks(refresh=True), [])

    def test_smart_summary_parses_json(self):
        with patch.object(self.nm, '_which', return_value=True), \
             patch.object(self.nm, '_run', return_value=(0, SMART_JSON)):
            data = self.nm.smart_summary("/dev/nvme0n1")
        self.assertEqual(data["health"], "ok")
        self.assertEqual(data["temp_c"], 41)
        self.assertEqual(data["power_on_hours"], 1234)
        self.assertEqual(data["attrs"][0]["name"], "Reallocated_Sector_Ct")

    def test_smart_summary_none_for_mmc_and_missing_tool(self):
        with patch.object(self.nm, '_which', return_value=True):
            self.assertIsNone(self.nm.smart_summary("/dev/mmcblk0"))
        self.nm._smart_cache.clear()
        with patch.object(self.nm, '_which', return_value=False):
            self.assertIsNone(self.nm.smart_summary("/dev/nvme0n1"))

    def test_smart_summary_failing_disk(self):
        with patch.object(self.nm, '_which', return_value=True), \
             patch.object(self.nm, '_run',
                          return_value=(0, '{"smart_status":{"passed":false}}')):
            self.assertEqual(
                self.nm.smart_summary("/dev/sda")["health"], "fail")


class DestructiveBuilderTests(unittest.TestCase):

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

    def test_build_mklabel_wipes_then_labels(self):
        with patch.object(self.nm, '_which', return_value=True):
            steps = self.nm.build_mklabel("/dev/sda", "gpt")
        self.assertEqual(steps[0], ["wipefs", "-a", "/dev/sda"])
        self.assertEqual(steps[1],
                         ["parted", "-s", "/dev/sda", "mklabel", "gpt"])

    def test_build_mklabel_msdos_allowed(self):
        steps = self.nm.build_mklabel("/dev/sda", "msdos")
        self.assertIn("msdos", steps[-1])

    def test_build_mklabel_refuses_unknown_table(self):
        for bad in ("ext4", "gpt2", "", None):
            with self.assertRaises(self.nm.GuardError, msg=repr(bad)):
                self.nm.build_mklabel("/dev/sda", bad)

    def test_build_delete_argv(self):
        self.assertEqual(self.nm.build_delete("/dev/sda", 3),
                         [["parted", "-s", "/dev/sda", "rm", "3"]])

    def test_mkpart_argv_table_dependence(self):
        gpt = self.nm.mkpart_argv("/dev/sda", "gpt", 0, 100, "myname")
        self.assertIn("myname", gpt)
        msdos = self.nm.mkpart_argv("/dev/sda", "msdos", 0, 100, "myname")
        self.assertIn("primary", msdos)
        self.assertNotIn("myname", msdos)

    def test_partition_table_reads_parted(self):
        out = ("BYT;\n/dev/sda:1000B:scsi:512:512:gpt:X:;\n"
               "1:0B:500B:500B:ext4::;\n")
        with patch.object(self.nm, '_run', return_value=(0, out)):
            table = self.nm.partition_table("/dev/sda")
        self.assertEqual(table["table"], "gpt")
        self.assertEqual(len(table["parts"]), 1)

    def test_partition_table_none_on_failure(self):
        with patch.object(self.nm, '_run', return_value=(1, "")):
            self.assertIsNone(self.nm.partition_table("/dev/sda"))

    def test_run_partition_job_stops_on_failure(self):
        class Handle:
            def __init__(self): self.progress = []
            def check_cancel(self): pass
            def set_progress(self, **kw): self.progress.append(kw)
            def watch_subprocess(self, argv, **kw):
                return 1 if argv[0] == "mkfs.ext4" else 0
        with self.assertRaises(RuntimeError) as ctx:
            self.nm.run_partition_job(
                Handle(), [["parted"], ["mkfs.ext4"], ["never-runs"]])
        self.assertIn("mkfs.ext4", str(ctx.exception))

    def test_run_partition_job_partprobes_after_success(self):
        runs = []

        class Handle:
            def check_cancel(self): pass
            def set_progress(self, **kw): pass
            def watch_subprocess(self, argv, **kw): return 0
        with patch.object(self.nm, '_run',
                          side_effect=lambda a, **k: (runs.append(a),
                                                      (0, ""))[1]), \
             patch.object(self.nm, 'invalidate_disk_cache'), \
             patch.object(self.nm.time, 'sleep'):
            self.nm.run_partition_job(Handle(), [["parted", "-s"]])
        self.assertIn(["partprobe"], runs)


class StatusTests(unittest.TestCase):

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

    def test_status_aggregates_every_section(self):
        with patch.object(self.nm, 'tools_available', return_value={}), \
             patch.object(self.nm, 'list_disks', return_value=[]), \
             patch.object(self.nm, 'samba_state', return_value={}), \
             patch.object(self.nm, 'list_shares', return_value=[]), \
             patch.object(self.nm, 'list_smb_users', return_value=[]):
            st = self.nm.status()
        for key in ("tools", "disks", "samba", "shares", "smb_users",
                    "jobs_active"):
            self.assertIn(key, st)

    def test_reconcile_summary_mentions_remotes(self):
        with patch.object(self.nm, '_assert_samba_lan_bind'), \
             patch.object(self.nm, 'reconcile_remotes', return_value="nas1:ok"), \
             patch.object(self.nm, 'list_disks', return_value=[]), \
             patch.object(self.nm, 'list_shares', return_value=[]):
            out = self.nm.reconcile()
        self.assertIn("remotes:nas1:ok", out)

    def test_public_http_roots_only_valid_flagged_shares(self):
        shares = [
            {"name": "pub", "path": "/mnt/a", "http_browse": True,
             "valid": True},
            {"name": "off", "path": "/mnt/b", "http_browse": False,
             "valid": True},
            {"name": "gone", "path": "/mnt/c", "http_browse": True,
             "valid": False},
        ]
        with patch.object(self.nm, 'list_shares', return_value=shares), \
             patch.object(self.nm.os.path, 'realpath', side_effect=lambda p: p):
            self.assertEqual(self.nm.public_http_roots(), {"pub": "/mnt/a"})

    def test_link_public_strips_hash(self):
        pub = self.nm.link_public({"token": "t", "path": "/mnt/x",
                                   "password_hash": "salt$digest"})
        self.assertNotIn("password_hash", pub)
        self.assertTrue(pub["has_password"])

    def test_apply_cron_writes_and_restarts_only_on_change(self):
        runs = []
        with tempfile.TemporaryDirectory() as tmp:
            cron = os.path.join(tmp, "root")
            with open(cron, "w") as f:
                f.write("0 4 * * * /etc/coremark.sh\n")
            job = {"id": "sabcdef12", "src": "/mnt/a", "dst": "/mnt/b",
                   "schedule": "daily", "enabled": True}
            with patch.object(self.nm, 'CRON_PATH', cron), \
                 patch.object(self.nm, 'list_sync_jobs', return_value=[job]), \
                 patch.object(self.nm, '_run',
                              side_effect=lambda a, **k: (runs.append(a),
                                                          (0, ""))[1]):
                self.nm.apply_cron()
                with open(cron) as f:
                    text = f.read()
                self.assertIn("/etc/coremark.sh", text)
                self.assertIn("# pcat-nas:sabcdef12", text)
                self.assertEqual(len(runs), 1)
                # Second call is a no-op: nothing changed, no cron restart.
                self.nm.apply_cron()
                self.assertEqual(len(runs), 1)

    def test_delete_smb_user_validates_and_strips_from_shares(self):
        self.assertEqual(
            self.nm.delete_smb_user("Bad Name")["code"], "bad_user")
        deletes, sets = [], []
        with patch.object(self.nm, '_run', return_value=(0, "")), \
             patch.object(self.nm, '_samba_reload'), \
             patch.object(self.nm, '_samba_sections', return_value={
                 "pcat_media": {"users": "alice bob"}}), \
             patch.object(self.nm, 'uci_set', side_effect=sets.append), \
             patch.object(self.nm, 'uci_delete', side_effect=deletes.append), \
             patch.object(self.nm, 'uci_commit'), \
             patch('builtins.open',
                   unittest.mock.mock_open(read_data="alice:x:21000:65534::/var/empty:/bin/false\n")), \
             patch.object(self.nm.os, 'replace'):
            out = self.nm.delete_smb_user("alice")
        self.assertTrue(out["ok"])
        self.assertIn("samba4.pcat_media.users=bob", sets)


if __name__ == '__main__':
    unittest.main()
