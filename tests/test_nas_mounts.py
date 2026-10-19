#!/usr/bin/env python3
"""Unit tests for NAS mount lifecycle, hotplug and samba service control.

These cover the functions that shell out or touch UCI — the ones the
earlier NAS test files skipped in favour of pure logic, which is exactly
where the msdos-mkpart bug hid. Subprocess and UCI are mocked; what is
asserted is the argv/UCI sequence and the branch taken, so a wrong flag or
a missing guard fails here rather than on the device.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_mounts`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

DISKS = [{
    "dev": "/dev/nvme0n1", "name": "nvme0n1", "bus": "nvme",
    "protected": False, "size_bytes": 128 * 10**9, "unallocated_bytes": 0,
    "partitions": [{"dev": "/dev/nvme0n1p1", "uuid": "u-1", "label": "data",
                    "fs": "ext4", "mounted": False, "mountpoint": None,
                    "size_bytes": 128 * 10**9, "avail_bytes": None,
                    "automount": False, "protected": False}],
}, {
    "dev": "/dev/mmcblk0", "name": "mmcblk0", "bus": "emmc",
    "protected": True, "size_bytes": 125 * 10**9, "unallocated_bytes": 0,
    "partitions": [
        {"dev": "/dev/mmcblk0p1", "uuid": "u-boot", "label": None,
         "fs": "ext2", "mounted": True, "mountpoint": "/boot",
         "size_bytes": 67 * 10**6, "avail_bytes": 1,
         "automount": False, "protected": True},
        {"dev": "/dev/mmcblk0p3", "uuid": "u-data", "label": "pcat-data",
         "fs": "ext4", "mounted": False, "mountpoint": None,
         "size_bytes": 112 * 10**9, "avail_bytes": None,
         "automount": False, "protected": False},
    ],
}]


class MountTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.runs = []
        self.uci_set = []
        self.uci_del = []
        self.patches = [
            patch.object(self.nm, '_run',
                         side_effect=lambda a, **k: (self.runs.append(a),
                                                     (0, ""))[1]),
            patch.object(self.nm, 'list_disks', return_value=DISKS),
            patch.object(self.nm, 'uci_set',
                         side_effect=self.uci_set.append),
            patch.object(self.nm, 'uci_delete',
                         side_effect=self.uci_del.append),
            patch.object(self.nm, 'uci_commit'),
            patch.object(self.nm, 'uci_show_dict', return_value={}),
            patch.object(self.nm, 'uci_get',
                         side_effect=lambda k: "/mnt/data"
                         if k.endswith(".target") else ""),
            patch.object(self.nm.os, 'makedirs'),
            patch.object(self.nm, 'invalidate_disk_cache'),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.module_patch.stop()

    def test_enable_automount_writes_uci_and_mounts(self):
        out = self.nm.enable_automount("/dev/nvme0n1p1")
        self.assertTrue(out["ok"])
        joined = " ".join(self.uci_set)
        self.assertIn("=mount", joined)
        self.assertIn(".uuid=u-1", joined)
        self.assertIn(".enabled=1", joined)
        self.assertIn(["block", "mount"], self.runs)

    def test_enable_automount_refuses_protected(self):
        out = self.nm.enable_automount("/dev/mmcblk0p1")
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "protected")
        self.assertEqual(self.uci_set, [])

    def test_enable_automount_needs_uuid(self):
        disks = [dict(DISKS[0])]
        disks[0]["partitions"] = [dict(DISKS[0]["partitions"][0], uuid=None)]
        with patch.object(self.nm, 'list_disks', return_value=disks):
            out = self.nm.enable_automount("/dev/nvme0n1p1")
        self.assertEqual(out["code"], "no_uuid")

    def test_enable_automount_unknown_device(self):
        self.assertEqual(
            self.nm.enable_automount("/dev/zzz9")["code"], "bad_dev")

    def test_disable_automount_removes_section(self):
        with patch.object(self.nm, '_find_mount_section',
                          return_value="pcat_u1"):
            out = self.nm.disable_automount("/dev/nvme0n1p1")
        self.assertTrue(out["ok"])
        self.assertIn("fstab.pcat_u1", self.uci_del)

    def test_mount_now_refuses_protected_and_mounts_others(self):
        self.assertEqual(
            self.nm.mount_now("/dev/mmcblk0p1")["code"], "protected")
        out = self.nm.mount_now("/dev/nvme0n1p1")
        self.assertTrue(out["ok"])
        self.assertTrue(any(a[0] == "mount" for a in self.runs))

    def test_mount_emmc_data_partition_allowed(self):
        # Free-space data volume on eMMC is not system-protected. Stub
        # is_protected so the unit test does not depend on live /proc/mounts.
        with patch.object(self.nm, 'is_protected',
                          side_effect=lambda n, *a: n in ("mmcblk0", "mmcblk0p1")):
            out = self.nm.mount_now("/dev/mmcblk0p3")
        self.assertTrue(out["ok"], out)
        self.assertTrue(any(a[0] == "mount" and a[1] == "/dev/mmcblk0p3"
                            for a in self.runs))

    def test_automount_emmc_data_partition_allowed(self):
        with patch.object(self.nm, 'is_protected',
                          side_effect=lambda n, *a: n in ("mmcblk0", "mmcblk0p1")):
            out = self.nm.enable_automount("/dev/mmcblk0p3")
        self.assertTrue(out["ok"], out)

    def test_mount_now_reports_failure(self):
        with patch.object(self.nm, '_run', return_value=(1, "no such device")):
            out = self.nm.mount_now("/dev/nvme0n1p1")
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "mount_failed")

    def test_unmount_rejects_unmanaged_path(self):
        with patch.object(self.nm, 'managed_mount_roots',
                          return_value=["/mnt/data"]):
            out = self.nm.unmount("/etc")
        self.assertEqual(out["code"], "bad_path")

    def test_unmount_busy_without_force(self):
        with patch.object(self.nm, 'managed_mount_roots',
                          return_value=["/mnt/data"]), \
             patch.object(self.nm, '_which', return_value=False), \
             patch.object(self.nm, '_procs_using', return_value=[123]):
            out = self.nm.unmount("/mnt/data")
        self.assertEqual(out["code"], "busy")
        self.assertEqual(out["pids"], [123])

    def test_unmount_force_disables_shares_and_lazy_unmounts(self):
        calls = []
        with patch.object(self.nm, 'managed_mount_roots',
                          return_value=["/mnt/data"]), \
             patch.object(self.nm, '_disable_shares_under',
                          side_effect=calls.append), \
             patch.object(self.nm, '_run',
                          side_effect=lambda a, **k:
                          (0, "") if a[:2] == ["umount", "-l"] else (1, "busy")):
            out = self.nm.unmount("/mnt/data", force=True)
        self.assertTrue(out["ok"])
        self.assertTrue(out.get("detached"))
        self.assertEqual(calls, ["/mnt/data"])

    def test_mounted_map_parses_only_block_sources(self):
        proc = ("/dev/sda1 /mnt/a ext4 rw 0 0\n"
                "tmpfs /tmp tmpfs rw 0 0\n"
                "overlayfs:/overlay / overlay rw 0 0\n")
        # The reader is an injectable default arg, so pass it directly rather
        # than patching the module attribute (which the default already bound).
        self.assertEqual(self.nm.mounted_map(lambda: proc),
                         {"/dev/sda1": "/mnt/a"})

    def test_automount_uuids_only_our_sections(self):
        cfg = {"fstab.pcat_aa.uuid": "u-1", "fstab.pcat_aa": "mount",
               "fstab.cfg030936.uuid": "u-foreign",
               "fstab.@global[0].anon_mount": "1"}
        with patch.object(self.nm, 'uci_show_dict', return_value=cfg):
            self.assertEqual(self.nm.automount_uuids(), {"u-1"})


class HotplugTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.nm._hotplug_snapshot = None

    def tearDown(self):
        self.nm._hotplug_snapshot = None
        self.module_patch.stop()

    def test_first_pass_seeds_snapshot_only(self):
        with patch.object(self.nm.os, 'listdir',
                          return_value=["mmcblk0", "loop0"]):
            self.assertEqual(self.nm.reconcile_hotplug(), "idle")
        self.assertEqual(self.nm._hotplug_snapshot, {"mmcblk0"})

    def test_no_change_is_idle(self):
        self.nm._hotplug_snapshot = {"mmcblk0"}
        with patch.object(self.nm.os, 'listdir', return_value=["mmcblk0"]):
            self.assertEqual(self.nm.reconcile_hotplug(), "idle")

    def test_appearance_spawns_worker(self):
        self.nm._hotplug_snapshot = {"mmcblk0"}
        with patch.object(self.nm.os, 'listdir',
                          return_value=["mmcblk0", "sda"]), \
             patch.object(self.nm.threading, 'Thread') as thread:
            out = self.nm.reconcile_hotplug()
        self.assertIn("appeared:sda", out)
        self.assertTrue(thread.called)
        # Snapshot updates immediately so the next tick doesn't re-fire.
        self.assertEqual(self.nm._hotplug_snapshot, {"mmcblk0", "sda"})

    def test_removal_unmounts_and_drops_shares(self):
        shares, runs = [], []
        with patch.object(self.nm, 'mounted_map',
                          return_value={"/dev/sda1": "/mnt/usb"}), \
             patch.object(self.nm, '_disable_shares_under',
                          side_effect=shares.append), \
             patch.object(self.nm, '_run',
                          side_effect=lambda a, **k: (runs.append(a),
                                                      (0, ""))[1]), \
             patch.object(self.nm, 'invalidate_disk_cache'), \
             patch.object(self.nm, '_emit'):
            self.nm._handle_hotplug_change(set(), {"sda"})
        self.assertEqual(shares, ["/mnt/usb"])
        self.assertIn(["umount", "-l", "/mnt/usb"], runs)

    def test_sysfs_unavailable(self):
        with patch.object(self.nm.os, 'listdir', side_effect=OSError):
            self.assertEqual(self.nm.reconcile_hotplug(), "sysfs_unavailable")


class SambaServiceTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.runs = []
        self.run_patch = patch.object(
            self.nm, '_run',
            side_effect=lambda a, **k: (self.runs.append(a), (0, ""))[1])
        self.run_patch.start()

    def tearDown(self):
        self.run_patch.stop()
        self.module_patch.stop()

    def test_samba_service_actions(self):
        self.assertTrue(self.nm.samba_service("enable")["ok"])
        self.assertIn(["/etc/init.d/samba4", "start"], self.runs)
        self.runs.clear()
        self.assertTrue(self.nm.samba_service("disable")["ok"])
        self.assertIn(["/etc/init.d/samba4", "stop"], self.runs)
        self.runs.clear()
        self.assertTrue(self.nm.samba_service("restart")["ok"])
        self.assertIn(["/etc/init.d/samba4", "restart"], self.runs)
        self.assertEqual(self.nm.samba_service("nonsense")["code"],
                         "bad_action")

    def test_samba_state_reports_installed(self):
        with patch.object(self.nm, '_which', return_value=True), \
             patch.object(self.nm, '_safe_listdir', return_value=[]):
            st = self.nm.samba_state()
        self.assertTrue(st["installed"])
        self.assertTrue(st["running"])  # mocked pgrep returns 0

    def test_samba_state_absent(self):
        with patch.object(self.nm, '_which', return_value=False):
            st = self.nm.samba_state()
        self.assertFalse(st["installed"])
        self.assertFalse(st["running"])

    def test_list_smb_users_parses_pdbedit(self):
        with patch.object(self.nm, '_run',
                          return_value=(0, "alice:21000:Alice\nbob:21001:B\n")):
            self.assertEqual(self.nm.list_smb_users(), ["alice", "bob"])

    def test_list_smb_users_when_pdbedit_missing(self):
        with patch.object(self.nm, '_run', return_value=(1, "")):
            self.assertEqual(self.nm.list_smb_users(), [])

    def test_set_smb_user_validates(self):
        self.assertEqual(self.nm.set_smb_user("Bad Name", "pw12")["code"],
                         "bad_user")
        self.assertEqual(self.nm.set_smb_user("alice", "x")["code"],
                         "bad_password")

    def test_set_smb_user_password_via_stdin_not_argv(self):
        seen = {}

        def fake_run(argv, timeout=10, stdin_data=None):
            seen["argv"] = argv
            seen["stdin"] = stdin_data
            return 0, ""
        with patch.object(self.nm, '_ensure_system_user', return_value=True), \
             patch.object(self.nm, '_run', side_effect=fake_run), \
             patch.object(self.nm, '_samba_reload'):
            out = self.nm.set_smb_user("alice", "hunter2")
        self.assertTrue(out["ok"])
        self.assertNotIn("hunter2", " ".join(seen["argv"]))
        self.assertEqual(seen["stdin"], "hunter2\nhunter2\n")

    def test_tools_available_shape(self):
        """Every advertised tool reports a plain bool when its binaries exist.

        Asserts a required *subset* rather than an exact set: this pinned an
        exact list and broke each time a formatter was added (btrfs, f2fs,
        zfs, ext4, vfat) without anything actually being wrong. The contract
        the UI depends on is that these keys are present and boolean, not that
        the dict never grows.
        """
        with patch.object(self.nm, '_which', return_value=True):
            tools = self.nm.tools_available()
        self.assertTrue({"smart", "ntfs", "exfat", "samba", "rsync"} <= set(tools))
        self.assertTrue(all(tools.values()))
        # bool, not a truthy path string -- the JSON API and the UI both
        # expect a real boolean here.
        for name, value in tools.items():
            self.assertIsInstance(value, bool, "{} is not a bool".format(name))


if __name__ == '__main__':
    unittest.main()
