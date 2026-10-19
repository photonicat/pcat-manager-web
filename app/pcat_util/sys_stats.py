"""System stat badges for /stats: CPU, memory, disks, TCP connections.

Pure parsers over /proc text (unit-testable); App.get_sys_stats() does the
reading. CPU is returned as cumulative jiffies so the page can take the
busy/total delta between its own polls -- the server keeps no per-client
state.
"""
import os

# Filesystems that live on a real block device (internal flash or external
# media). tmpfs/devtmpfs etc. are RAM, and the pseudo filesystems carry no
# usage at all.
_DISK_FSTYPES = {"overlay", "ext2", "ext3", "ext4", "f2fs", "btrfs", "xfs", "vfat",
                 "exfat", "ntfs", "ntfs3", "fuseblk", "jffs2", "ubifs"}
# Mounts that duplicate or hide the interesting number: /overlay is the upper
# layer already reported by /, /rom is the read-only image (always 100%),
# /boot is the tiny boot partition.
_SKIP_MOUNTS = {"/overlay", "/rom", "/boot"}


def parse_proc_stat(text):
  """First 'cpu' line of /proc/stat -> (busy, total) jiffies, or (0, 0)."""
  for line in text.splitlines():
    parts = line.split()
    if len(parts) >= 5 and parts[0] == "cpu":
      try:
        vals = [int(v) for v in parts[1:]]
      except ValueError:
        return (0, 0)
      # user nice system idle iowait irq softirq steal [guest guest_nice]
      idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
      total = sum(vals[:8])
      return (total - idle, total)
  return (0, 0)


def parse_proc_stat_cores(text):
  """'cpuN' lines of /proc/stat -> [(busy, total)] in core order."""
  cores = []
  for line in text.splitlines():
    parts = line.split()
    if len(parts) >= 5 and parts[0].startswith("cpu") and parts[0][3:].isdigit():
      try:
        vals = [int(v) for v in parts[1:]]
      except ValueError:
        continue
      idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
      total = sum(vals[:8])
      cores.append((int(parts[0][3:]), total - idle, total))
  cores.sort()
  return [(busy, total) for _, busy, total in cores]


def parse_loadavg(text):
  """/proc/loadavg -> [1, 5, 15 minute load] as floats, [] if unreadable."""
  parts = text.split()
  try:
    return [float(parts[0]), float(parts[1]), float(parts[2])]
  except (IndexError, ValueError):
    return []


def parse_meminfo(text):
  """/proc/meminfo -> {'total','available','used','percent'} in bytes.

  'used' follows free(1): total minus MemAvailable, so page cache that the
  kernel would drop on demand is not counted as used."""
  fields = {}
  for line in text.splitlines():
    if ":" not in line:
      continue
    key, rest = line.split(":", 1)
    try:
      fields[key.strip()] = int(rest.split()[0]) * 1024
    except (IndexError, ValueError):
      continue
  total = fields.get("MemTotal", 0)
  available = fields.get("MemAvailable", fields.get("MemFree", 0))
  used = max(0, total - available)
  percent = round(used * 100.0 / total, 1) if total else 0.0
  return {"total": total, "available": available, "used": used, "percent": percent}


def select_disk_mounts(mounts_text):
  """/proc/mounts -> [(mountpoint, fstype, device)] worth a badge: the root
  overlay plus any mounted media (SD, NVMe, USB), skipping the duplicates and
  pseudo filesystems. The root overlay's own "device" is a pseudo name, so it
  borrows the device of the /overlay upper layer (the loop/partition it sits
  on) for classification."""
  seen = set()
  out = []
  overlay_dev = None
  for line in mounts_text.splitlines():
    parts = line.split()
    if len(parts) < 3:
      continue
    dev, mount, fstype = parts[0], parts[1], parts[2]
    if mount == "/overlay":
      overlay_dev = dev
    if mount in seen or mount in _SKIP_MOUNTS:
      continue
    if mount != "/" and fstype not in _DISK_FSTYPES:
      continue
    seen.add(mount)
    out.append([mount, fstype, dev])
  for entry in out:
    if entry[0] == "/" and not entry[2].startswith("/dev/") and overlay_dev:
      entry[2] = overlay_dev
  return [tuple(e) for e in out]


def _base_disk(name):
  """Partition -> whole-disk name: mmcblk1p2 -> mmcblk1, nvme0n1p1 -> nvme0n1,
  sda1 -> sda."""
  if name.startswith(("mmcblk", "nvme")):
    head, sep, tail = name.rpartition("p")
    return head if sep and tail.isdigit() and head else name
  if name.startswith("sd"):
    return name.rstrip("0123456789")
  return name


def disk_kind(dev, read, depth=0):
  """Storage type behind a /dev node: 'eMMC', 'SD', 'NVMe', 'USB', 'SATA' or
  'Disk'. `read(path)` returns the text of a sysfs file or '' -- injected so
  the logic is testable off-device. A loop device is followed to its
  backing file (OpenWrt's f2fs overlay lives on one)."""
  name = dev.rsplit("/", 1)[-1]
  if name.startswith("loop") and depth < 3:
    backing = read("/sys/block/%s/loop/backing_file" % name).strip()
    if backing:
      return disk_kind(backing, read, depth + 1)
    return "Disk"
  base = _base_disk(name)
  if base.startswith("mmcblk"):
    mmc_type = read("/sys/block/%s/device/type" % base).strip().upper()
    if mmc_type == "SD":
      return "SD"
    if mmc_type == "MMC":
      return "eMMC"
    return "MMC"
  if base.startswith("nvme"):
    return "NVMe"
  if base.startswith("sd"):
    devpath = read("/sys/block/%s/device/path" % base)
    removable = read("/sys/block/%s/removable" % base).strip()
    if "usb" in devpath or removable == "1":
      return "USB"
    return "SATA"
  return "Disk"


def disk_usage(mount):
  """df-style numbers for a mountpoint, or None when statvfs fails."""
  try:
    st = os.statvfs(mount)
  except OSError:
    return None
  total = st.f_blocks * st.f_frsize
  free = st.f_bfree * st.f_frsize
  avail = st.f_bavail * st.f_frsize
  used = total - free
  denom = used + avail
  percent = round(used * 100.0 / denom, 1) if denom else 0.0
  return {"total": total, "used": used, "percent": percent}


def count_tcp_flows(conntrack_text):
  """Number of TCP entries in /proc/net/nf_conntrack text."""
  return sum(1 for line in conntrack_text.splitlines() if " tcp " in line)
