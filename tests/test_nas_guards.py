#!/usr/bin/env python3
"""Unit tests for NAS partition guards, parted parsing and command builders.

partition_guard is the wall between the UI and a bricked router: everything
destructive passes it at submit AND at job start. These pin the guard
matrix (protected disk, the create_free carve-out, mounted/busy/swap/confirm
token) and the exact argv the builders emit — including the shrink sequence's
interactive-parted stdin payload.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_guards`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

PARTED_FREE_OUTPUT = """BYT;
/dev/mmcblk0:125074145280B:sd/mmc:512:512:gpt:MMC BJTD4R:;
1:17408B:67126271B:67108864B:ext4:kernel:;
2:67126272B:4362076159B:4294949888B::rootfs:;
3:4362076160B:4362077183B:1024B:free;
4:4362077184B:125074128895B:120712051712B:free;
"""


class PartedParseTests(unittest.TestCase):

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

    def test_parse_free_regions_and_parts(self):
        table = self.nm.parse_parted_free(PARTED_FREE_OUTPUT)
        self.assertEqual(table["table"], "gpt")
        self.assertEqual([p["num"] for p in table["parts"]], [1, 2])
        # The 1 KiB scrap is dropped; the 120 GB region survives.
        self.assertEqual(len(table["free"]), 1)
        self.assertEqual(table["free"][0]["size"], 120712051712)

    def test_use_free_plan(self):
        table = self.nm.parse_parted_free(PARTED_FREE_OUTPUT)
        with patch.object(self.nm, 'find_orphan_data_part', return_value=None):
            steps, part_dev = self.nm.build_use_free_plan("/dev/mmcblk0", table)
        self.assertEqual(part_dev, "/dev/mmcblk0p3")
        self.assertEqual(len(steps), 1)  # gpt already: no mklabel
        self.assertEqual(steps[0][:5],
                         ["parted", "-s", "-a", "optimal", "/dev/mmcblk0"])
        self.assertIn("mkpart", steps[0])
        self.assertIn("4362077184B", steps[0])

    def test_use_free_plan_msdos_uses_partition_type_not_name(self):
        # Regression: the photonicat2 eMMC ships an MSDOS table, where
        # `mkpart` expects primary/logical — passing the GPT-style name made
        # parted fail with "invalid token: pcat-data".
        table = self.nm.parse_parted_free(
            PARTED_FREE_OUTPUT.replace(":gpt:", ":msdos:"))
        with patch.object(self.nm, 'find_orphan_data_part', return_value=None):
            steps, part_dev = self.nm.build_use_free_plan("/dev/mmcblk0", table)
        self.assertIn("primary", steps[-1])
        self.assertNotIn(self.nm.DATA_PART_LABEL, steps[-1])
        self.assertEqual(part_dev, "/dev/mmcblk0p3")

    def test_use_free_plan_gpt_uses_name(self):
        table = self.nm.parse_parted_free(PARTED_FREE_OUTPUT)
        with patch.object(self.nm, 'find_orphan_data_part', return_value=None):
            steps, _dev = self.nm.build_use_free_plan("/dev/mmcblk0", table)
        self.assertIn(self.nm.DATA_PART_LABEL, steps[-1])
        self.assertNotIn("primary", steps[-1])

    def test_msdos_primary_limit(self):
        table = self.nm.parse_parted_free(
            PARTED_FREE_OUTPUT.replace(":gpt:", ":msdos:"))
        table["parts"] = [{"num": n, "start": 0, "end": 1, "size": 1,
                           "fs": None} for n in (1, 2, 3, 4)]
        with patch.object(self.nm, 'find_orphan_data_part', return_value=None), \
             self.assertRaises(self.nm.GuardError) as ctx:
            self.nm.build_use_free_plan("/dev/mmcblk0", table)
        self.assertEqual(ctx.exception.code, "msdos_full")

    def test_part_device_path_naming(self):
        self.assertEqual(self.nm.part_device_path("/dev/mmcblk0", 3),
                         "/dev/mmcblk0p3")
        self.assertEqual(self.nm.part_device_path("/dev/nvme0n1", 1),
                         "/dev/nvme0n1p1")
        self.assertEqual(self.nm.part_device_path("/dev/sda", 2), "/dev/sda2")

    def test_build_create_respects_table_type(self):
        gpt = {"table": "gpt", "parts": [],
               "free": [{"start": 0, "end": 10**12, "size": 10**12}]}
        msdos = dict(gpt, table="msdos")
        self.assertIn("pcat", self.nm.build_create(
            "/dev/sda", 1000, 10**11, gpt)[0])
        self.assertIn("primary", self.nm.build_create(
            "/dev/sda", 1000, 10**11, msdos)[0])

    def test_use_free_resumes_after_half_finished_run(self):
        # Regression: the first on-device run created mmcblk0p3 and then died
        # on a missing wipefs. Re-running tried to mkpart the same range and
        # failed with "Can't have overlapping partitions" — permanently
        # stuck. The plan must instead resume at the mkfs step.
        table = self.nm.parse_parted_free(PARTED_FREE_OUTPUT)
        table["parts"].append({"num": 3, "start": 4362077184,
                               "end": 125074128895, "size": 120712051712,
                               "fs": None})
        table["free"] = []
        with patch.object(self.nm, 'is_protected', return_value=False), \
             patch.object(self.nm, 'blkid_type', return_value=""):
            steps, part_dev = self.nm.build_use_free_plan("/dev/mmcblk0",
                                                          table)
        self.assertEqual(steps, [])            # nothing for parted to do
        self.assertEqual(part_dev, "/dev/mmcblk0p3")

    def test_resume_never_adopts_the_rootfs(self):
        # The real photonicat2 hazard: parted reports an EMPTY fs column for
        # the squashfs rootfs (partition 2), so an "fs is blank" rule would
        # hand the running system over to mkfs. blkid knows better, and only
        # the last partition is ever a candidate anyway.
        table = self.nm.parse_parted_free(PARTED_FREE_OUTPUT)
        self.assertIsNone(table["parts"][1]["fs"])   # parted says nothing
        with patch.object(self.nm, 'is_protected', return_value=False), \
             patch.object(self.nm, 'blkid_type', return_value="squashfs"):
            self.assertIsNone(self.nm.find_orphan_data_part(
                "/dev/mmcblk0", table))

    def test_resume_ignores_formatted_last_partition(self):
        table = self.nm.parse_parted_free(PARTED_FREE_OUTPUT)
        table["parts"].append({"num": 3, "start": 4362077184,
                               "end": 125074128895, "size": 120712051712,
                               "fs": "ext4"})
        with patch.object(self.nm, 'is_protected', return_value=False):
            self.assertIsNone(self.nm.find_orphan_data_part(
                "/dev/mmcblk0", table))

    def test_blkid_type_parsing(self):
        with patch.object(self.nm, '_run', return_value=(
                0, '/dev/sda1: TYPE="ext4" PARTUUID="x"')):
            self.assertEqual(self.nm.blkid_type("/dev/sda1"), "ext4")
        with patch.object(self.nm, '_run', return_value=(
                0, '/dev/sda1: PARTUUID="x"')):
            self.assertEqual(self.nm.blkid_type("/dev/sda1"), "")
        with patch.object(self.nm, '_run', return_value=(2, "")):
            self.assertEqual(self.nm.blkid_type("/dev/sda1"), "")

    def test_format_skips_wipefs_when_absent(self):
        # util-linux-wipefs is not in this firmware; mkfs -F/-f overwrites
        # the superblock anyway, so its absence must not fail the job.
        #
        # Mock wipefs alone, not every tool: a blanket _which=False also
        # empties tools_available(), so build_format refuses with "mkfs for
        # ext4 is not installed" and this test never reaches what it is about.
        # It also made the result depend on whether the *host* had mkfs.ext4.
        absent_wipefs = lambda tool: tool != "wipefs"
        with patch.object(self.nm, '_which', side_effect=absent_wipefs), \
             patch.object(self.nm, 'tools_available', return_value={"ext4": True}):
            steps = self.nm.build_format("/dev/sda1", "ext4")
        self.assertTrue(all(s[0] != "wipefs" for s in steps))
        self.assertEqual(steps[0][0], "mkfs.ext4")
        with patch.object(self.nm, '_which', return_value=True), \
             patch.object(self.nm, 'tools_available', return_value={"ext4": True}):
            steps = self.nm.build_format("/dev/sda1", "ext4")
        self.assertEqual(steps[0][0], "wipefs")

    def test_mklabel_skips_wipefs_when_absent(self):
        with patch.object(self.nm, '_which', return_value=False):
            steps = self.nm.build_mklabel("/dev/sda", "gpt")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][0], "parted")

    def test_use_free_plan_no_space(self):
        with self.assertRaises(self.nm.GuardError):
            self.nm.build_use_free_plan(
                "/dev/sda", {"table": "gpt", "parts": [], "free": []})

    def test_next_partition_number(self):
        table = self.nm.parse_parted_free(PARTED_FREE_OUTPUT)
        self.assertEqual(self.nm.next_partition_number(table), 3)
        self.assertEqual(self.nm.next_partition_number(
            {"parts": [], "free": []}), 1)


class BuilderTests(unittest.TestCase):

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

    def test_format_builders(self):
        # Modelled with wipefs present; the absent case has its own test.
        with patch.object(self.nm, '_which', return_value=True):
            steps = self.nm.build_format("/dev/sda1", "ext4", "media")
            self.assertEqual(steps[0], ["wipefs", "-a", "/dev/sda1"])
            self.assertEqual(steps[1],
                             ["mkfs.ext4", "-F", "-L", "media", "/dev/sda1"])
            steps = self.nm.build_format("/dev/sda1", "vfat", None)
            self.assertEqual(steps[1], ["mkfs.vfat", "-F", "32", "/dev/sda1"])
        with self.assertRaises(self.nm.GuardError):
            self.nm.build_format("/dev/sda1", "ntfs")
        with self.assertRaises(self.nm.GuardError):
            self.nm.build_format("/dev/sda1", "ext4", "bad label!")

    def test_create_inside_free_region_only(self):
        table = {"table": "gpt", "parts": [],
                 "free": [{"start": 1000000, "end": 900000000000,
                           "size": 899999000000}]}
        steps = self.nm.build_create("/dev/sda", 1000000, 500000000, table)
        self.assertIn("mkpart", steps[0])
        with self.assertRaises(self.nm.GuardError):  # outside free
            self.nm.build_create("/dev/sda", 0, 500000000, table)
        with self.assertRaises(self.nm.GuardError):  # too small
            self.nm.build_create("/dev/sda", 1000000, 2000000, table)

    def test_resize_grow_and_shrink_sequences(self):
        steps, stdin = self.nm.build_resize("/dev/sda", "/dev/sda1", 1,
                                            50_000_000_000, grow=True)
        self.assertIsNone(stdin)
        self.assertEqual(steps[0][:4],
                         ["parted", "-s", "/dev/sda", "resizepart"])
        self.assertEqual(steps[1], ["e2fsck", "-f", "-y", "/dev/sda1"])
        self.assertEqual(steps[2], ["resize2fs", "/dev/sda1"])

        steps, stdin = self.nm.build_resize("/dev/sda", "/dev/sda1", 1,
                                            10_000_000_000, grow=False)
        self.assertEqual(stdin, "Yes\n")
        self.assertEqual(steps[0], ["e2fsck", "-f", "-y", "/dev/sda1"])
        self.assertEqual(steps[1][:2], ["resize2fs", "/dev/sda1"])
        self.assertIn("---pretend-input-tty", steps[2])
        self.assertEqual(steps[3], ["e2fsck", "-f", "-y", "/dev/sda1"])


class GuardTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        self.patches = [
            patch.object(self.nm.os.path, 'exists', return_value=True),
            patch.object(self.nm, 'parent_disk',
                         side_effect=lambda n, *a, **k:
                         n.split("p")[0] if "mmcblk" in n or "nvme" in n
                         else n.rstrip("0123456789")),
            patch.object(self.nm, 'is_protected',
                         side_effect=lambda n, *a: "mmcblk0" in n),
            patch.object(self.nm, 'classify_bus',
                         side_effect=lambda n, *a:
                         "emmc" if "mmcblk0" in n else "usb"),
            patch.object(self.nm, 'mounted_map',
                         return_value={"/dev/sda1": "/mnt/sda1"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.module_patch.stop()

    def _guard(self, dev, op, confirm=None):
        return self.nm.partition_guard(dev, op, confirm=confirm)

    def test_protected_disk_refused_except_create_free(self):
        for op in ("delete", "format", "resize", "create"):
            with self.assertRaises(self.nm.GuardError, msg=op):
                self._guard("/dev/mmcblk0p2", op)
        # The one carve-out: creating in free space on the eMMC.
        self.assertEqual(self._guard("/dev/mmcblk0", "create_free"),
                         "/dev/mmcblk0")

    def test_mounted_partition_refused(self):
        with self.assertRaises(self.nm.GuardError) as ctx:
            self._guard("/dev/sda1", "format")
        self.assertEqual(ctx.exception.code, "mounted")

    def test_disk_with_mounts_refuses_create(self):
        with self.assertRaises(self.nm.GuardError) as ctx:
            self._guard("/dev/sda", "create")
        self.assertEqual(ctx.exception.code, "busy")

    def test_confirm_token(self):
        with self.assertRaises(self.nm.GuardError) as ctx:
            self._guard("/dev/sdb1", "format", confirm="wrong")
        self.assertEqual(ctx.exception.code, "confirm_mismatch")
        self._guard("/dev/sdb1", "format", confirm="sdb")  # ok

    def test_missing_device(self):
        self.patches[0].stop()
        try:
            with patch.object(self.nm.os.path, 'exists', return_value=False):
                with self.assertRaises(self.nm.GuardError) as ctx:
                    self._guard("/dev/sdz1", "format")
                self.assertEqual(ctx.exception.code, "bad_dev")
        finally:
            self.patches[0] = patch.object(self.nm.os.path, 'exists',
                                           return_value=True)
            self.patches[0].start()


if __name__ == '__main__':
    unittest.main()
