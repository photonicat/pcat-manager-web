#!/usr/bin/env python3
"""Unit tests for the NAS disk model (pcat_util/nas_manager).

The load-bearing property is protection: the disk(s) backing the running
system must be derived from /proc/mounts — including through a squashfs
loop mount — and every classification path must keep the internal eMMC
protected even when the derivation is degraded. Also pins bus
classification from sysfs paths and the vanished-disk partition matcher
used by the hotplug pass.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_disk_model`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

# photonicat2-shaped fakes: eMMC boot disk, SD card, NVMe data disk.
SYSFS_REAL = {
    "mmcblk0": "/sys/devices/platform/soc/sdhci@fe2e0000/mmc_host/mmc0/"
               "mmc0:0001/block/mmcblk0",
    "mmcblk1": "/sys/devices/platform/soc/dwmmc@fe2c0000/mmc_host/mmc1/"
               "mmc1:aaaa/block/mmcblk1",
    "nvme0n1": "/sys/devices/platform/pcie@fe150000/pci0000:00/0000:01:00.0/"
               "nvme/nvme0/nvme0n1",
    "sda": "/sys/devices/platform/usb@fc000000/usb1/1-1/1-1.2/host0/"
           "target0:0:0/0:0:0:0/block/sda",
}
SYSFS_CLASS = {
    "mmcblk0p8": SYSFS_REAL["mmcblk0"] + "/mmcblk0p8",
    "mmcblk1p1": SYSFS_REAL["mmcblk1"] + "/mmcblk1p1",
    "nvme0n1p1": SYSFS_REAL["nvme0n1"] + "/nvme0n1p1",
    "sda1": SYSFS_REAL["sda"] + "/sda1",
}
SYSFS_READ = {
    # Real photonicat2 values: removable is 0 even for the SD card (the mmc
    # block layer reports the drive, not the media); device/type is the
    # reliable discriminator and what classify_bus must prefer.
    "/sys/block/mmcblk0/removable": "0",
    "/sys/block/mmcblk0/device/type": "MMC",
    "/sys/block/mmcblk1/removable": "0",
    "/sys/block/mmcblk1/device/type": "SD",
}

PROC_MOUNTS = """\
/dev/root / squashfs ro,relatime 0 0
proc /proc proc rw 0 0
/dev/mmcblk0p8 /overlay f2fs rw,noatime 0 0
overlayfs:/overlay / overlay rw 0 0
/dev/nvme0n1p1 /mnt/nvme ext4 rw,noatime 0 0
/dev/mmcblk1p1 /mnt/sdcard vfat rw 0 0
tmpfs /tmp tmpfs rw 0 0
"""

PROC_MOUNTS_LOOP = """\
/dev/loop0 /rom squashfs ro 0 0
/dev/mmcblk0p3 /boot vfat ro 0 0
/dev/mmcblk0p8 /overlay f2fs rw 0 0
/dev/nvme0n1p1 /mnt/nvme ext4 rw 0 0
"""


def fake_realpath(name):
    real = SYSFS_REAL.get(name)
    return real if real else "/sys/block/" + name


def fake_class_realpath(name):
    return SYSFS_CLASS.get(name) or SYSFS_REAL.get(name, "")


def fake_read(path):
    return SYSFS_READ.get(path, "")


class DiskModelTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.openwrt': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_manager
        self.nm = nas_manager
        # parent_disk consults /sys/class/block; route it to the fake maps
        # (partition nodes are exactly the SYSFS_CLASS keys).
        self.sysfs_patches = [
            patch.object(self.nm, 'sysfs_realpath_class',
                         lambda name, sysfs_realpath=None:
                         fake_class_realpath(name)),
            patch.object(self.nm, '_has_partition_attr',
                         lambda name: name in SYSFS_CLASS),
        ]
        for p in self.sysfs_patches:
            p.start()

    def tearDown(self):
        for p in self.sysfs_patches:
            p.stop()
        self.module_patch.stop()

    def test_classify_bus(self):
        cases = {"mmcblk0": "emmc", "mmcblk1": "sd",
                 "nvme0n1": "nvme", "sda": "usb"}
        for name, want in cases.items():
            self.assertEqual(
                self.nm.classify_bus(name, fake_realpath, fake_read),
                want, name)

    def test_loop_and_root_devices_protected(self):
        # The real photonicat2 mounts /overlay from /dev/loop0 (an f2fs
        # image) and /rom from /dev/root — neither resolves to the eMMC via
        # sysfs, and both must still be refused. Regression: /overlay was
        # exposed as a browsable NAS volume on-device.
        for dev in ("loop0", "root", "zram0"):
            self.assertTrue(self.nm.is_protected(
                dev, lambda: PROC_MOUNTS, fake_realpath, fake_read), dev)

    def test_managed_roots_exclude_system_targets(self):
        device_mounts = {
            "/dev/root": "/rom",
            "/dev/loop0": "/overlay",
            "/dev/mmcblk0p1": "/boot",
            "/dev/mmcblk1p1": "/mnt/mmcblk1p1",
            "/dev/nvme0n1p1": "/mnt/nvme0n1p1",
        }
        with patch.object(self.nm, 'mounted_map',
                          return_value=device_mounts), \
             patch.object(self.nm, 'is_protected',
                          side_effect=lambda n, *a: "mmcblk0" in n
                          or n in ("root", "loop0")), \
             patch.object(self.nm.os.path, 'realpath',
                          side_effect=lambda p: p):
            roots = self.nm.managed_mount_roots()
        self.assertEqual(roots, ["/mnt/mmcblk1p1", "/mnt/nvme0n1p1"])

    def test_classify_bus_removable_fallback(self):
        # No device/type attr (mmcblkXboot0 nodes): fall back to removable.
        reads = {"/sys/block/mmcblk0boot0/removable": "0",
                 "/sys/block/mmcblk2/removable": "1"}
        self.assertEqual(self.nm.classify_bus(
            "mmcblk0boot0", fake_realpath, reads.get), "emmc")
        self.assertEqual(self.nm.classify_bus(
            "mmcblk2", fake_realpath, reads.get), "sd")

    def test_parent_disk(self):
        self.assertEqual(self.nm.parent_disk("mmcblk0p8"), "mmcblk0")
        self.assertEqual(self.nm.parent_disk("nvme0n1p1"), "nvme0n1")
        self.assertEqual(self.nm.parent_disk("nvme0n1"), "nvme0n1")

    def test_rootfs_disks_plain(self):
        disks = self.nm.rootfs_disks(lambda: PROC_MOUNTS, fake_realpath)
        self.assertIn("mmcblk0", disks)
        self.assertNotIn("nvme0n1", disks)
        self.assertNotIn("mmcblk1", disks)

    def test_rootfs_disks_via_loop(self):
        # /rom is a squashfs loop over a file living on the eMMC overlay.
        disks = self.nm.rootfs_disks(
            lambda: PROC_MOUNTS_LOOP, fake_realpath,
            loop_backing=lambda name: "/overlay/rom.img")
        self.assertIn("mmcblk0", disks)
        self.assertNotIn("nvme0n1", disks)

    def test_is_protected_matrix(self):
        # Whole eMMC + system partitions locked; data partitions on the same
        # disk and all external storage remain usable.
        with patch.object(self.nm, 'blkid_type', return_value=""):
            for dev, want in (("mmcblk0", True), ("mmcblk0p8", True),
                              ("mmcblk1", False), ("mmcblk1p1", False),
                              ("nvme0n1", False), ("nvme0n1p1", False),
                              ("sda1", False)):
                got = self.nm.is_protected(
                    dev, lambda: PROC_MOUNTS, fake_realpath, fake_read)
                self.assertEqual(got, want, dev)

    def test_emmc_data_partition_not_protected(self):
        # Free-space / expanded data volume on eMMC (not mounted as system).
        with patch.object(self.nm, 'blkid_type', return_value=""), \
             patch.object(self.nm, '_has_partition_attr',
                          lambda n: n.startswith("mmcblk0p")):
            self.assertFalse(self.nm.is_protected(
                "mmcblk0p3", lambda: PROC_MOUNTS, fake_realpath, fake_read))

    def test_emmc_squashfs_partition_protected_via_blkid(self):
        with patch.object(self.nm, 'blkid_type', return_value="squashfs"), \
             patch.object(self.nm, '_has_partition_attr',
                          lambda n: n.startswith("mmcblk0p")):
            self.assertTrue(self.nm.is_protected(
                "mmcblk0p2", lambda: "", fake_realpath, fake_read))

    def test_emmc_protected_even_without_rootfs_evidence(self):
        # Degraded derivation (empty /proc/mounts) must still refuse whole eMMC.
        self.assertTrue(self.nm.is_protected(
            "mmcblk0", lambda: "", fake_realpath, fake_read))
        self.assertFalse(self.nm.is_protected(
            "nvme0n1", lambda: "", fake_realpath, fake_read))

    def test_belongs_to_disk(self):
        self.assertTrue(self.nm.belongs_to_disk("mmcblk1p1", "mmcblk1"))
        self.assertTrue(self.nm.belongs_to_disk("sda1", "sda"))
        self.assertTrue(self.nm.belongs_to_disk("sda", "sda"))
        self.assertFalse(self.nm.belongs_to_disk("mmcblk1p1", "mmcblk0"))
        self.assertFalse(self.nm.belongs_to_disk("sdab1", "sda"))
        self.assertFalse(self.nm.belongs_to_disk("nvme0n11", "nvme0n1"))
        # nvme partition suffix is 'p1', which the matcher accepts
        self.assertTrue(self.nm.belongs_to_disk("nvme0n1p1", "nvme0n1"))

    def test_build_disk_model(self):
        lsblk = {"blockdevices": [
            {"name": "mmcblk0", "type": "disk", "size": 64000000000,
             "rm": False, "model": None, "serial": None, "children": [
                 {"name": "mmcblk0p8", "type": "part", "size": 60000000000,
                  "fstype": "f2fs", "uuid": "aaaa-bbbb", "label": None,
                  "mountpoint": "/overlay"}]},
            {"name": "nvme0n1", "type": "disk", "size": 512000000000,
             "rm": False, "model": "Samsung SSD 970", "serial": "S1234",
             "children": [
                 {"name": "nvme0n1p1", "type": "part", "size": 512000000000,
                  "fstype": "ext4", "uuid": "cccc-dddd", "label": "data",
                  "mountpoint": None}]},
            {"name": "loop0", "type": "disk", "size": 1000, "children": []},
            {"name": "mmcblk0boot0", "type": "disk", "size": 4194304,
             "rm": False, "children": []},
        ]}
        with patch.object(self.nm, '_statvfs_usage',
                          return_value=(None, None)), \
             patch.object(self.nm, 'blkid_type', return_value=""):
            disks = self.nm.build_disk_model(
                lsblk, {"cccc-dddd"}, lambda: PROC_MOUNTS,
                fake_realpath, fake_read)
        names = [d["name"] for d in disks]
        self.assertEqual(names, ["nvme0n1", "mmcblk0"])  # nvme sorts first
        emmc = next(d for d in disks if d["name"] == "mmcblk0")
        nvme = next(d for d in disks if d["name"] == "nvme0n1")
        self.assertTrue(emmc["protected"])
        self.assertFalse(nvme["protected"])
        self.assertEqual(nvme["model"], "Samsung SSD 970")
        self.assertTrue(nvme["partitions"][0]["automount"])
        self.assertFalse(emmc["partitions"][0]["automount"])
        self.assertTrue(emmc["partitions"][0]["mounted"])
        # overlay partition on eMMC is system-protected; NVMe data is not.
        self.assertTrue(emmc["partitions"][0]["protected"])
        self.assertFalse(nvme["partitions"][0]["protected"])


if __name__ == '__main__':
    unittest.main()
