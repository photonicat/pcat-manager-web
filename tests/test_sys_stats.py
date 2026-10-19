#!/usr/bin/env python3
"""Unit tests for app/pcat_util/sys_stats.py -- the /stats system badges.

Pins the /proc parsers against real photonicat2 output (8 cores, 16 GB,
overlay root) so a format surprise shows up here and not as a blank badge.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_sys_stats`.
"""

import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

from pcat_util import sys_stats  # noqa: E402

PROC_STAT = """\
cpu  2895858 0 2293744 72285970 10549 668384 1098438 0 0 0
cpu0 361982 0 286718 9035746 1318 83548 137304 0 0 0
intr 123 0
"""
MEMINFO = """\
MemTotal:       16370064 kB
MemFree:        15184680 kB
MemAvailable:   15527212 kB
Buffers:           12345 kB
"""
MOUNTS = """\
/dev/root /rom squashfs ro,relatime 0 0
proc /proc proc rw,nosuid 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev,noatime 0 0
/dev/loop0 /overlay f2fs rw,lazytime,noatime 0 0
overlayfs:/overlay / overlay rw,noatime,lowerdir=/,upperdir=/overlay/upper 0 0
tmpfs /dev tmpfs rw,nosuid,relatime,size=512k 0 0
/dev/mmcblk0p1 /boot ext4 rw,relatime 0 0
/dev/sda1 /mnt/sda1 vfat rw,relatime 0 0
nfsd /proc/fs/nfsd nfsd rw,relatime 0 0
"""
CONNTRACK = """\
ipv6     10 udp      17 9 src=::1 dst=::1 sport=58580 dport=15353 packets=1 bytes=90
ipv4     2 tcp      6 431999 ESTABLISHED src=192.168.20.5 dst=1.1.1.1 sport=5 dport=443
ipv4     2 tcp      6 10 TIME_WAIT src=192.168.20.6 dst=1.1.1.1 sport=6 dport=80
ipv4     2 udp      17 29 src=192.168.20.7 dst=8.8.8.8 sport=7 dport=53
"""


class ProcStatTests(unittest.TestCase):
    def test_busy_and_total_from_cpu_line(self):
        busy, total = sys_stats.parse_proc_stat(PROC_STAT)
        self.assertEqual(total, 2895858 + 0 + 2293744 + 72285970 + 10549 + 668384 + 1098438 + 0)
        self.assertEqual(busy, total - 72285970 - 10549)

    def test_garbage_is_zero(self):
        self.assertEqual(sys_stats.parse_proc_stat("cpu x y z w"), (0, 0))
        self.assertEqual(sys_stats.parse_proc_stat(""), (0, 0))

    def test_loadavg(self):
        self.assertEqual(sys_stats.parse_loadavg("0.51 0.70 0.76 3/316 19218\n"), [0.51, 0.70, 0.76])
        self.assertEqual(sys_stats.parse_loadavg("nope"), [])


class MeminfoTests(unittest.TestCase):
    def test_used_is_total_minus_available(self):
        m = sys_stats.parse_meminfo(MEMINFO)
        self.assertEqual(m["total"], 16370064 * 1024)
        self.assertEqual(m["available"], 15527212 * 1024)
        self.assertEqual(m["used"], (16370064 - 15527212) * 1024)
        self.assertAlmostEqual(m["percent"], 5.1, places=1)

    def test_missing_memavailable_falls_back_to_memfree(self):
        m = sys_stats.parse_meminfo("MemTotal: 1000 kB\nMemFree: 250 kB\n")
        self.assertEqual(m["used"], 750 * 1024)
        self.assertEqual(m["percent"], 75.0)

    def test_empty(self):
        self.assertEqual(sys_stats.parse_meminfo("")["percent"], 0.0)


class MountSelectionTests(unittest.TestCase):
    def test_root_and_external_media_only(self):
        # / (overlay) and the USB stick; not /overlay (duplicate of /), /rom
        # (always full), /boot, tmpfs, or pseudo filesystems. The root entry
        # borrows /overlay's device so it can be classified (eMMC vs SD boot).
        self.assertEqual(sys_stats.select_disk_mounts(MOUNTS),
                         [("/", "overlay", "/dev/loop0"), ("/mnt/sda1", "vfat", "/dev/sda1")])

    def test_per_core_jiffies(self):
        text = PROC_STAT + "cpu1 10 0 10 80 0 0 0 0 0 0\n"
        cores = sys_stats.parse_proc_stat_cores(text)
        self.assertEqual(len(cores), 2)
        self.assertEqual(cores[1], (20, 100))
        self.assertEqual(cores[0][1], 361982 + 286718 + 9035746 + 1318 + 83548 + 137304)

    def test_disk_kind_classification(self):
        sysfs = {
            "/sys/block/loop0/loop/backing_file": "/mmcblk0p2\n",
            "/sys/block/mmcblk0/device/type": "MMC\n",
            "/sys/block/mmcblk1/device/type": "SD\n",
            "/sys/block/sda/device/path": "",
            "/sys/block/sda/removable": "1\n",
            "/sys/block/sdb/device/path": "pci-0000:00:1f.2-ata-1",
            "/sys/block/sdb/removable": "0\n",
        }
        read = lambda path: sysfs.get(path, "")
        # the photonicat2 root overlay: loop0 -> mmcblk0p2 -> eMMC
        self.assertEqual(sys_stats.disk_kind("/dev/loop0", read), "eMMC")
        self.assertEqual(sys_stats.disk_kind("/dev/mmcblk1p1", read), "SD")
        self.assertEqual(sys_stats.disk_kind("/dev/nvme0n1p1", read), "NVMe")
        self.assertEqual(sys_stats.disk_kind("/dev/sda1", read), "USB")
        self.assertEqual(sys_stats.disk_kind("/dev/sdb2", read), "SATA")
        self.assertEqual(sys_stats.disk_kind("overlayfs:/overlay", read), "Disk")

    def test_disk_usage_uses_df_semantics(self):
        class St:
            f_blocks, f_frsize, f_bfree, f_bavail = 1000, 4096, 400, 300
        with patch.object(sys_stats.os, "statvfs", return_value=St()):
            d = sys_stats.disk_usage("/")
        self.assertEqual(d["total"], 1000 * 4096)
        self.assertEqual(d["used"], 600 * 4096)
        self.assertAlmostEqual(d["percent"], 600 * 100.0 / 900, places=1)

    def test_disk_usage_failure_is_none(self):
        with patch.object(sys_stats.os, "statvfs", side_effect=OSError("gone")):
            self.assertIsNone(sys_stats.disk_usage("/mnt/gone"))


class ConntrackTests(unittest.TestCase):
    def test_counts_tcp_entries_only(self):
        self.assertEqual(sys_stats.count_tcp_flows(CONNTRACK), 2)
        self.assertEqual(sys_stats.count_tcp_flows(""), 0)


if __name__ == "__main__":
    unittest.main()
