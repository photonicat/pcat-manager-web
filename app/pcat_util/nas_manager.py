"""
NAS subsystem manager: disk inventory, mount lifecycle, Samba shares.

The storage-side peer of wan_manager, in the same idiom: module state behind
an RLock, a cheap fast pass + a slower keeper called from the App tick loop,
and a status()/apply-style surface consumed by the /api/v1/nas/* routes.

Disk model. Enumeration is `lsblk -J -b` (in-image) with a /sys/block +
blkid fallback; classification (emmc/sd/nvme/usb) resolves the sysfs device
path rather than trusting name prefixes. The one rule everything else hangs
off: the disk(s) backing /, /rom, /overlay or /boot are PROTECTED, derived
from /proc/mounts at every guard check (never cached for a guard, never
hardcoded — recovery images can shuffle device names), with a belt-and-braces
second rule that a non-removable eMMC is protected even if the derivation
comes up empty. Destructive operations check protection here, inside the
manager, so a confused route handler cannot format the boot disk.

Mounts persist as UCI /etc/config/fstab sections (block-mount's native
config, keyed by filesystem UUID so device renumbering is harmless) named
with our "pcat_" prefix; immediate effect comes from `block mount` / direct
umount. Only our sections are ever written or removed.

Hotplug is a POLLED diff of /sys/block. The photonicat2 SD slot's card-detect
line is dead (DTS `broken-cd`), so removal raises no event anywhere — the
kernel notices within ~1 s by polling, and we notice by diffing every 5 s
tick. On removal: dependent Samba shares are disabled and the mount lazily
detached; in-flight EIO from a surprise pull is expected noise.

Samba is UCI /etc/config/samba4 (the samba4/luci-app-samba4 schema). We only
touch `sambashare` sections named with the "pcat_" prefix — LuCI- or
hand-written shares are invisible to us and never modified. Share users are
real (nologin) system users in a reserved uid range plus smbpasswd entries;
only users inside that range are ever deleted. An extra per-share option
`pcat_http_public` (ignored by samba) marks shares exposed through the
read-only no-login HTTP browse routes.
"""

import json
import os
import re
import stat
import subprocess
import threading
import time

from pcat_util.openwrt import (
    uci_get,
    uci_set,
    uci_commit,
    uci_delete,
    uci_show_dict,
)
from pcat_util import pcat_logger

_lock = threading.RLock()

MOUNT_BASE = "/mnt"
UCI_MOUNT_PREFIX = "pcat_"
SHARE_PREFIX = "pcat_"
PASSWD_PATH = "/etc/passwd"
SMB_UID_MIN = 21000
SMB_UID_MAX = 21999

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHARENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")

_DISK_CACHE_TTL = 5.0
_SMART_CACHE_TTL = 60.0

_disk_cache = {"time": 0.0, "data": None}
_smart_cache = {}          # devpath -> {"time": ts, "data": {...}}
_hotplug_snapshot = None   # set of /sys/block names, seeded by startup()
_emitter = None            # callable(event:str, payload:dict)|None


def set_emitter(fn):
    """Event-bus push hook (nas_disks_changed etc.); None in unit tests."""
    global _emitter
    _emitter = fn


def _emit(event, payload):
    if _emitter is None:
        return
    try:
        _emitter(event, payload)
    except Exception as e:
        pcat_logger.logger.warning("nas_manager: emit failed: {}".format(e))


def _run(argv, timeout=10, stdin_data=None):
    """Subprocess helper in the openwrt.py idiom: never raises, returns
    (returncode, stdout) with rc=-1 on infrastructure failure."""
    try:
        proc = subprocess.run(
            argv, input=stdin_data, capture_output=True, text=True,
            timeout=timeout)
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        pcat_logger.logger.warning(
            "nas_manager: {} failed: {}".format(argv[0], e))
        return -1, ""


def _which(binary):
    for prefix in ("/usr/sbin", "/usr/bin", "/sbin", "/bin"):
        if os.path.exists(os.path.join(prefix, binary)):
            return True
    return False


# Filesystems the format UI may offer. Presence of the matching mkfs binary
# is reported via tools_available() so the UI can disable missing ones.
FORMAT_FILESYSTEMS = ("ext4", "exfat", "vfat", "btrfs", "f2fs")


def tools_available():
    """Which optional binaries exist — the UI greys out what's missing and
    offers the opkg install flow."""
    return {
        "smart": _which("smartctl"),
        "ntfs": _which("ntfs-3g") or _which("mount.ntfs3"),
        "exfat": _which("mkfs.exfat"),
        "btrfs": _which("mkfs.btrfs"),
        "f2fs": _which("mkfs.f2fs"),
        # ZFS is not shipped on stock OpenWrt photonicat images; report so the
        # UI can show it disabled rather than offering a dead option.
        "zfs": bool(_which("zpool") and _which("zfs")),
        "samba": _which("smbd"),
        "rsync": _which("rsync"),
        # Always-on core formatters (busybox/e2fsprogs).
        "ext4": _which("mkfs.ext4"),
        "vfat": _which("mkfs.vfat") or _which("mkfs.fat"),
    }


# ── sysfs / mounts readers (injectable for tests) ────────────────────────────

def _read_proc_mounts():
    try:
        with open("/proc/mounts") as f:
            return f.read()
    except OSError:
        return ""


def _sysfs_realpath(block_name):
    try:
        return os.path.realpath("/sys/block/{}".format(block_name))
    except OSError:
        return ""


def _sysfs_read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


# ── disk classification & protection ─────────────────────────────────────────

def classify_bus(disk_name, sysfs_realpath=_sysfs_realpath,
                 sysfs_read=_sysfs_read):
    """emmc/sd/nvme/usb/unknown from the resolved sysfs path, not the name."""
    if disk_name.startswith("nvme"):
        return "nvme"
    real = sysfs_realpath(disk_name)
    if "/usb" in real:
        return "usb"
    if disk_name.startswith("mmcblk"):
        # The removable flag is 0 for SD cards on this platform (mmc block
        # devices report the drive, not the media) — the card's device/type
        # attribute ("SD" vs "MMC") is the reliable discriminator. Fall back
        # to removable only when type is absent (e.g. mmcblkXboot0 nodes,
        # which have no card of their own and are eMMC by definition).
        ctype = sysfs_read("/sys/block/{}/device/type".format(disk_name))
        if ctype == "SD":
            return "sd"
        if ctype:
            return "emmc"
        removable = sysfs_read(
            "/sys/block/{}/removable".format(disk_name))
        return "sd" if removable == "1" else "emmc"
    if disk_name.startswith("sd"):
        # SATA/SCSI without a usb hop (rare here) — treat as usb-class
        # removable storage for UI purposes.
        return "usb"
    return "unknown"


def _has_partition_attr(name):
    """The kernel marks partition nodes (and only them) with a 'partition'
    attribute — the reliable is-this-a-partition test."""
    return os.path.exists("/sys/class/block/{}/partition".format(name))


def parent_disk(part_name, sysfs_realpath=_sysfs_realpath,
                is_partition=None):
    """'nvme0n1p2' -> 'nvme0n1'; a whole disk maps to itself. A partition's
    sysfs node nests inside its disk's node, so the parent directory is the
    disk — but only for actual partitions (a whole NVMe namespace's parent is
    the controller, hence the partition-attribute gate)."""
    checker = is_partition or _has_partition_attr
    if not checker(part_name):
        # Name-based fallback when sysfs has no 'partition' attr (tests, or
        # a node that vanished mid-op): mmcblk0p3 -> mmcblk0, nvme0n1p1 ->
        # nvme0n1, sda2 -> sda.
        m = re.match(r"^(mmcblk\d+)p\d+$", part_name)
        if m:
            return m.group(1)
        m = re.match(r"^(nvme\d+n\d+)p\d+$", part_name)
        if m:
            return m.group(1)
        m = re.match(r"^(sd[a-z]+)\d+$", part_name)
        if m:
            return m.group(1)
        return part_name
    real = sysfs_realpath_class(part_name, sysfs_realpath)
    if real:
        parent = os.path.basename(os.path.dirname(real))
        if parent and parent != "block":
            return parent
    # Sysfs said it's a partition but parent walk failed — same name rules.
    m = re.match(r"^(mmcblk\d+)p\d+$", part_name)
    if m:
        return m.group(1)
    m = re.match(r"^(nvme\d+n\d+)p\d+$", part_name)
    if m:
        return m.group(1)
    m = re.match(r"^(sd[a-z]+)\d+$", part_name)
    if m:
        return m.group(1)
    return part_name


def sysfs_realpath_class(name, sysfs_realpath=_sysfs_realpath):
    """realpath of /sys/class/block/<name> (partition OR disk)."""
    try:
        return os.path.realpath("/sys/class/block/{}".format(name))
    except OSError:
        return ""


def belongs_to_disk(dev_name, disk_name):
    """Name-only test that `dev_name` is `disk_name` or one of its
    partitions ('sda1' of 'sda', 'mmcblk1p2' of 'mmcblk1'). Used when the
    device has already VANISHED, so sysfs can no longer answer. Kernel
    naming rule: a disk whose name ends in a digit separates its partition
    number with 'p' (mmcblk1p1, nvme0n1p1) — without that rule 'nvme0n11'
    (another namespace) would read as a partition of 'nvme0n1'."""
    if dev_name == disk_name:
        return True
    if not dev_name.startswith(disk_name):
        return False
    suffix = dev_name[len(disk_name):]
    if disk_name[-1].isdigit():
        return bool(re.match(r"^p\d+$", suffix))
    return suffix.isdigit()


# Mount points that define the running OS. Partitions mounted here are always
# protected; other partitions on the same disk (e.g. eMMC free-space p3) may
# be formatted/mounted as NAS data.
_SYSTEM_MOUNT_TARGETS = ("/", "/rom", "/overlay", "/boot")


def _loop_backing_file(loop_name):
    return _sysfs_read("/sys/block/{}/loop/backing_file".format(loop_name))


def system_partition_names(read_mounts=_read_proc_mounts,
                           sysfs_realpath=_sysfs_realpath,
                           loop_backing=None):
    """Basenames of partitions that back the running OS (/ /rom /overlay /boot).

    Used for fine-grained protection: the eMMC boot + squashfs root stay
    locked, while a later data partition on the same disk can be used."""
    names = set()
    loop_backing = loop_backing or _loop_backing_file
    for line in read_mounts().splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/"):
            continue
        source, target = parts[0], parts[1]
        if target not in _SYSTEM_MOUNT_TARGETS:
            continue
        name = os.path.basename(source)
        if name.startswith("loop"):
            backing = loop_backing(name)
            if backing:
                # The loop file lives on some filesystem; find that mount's
                # device by longest-prefix match against /proc/mounts.
                best = ""
                best_src = ""
                for l2 in read_mounts().splitlines():
                    p2 = l2.split()
                    if len(p2) < 2 or not p2[0].startswith("/dev/"):
                        continue
                    if backing.startswith(p2[1].rstrip("/") + "/") and \
                            len(p2[1]) > len(best):
                        best, best_src = p2[1], p2[0]
                if best_src:
                    names.add(os.path.basename(best_src))
            continue
        if name in ("root",):
            # /dev/root is a kernel alias, not a real partition node.
            continue
        names.add(name)
    return names


def rootfs_disks(read_mounts=_read_proc_mounts,
                 sysfs_realpath=_sysfs_realpath,
                 loop_backing=None):
    """Disks that host at least one system partition. Never trust a cache for
    a guard — this is cheap and re-derived every time it matters."""
    disks = set()
    for part in system_partition_names(read_mounts, sysfs_realpath,
                                       loop_backing):
        disks.add(parent_disk(part, sysfs_realpath))
    return disks


def is_protected(dev_name, read_mounts=_read_proc_mounts,
                 sysfs_realpath=_sysfs_realpath, sysfs_read=_sysfs_read):
    """True when a device must never see a destructive NAS operation.

    Protection is *partition-aware* on the system disk:
      - whole eMMC (and any disk that hosts / /rom /overlay /boot) stays
        protected for whole-disk ops (mklabel, wipe) and create_free is the
        only carve-out on that whole-disk node;
      - boot/root/overlay partitions themselves stay protected;
      - a data partition on the same eMMC (free-space p3, pcat-data, etc.)
        is NOT protected — it may be formatted, mounted and re-formatted.
    """
    name = os.path.basename(dev_name)
    disk = parent_disk(name, sysfs_realpath)
    # Loop devices are never NAS-managed storage, and the interesting ones
    # (the /overlay f2fs image on this firmware) back the running system —
    # their backing disk wouldn't resolve through parent_disk, so refuse the
    # whole class outright rather than resolve the chain.
    if name.startswith(("loop", "ram", "zram")) or name == "root" or \
            disk.startswith(("loop", "ram", "zram")) or disk == "root":
        return True
    sys_parts = system_partition_names(read_mounts, sysfs_realpath)
    if name in sys_parts:
        return True
    # Whole-disk node (not a partition): protect system disks and all eMMC
    # so mklabel/create-on-whole-disk cannot erase the OS. create_free is
    # still allowed via partition_guard's carve-out.
    is_part = (name != disk)
    if not is_part:
        if disk in rootfs_disks(read_mounts, sysfs_realpath):
            return True
        # Belt and braces: internal eMMC whole-disk always protected even if
        # /proc/mounts is empty (early boot / degraded derivation).
        if classify_bus(disk, sysfs_realpath, sysfs_read) == "emmc":
            return True
        return False
    # Partition that is not a system mount: usable (including eMMC data).
    # Extra safety: never treat squashfs as data even if the mount map is
    # incomplete (parted reports empty fs for it; blkid knows). Only check
    # on eMMC/nvme system-class disks to avoid extra blkid on every USB poll.
    if classify_bus(disk, sysfs_realpath, sysfs_read) in ("emmc", "nvme"):
        try:
            if blkid_type("/dev/" + name) == "squashfs":
                return True
        except Exception:
            pass
    return False


# ── disk inventory ───────────────────────────────────────────────────────────

def _lsblk_json():
    rc, out = _run(["lsblk", "-J", "-b", "-o",
                    "NAME,TYPE,SIZE,RM,MODEL,SERIAL,FSTYPE,UUID,LABEL,"
                    "MOUNTPOINT"], timeout=10)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _statvfs_usage(mountpoint):
    try:
        vfs = os.statvfs(mountpoint)
        total = vfs.f_blocks * vfs.f_frsize
        return total - vfs.f_bfree * vfs.f_frsize, vfs.f_bavail * vfs.f_frsize
    except OSError:
        return None, None


def build_disk_model(lsblk_data, automount_uuids, read_mounts=_read_proc_mounts,
                     sysfs_realpath=_sysfs_realpath, sysfs_read=_sysfs_read):
    """Pure transform of lsblk JSON -> the API disk list. Split out so tests
    can feed canned lsblk output."""
    protected = rootfs_disks(read_mounts, sysfs_realpath)
    disks = []
    for node in (lsblk_data or {}).get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        name = node.get("name", "")
        if name.startswith(("loop", "ram", "zram", "mtd", "ubi")):
            continue
        # The eMMC hardware boot partitions (mmcblk0boot0/1, ~4 MiB each,
        # bootloader storage) show up as separate disks; pure noise in a
        # storage UI where nothing may ever touch them anyway.
        if re.match(r"^mmcblk\d+boot\d+$", name):
            continue
        bus = classify_bus(name, sysfs_realpath, sysfs_read)
        # Whole-disk flag: eMMC / rootfs hosts stay "system disk" in the UI
        # (no mklabel), but individual data partitions can still be used.
        disk_protected = (name in protected or bus == "emmc"
                          or is_protected(name, read_mounts, sysfs_realpath,
                                          sysfs_read))
        parts = []
        for child in node.get("children") or []:
            if child.get("type") != "part":
                continue
            mp = child.get("mountpoint")
            used = avail = None
            if mp:
                used, avail = _statvfs_usage(mp)
            child_name = child.get("name", "")
            # Per-partition protection: system mounts / squashfs locked;
            # free-space data partitions on the same eMMC are usable.
            part_protected = is_protected(
                child_name, read_mounts, sysfs_realpath, sysfs_read)
            # Also lock by fstype when lsblk already knows (no extra blkid).
            if (child.get("fstype") or "").lower() == "squashfs":
                part_protected = True
            parts.append({
                "dev": "/dev/" + child_name,
                "fs": child.get("fstype"),
                "label": child.get("label"),
                "uuid": child.get("uuid"),
                "size_bytes": int(child.get("size") or 0),
                "mounted": bool(mp),
                "mountpoint": mp,
                "used_bytes": used,
                "avail_bytes": avail,
                "automount": (child.get("uuid") or "") in automount_uuids,
                "protected": part_protected,
            })
        # Unpartitioned tail: what a one-click "use free space" could claim.
        # Cheap arithmetic (disk size minus the partitions we can see) rather
        # than a parted call per disk on a 10 s UI poll.
        claimed = sum(int(c.get("size") or 0)
                      for c in (node.get("children") or [])
                      if c.get("type") == "part")
        unallocated = max(0, int(node.get("size") or 0) - claimed)
        disks.append({
            "dev": "/dev/" + name,
            "name": name,
            "bus": bus,
            "unallocated_bytes": unallocated,
            "model": (node.get("model") or "").strip() or None,
            "serial": (node.get("serial") or "").strip() or None,
            "size_bytes": int(node.get("size") or 0),
            "removable": node.get("rm") in (True, "1", 1),
            "protected": disk_protected,
            "partitions": parts,
        })
    order = {"nvme": 0, "usb": 1, "sd": 2, "emmc": 3}
    disks.sort(key=lambda d: (order.get(d["bus"], 9), d["name"]))
    return disks


def list_disks(refresh=False):
    with _lock:
        now = time.time()
        if (not refresh and _disk_cache["data"] is not None
                and now - _disk_cache["time"] < _DISK_CACHE_TTL):
            return _disk_cache["data"]
    data = _lsblk_json()
    disks = build_disk_model(data, automount_uuids()) if data else []
    with _lock:
        _disk_cache.update(time=time.time(), data=disks)
    return disks


def invalidate_disk_cache():
    with _lock:
        _disk_cache.update(time=0.0, data=None)


def user_data_partitions(read_mounts=_read_proc_mounts,
                         sysfs_realpath=_sysfs_realpath):
    """Partitions on a system disk that the user carved out of free space.

    These are the ones a sysupgrade without -p destroys: everything the stock
    image lays down is restored from the image's own table, but a partition
    created after the fact (the 'use free space' flow, which labels its
    volume pcat-data) only exists in the live table. Returns a list of
    {name, dev, label, size} -- empty on a device still running the stock
    layout, which is the common case.

    Only the system disk is considered. A USB stick or NVMe is not touched by
    sysupgrade at all, so warning about it would be noise.
    """
    found = []
    system_names = system_partition_names(read_mounts, sysfs_realpath)
    for disk in list_disks():
        if not disk.get("protected"):
            # Not a system disk -- sysupgrade never writes here.
            continue
        for part in disk.get("partitions") or []:
            dev = part.get("dev") or ""
            name = os.path.basename(dev)
            if not name or name in system_names:
                continue
            if part.get("protected"):
                # boot/root/overlay: restored by the image, not user data.
                continue
            found.append({
                "name": name,
                "dev": dev,
                "label": part.get("label") or "",
                "size_bytes": part.get("size_bytes") or 0,
            })
    return found


def smart_summary(devpath):
    """Lazy, cached SMART health. None when smartctl is absent/unsupported
    (mmc has no SMART)."""
    with _lock:
        entry = _smart_cache.get(devpath)
        if entry and time.time() - entry["time"] < _SMART_CACHE_TTL:
            return entry["data"]
    data = None
    if _which("smartctl") and not os.path.basename(devpath).startswith("mmcblk"):
        rc, out = _run(["smartctl", "-j", "-H", "-A", devpath], timeout=15)
        try:
            j = json.loads(out) if out.strip().startswith("{") else {}
        except ValueError:
            j = {}
        if j:
            passed = (j.get("smart_status") or {}).get("passed")
            temp = (j.get("temperature") or {}).get("current")
            hours = (j.get("power_on_time") or {}).get("hours")
            data = {"health": ("ok" if passed else
                               "fail" if passed is not None else "na"),
                    "temp_c": temp, "power_on_hours": hours,
                    "attrs": [
                        {"id": a.get("id"), "name": a.get("name"),
                         "value": a.get("value"),
                         "raw": (a.get("raw") or {}).get("string")}
                        for a in ((j.get("ata_smart_attributes") or {})
                                  .get("table") or [])[:24]
                    ]}
    with _lock:
        _smart_cache[devpath] = {"time": time.time(), "data": data}
    return data


# ── mounts ───────────────────────────────────────────────────────────────────

def _slug(text):
    s = _SLUG_RE.sub("-", (text or "").strip()).strip("-").lower()
    return s[:32] or None


def mounted_map(read_mounts=_read_proc_mounts):
    """{'/dev/sda1': '/mnt/foo', ...} for real block sources."""
    out = {}
    for line in read_mounts().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("/dev/"):
            out[parts[0]] = parts[1]
    return out


# Mount targets that are the running system, never NAS storage — excluded
# from the jail regardless of what device serves them (the /overlay loop
# mount taught us not to trust source-device classification alone here).
_SYSTEM_TARGETS = ("/", "/rom", "/overlay", "/boot", "/tmp")


def managed_mount_roots():
    """The file-manager jail roots: every live mountpoint whose backing disk
    is NOT protected. Covers our UCI mounts, blockd automounts and hand
    mounts alike — if it's external storage and mounted, it's browsable."""
    roots = []
    for source, target in mounted_map().items():
        if target in _SYSTEM_TARGETS or target.startswith("/overlay/"):
            continue
        name = os.path.basename(source)
        if is_protected(name):
            continue
        roots.append(os.path.realpath(target))
    # Live remote mounts (sshfs/rclone; fuse sources aren't /dev/*) join the
    # jail so the file manager and shares work on them too.
    if list_remotes():
        roots.extend(remote_mount_roots())
    return sorted(set(roots))


def automount_uuids():
    """UUIDs of our UCI fstab sections (enabled or not)."""
    cfg = uci_show_dict("fstab")
    uuids = set()
    for key, value in cfg.items():
        if ".uuid" in key and ".@" not in key and \
                key.split(".")[1].startswith(UCI_MOUNT_PREFIX):
            uuids.add(value)
    return uuids


def _find_mount_section(uuid):
    cfg = uci_show_dict("fstab")
    for key, value in cfg.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[2] == "uuid" and value == uuid and \
                parts[1].startswith(UCI_MOUNT_PREFIX):
            return parts[1]
    return None


def _part_info(devpath):
    for disk in list_disks(refresh=True):
        for part in disk["partitions"]:
            if part["dev"] == devpath:
                return disk, part
    return None, None


def enable_automount(devpath):
    """Persist a mount for the partition (by UUID) and mount it now."""
    disk, part = _part_info(devpath)
    if part is None:
        return {"ok": False, "code": "bad_dev", "message": "unknown device"}
    # Partition-level protection (boot/root), not whole-disk eMMC flag —
    # otherwise create_free on eMMC could never automount the new volume.
    if part.get("protected") or is_protected(os.path.basename(devpath)):
        return {"ok": False, "code": "protected",
                "message": "system partition is read-only in the NAS UI"}
    if not part["uuid"]:
        return {"ok": False, "code": "no_uuid",
                "message": "partition has no filesystem UUID (format first)"}
    with _lock:
        section = _find_mount_section(part["uuid"])
        if section is None:
            section = UCI_MOUNT_PREFIX + part["uuid"].replace("-", "")[:8]
            target = os.path.join(
                MOUNT_BASE,
                _slug(part["label"]) or os.path.basename(devpath))
            uci_set("fstab.{}=mount".format(section))
            uci_set("fstab.{}.uuid={}".format(section, part["uuid"]))
            uci_set("fstab.{}.target={}".format(section, target))
            uci_set("fstab.{}.options=rw,noatime".format(section))
        uci_set("fstab.{}.enabled=1".format(section))
        uci_commit("fstab")
        target = uci_get("fstab.{}.target".format(section))
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            pass
    _run(["block", "mount"], timeout=30)
    invalidate_disk_cache()
    return {"ok": True, "mountpoint": target}


def disable_automount(devpath, unmount_now=True):
    disk, part = _part_info(devpath)
    if part is None:
        return {"ok": False, "code": "bad_dev", "message": "unknown device"}
    with _lock:
        section = _find_mount_section(part["uuid"]) if part["uuid"] else None
        if section:
            uci_delete("fstab.{}".format(section))
            uci_commit("fstab")
    result = {"ok": True}
    if unmount_now and part["mounted"]:
        result = unmount(part["mountpoint"])
    invalidate_disk_cache()
    return result


def mount_now(devpath):
    """One-off mount without persistence (no UCI section)."""
    disk, part = _part_info(devpath)
    if part is None:
        return {"ok": False, "code": "bad_dev", "message": "unknown device"}
    if part.get("protected") or is_protected(os.path.basename(devpath)):
        return {"ok": False, "code": "protected",
                "message": "system partition is read-only in the NAS UI"}
    if part["mounted"]:
        return {"ok": True, "mountpoint": part["mountpoint"]}
    target = os.path.join(
        MOUNT_BASE, _slug(part["label"]) or os.path.basename(devpath))
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        return {"ok": False, "code": "io_error", "message": str(e)}
    rc, out = _run(["mount", devpath, target], timeout=30)
    if rc != 0:
        return {"ok": False, "code": "mount_failed",
                "message": out.strip() or "mount failed"}
    invalidate_disk_cache()
    return {"ok": True, "mountpoint": target}


def _procs_using(mountpoint):
    """Fallback busy-scan when fuser is unavailable: pids whose cwd/root or
    open fds live under the mountpoint."""
    pids = []
    prefix = mountpoint.rstrip("/") + "/"
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        base = "/proc/" + pid
        try:
            for link in ("cwd", "root"):
                t = os.readlink(os.path.join(base, link))
                if t == mountpoint or t.startswith(prefix):
                    pids.append(int(pid))
                    raise StopIteration
            for fd in os.listdir(os.path.join(base, "fd")):
                t = os.readlink(os.path.join(base, "fd", fd))
                if t == mountpoint or t.startswith(prefix):
                    pids.append(int(pid))
                    break
        except StopIteration:
            continue
        except OSError:
            continue
    return pids


def unmount(mountpoint, force=False):
    mountpoint = os.path.realpath(mountpoint)
    if mountpoint not in managed_mount_roots():
        return {"ok": False, "code": "bad_path",
                "message": "not a managed mountpoint"}
    if not force:
        if _which("fuser"):
            rc, _out = _run(["fuser", "-m", mountpoint], timeout=10)
            busy = rc == 0
            pids = []
        else:
            pids = _procs_using(mountpoint)
            busy = bool(pids)
        if busy:
            return {"ok": False, "code": "busy", "pids": pids[:10],
                    "message": "volume is in use"}
    else:
        _disable_shares_under(mountpoint)
    rc, out = _run(["umount", mountpoint], timeout=30)
    if rc != 0 and force:
        rc, out = _run(["umount", "-l", mountpoint], timeout=10)
        if rc == 0:
            invalidate_disk_cache()
            return {"ok": True, "detached": True}
    if rc != 0:
        return {"ok": False, "code": "umount_failed",
                "message": out.strip() or "umount failed"}
    invalidate_disk_cache()
    return {"ok": True}


# ── hotplug (polled — the SD slot has no working card-detect) ────────────────

_hotplug_worker_active = threading.Lock()


def reconcile_hotplug():
    """5 s tick pass, called INLINE from the App tick: diff /sys/block —
    one listdir when nothing changed. On a change the mount/unmount work
    (subprocesses, up to ~30 s) runs on a worker thread so the tick loop is
    never stalled behind a slow SD card."""
    global _hotplug_snapshot
    try:
        current = set(os.listdir("/sys/block"))
    except OSError:
        return "sysfs_unavailable"
    current = {n for n in current
               if not n.startswith(("loop", "ram", "zram", "mtd", "ubi"))}
    with _lock:
        previous = _hotplug_snapshot
        _hotplug_snapshot = current
    if previous is None or current == previous:
        return "idle"

    appeared = current - previous
    vanished = previous - current
    if not _hotplug_worker_active.acquire(blocking=False):
        # A previous change is still being handled; the snapshot is already
        # updated, and the next diff will reconcile any further drift.
        return "busy"

    def _worker():
        try:
            _handle_hotplug_change(appeared, vanished)
        except Exception as e:
            pcat_logger.logger.error(
                "nas_manager: hotplug worker failed: {}".format(e))
        finally:
            _hotplug_worker_active.release()

    threading.Thread(target=_worker, daemon=True,
                     name="nas-hotplug").start()
    return "changed appeared:{} vanished:{}".format(
        ",".join(sorted(appeared)) or "-", ",".join(sorted(vanished)) or "-")


def _handle_hotplug_change(appeared, vanished):
    if appeared:
        _run(["block", "mount"], timeout=30)
    if vanished:
        stale = [(src, tgt) for src, tgt in mounted_map().items()
                 if any(belongs_to_disk(os.path.basename(src), d)
                        for d in vanished)]
        for src, tgt in stale:
            _disable_shares_under(tgt)
            _run(["umount", "-l", tgt], timeout=10)
    invalidate_disk_cache()
    _emit("nas_disks_changed", {"appeared": sorted(appeared),
                                "vanished": sorted(vanished)})


# ── samba ────────────────────────────────────────────────────────────────────

def _samba_sections():
    """{section: {option: value}} for OUR sambashare sections only."""
    cfg = uci_show_dict("samba4")
    sections = {}
    for key, value in cfg.items():
        parts = key.split(".")
        if len(parts) == 2 and value == "sambashare" and \
                parts[1].startswith(SHARE_PREFIX):
            sections[parts[1]] = {}
    for key, value in cfg.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[1] in sections:
            sections[parts[1]][parts[2]] = value
    return sections


def list_shares():
    roots = managed_mount_roots()
    shares = []
    for section, opts in sorted(_samba_sections().items()):
        path = opts.get("path", "")
        real = os.path.realpath(path) if path else ""
        valid = any(real == r or real.startswith(r + "/") for r in roots)
        users = (opts.get("users") or "").split()
        guest = opts.get("guest_ok") == "yes"
        shares.append({
            "id": section,
            "name": opts.get("name", section),
            "path": path,
            "mode": "guest" if guest else "users",
            "read_only": opts.get("read_only", "no") == "yes",
            "users": users,
            "http_browse": opts.get("pcat_http_public") == "1",
            "valid": valid,
        })
    return shares


def share_uci_ops(share_id, name, path, mode, users, read_only, http_browse):
    """Pure builder: the exact uci_set key=value strings for one share.
    Split from upsert_share so tests can pin the schema."""
    ops = ["samba4.{}=sambashare".format(share_id),
           "samba4.{}.name={}".format(share_id, name),
           "samba4.{}.path={}".format(share_id, path),
           "samba4.{}.read_only={}".format(share_id,
                                           "yes" if read_only else "no"),
           "samba4.{}.guest_ok={}".format(share_id,
                                          "yes" if mode == "guest" else "no"),
           "samba4.{}.create_mask=0666".format(share_id),
           "samba4.{}.dir_mask=0777".format(share_id),
           "samba4.{}.force_root=1".format(share_id),
           "samba4.{}.pcat_http_public={}".format(
               share_id, "1" if http_browse else "0")]
    if mode == "users" and users:
        ops.append("samba4.{}.users={}".format(share_id, " ".join(users)))
    return ops


def upsert_share(share_id, name, path, mode="guest", users=(),
                 read_only=False, http_browse=False):
    if not _SHARENAME_RE.match(name or ""):
        return {"ok": False, "code": "bad_name",
                "message": "invalid share name"}
    if mode not in ("guest", "users"):
        return {"ok": False, "code": "bad_mode", "message": "invalid mode"}
    for user in users:
        if not _USERNAME_RE.match(user):
            return {"ok": False, "code": "bad_user",
                    "message": "invalid user name"}
    real = os.path.realpath(path or "")
    roots = managed_mount_roots()
    if not any(real == r or real.startswith(r + "/") for r in roots):
        return {"ok": False, "code": "bad_path",
                "message": "share path must be on mounted NAS storage"}
    if not os.path.isdir(real):
        return {"ok": False, "code": "bad_path",
                "message": "share path is not a directory"}
    with _lock:
        if share_id is None:
            share_id = SHARE_PREFIX + re.sub(
                r"[^a-z0-9]", "", name.lower())[:12]
            if not share_id[len(SHARE_PREFIX):]:
                share_id = SHARE_PREFIX + "share{}".format(
                    len(_samba_sections()) + 1)
            base, n = share_id, 1
            while share_id in _samba_sections():
                n += 1
                share_id = "{}{}".format(base, n)
        elif not share_id.startswith(SHARE_PREFIX) or \
                share_id not in _samba_sections():
            return {"ok": False, "code": "not_found",
                    "message": "unknown share"}
        else:
            uci_delete("samba4.{}.users".format(share_id))
        for op in share_uci_ops(share_id, name, real, mode, list(users),
                                read_only, http_browse):
            uci_set(op)
        uci_commit("samba4")
    _samba_reload()
    return {"ok": True, "id": share_id}


def delete_share(share_id):
    with _lock:
        if not share_id.startswith(SHARE_PREFIX) or \
                share_id not in _samba_sections():
            return {"ok": False, "code": "not_found",
                    "message": "unknown share"}
        uci_delete("samba4.{}".format(share_id))
        uci_commit("samba4")
    _samba_reload()
    return {"ok": True}


def _disable_shares_under(mountpoint):
    """Disable (delete) our shares whose path lives under a mountpoint that
    is going away; samba holding open handles would pin the mount."""
    prefix = mountpoint.rstrip("/") + "/"
    doomed = []
    with _lock:
        for section, opts in _samba_sections().items():
            p = opts.get("path", "")
            if p == mountpoint or p.startswith(prefix):
                doomed.append(section)
        for section in doomed:
            uci_delete("samba4.{}".format(section))
        if doomed:
            uci_commit("samba4")
    if doomed:
        pcat_logger.logger.info(
            "nas_manager: removed {} share(s) under vanishing mount {}".format(
                len(doomed), mountpoint))
        _samba_reload()
    return doomed


def public_http_roots():
    """{share_name: realpath} for shares opted into no-login HTTP browse."""
    out = {}
    for share in list_shares():
        if share["http_browse"] and share["valid"]:
            out[share["name"]] = os.path.realpath(share["path"])
    return out


# ── samba users ──────────────────────────────────────────────────────────────

def passwd_line(username, uid):
    return "{}:x:{}:65534::/var/empty:/bin/false".format(username, uid)


def _parse_passwd(text):
    users = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) >= 3:
            try:
                users[parts[0]] = int(parts[2])
            except ValueError:
                continue
    return users


def _ensure_system_user(username):
    with open(PASSWD_PATH) as f:
        text = f.read()
    users = _parse_passwd(text)
    if username in users:
        return True
    taken = set(users.values())
    uid = next((u for u in range(SMB_UID_MIN, SMB_UID_MAX + 1)
                if u not in taken), None)
    if uid is None:
        return False
    if not text.endswith("\n"):
        text += "\n"
    text += passwd_line(username, uid) + "\n"
    tmp = PASSWD_PATH + ".pcat_tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, PASSWD_PATH)
    return True


def list_smb_users():
    rc, out = _run(["pdbedit", "-L"], timeout=10)
    if rc != 0:
        return []
    users = []
    for line in out.splitlines():
        name = line.split(":")[0].strip()
        if name and _USERNAME_RE.match(name):
            users.append(name)
    return sorted(users)


def set_smb_user(username, password):
    """Create/update a samba credential; password only ever via stdin."""
    if not _USERNAME_RE.match(username or ""):
        return {"ok": False, "code": "bad_user",
                "message": "invalid user name"}
    if not password or len(password) < 4 or len(password) > 128:
        return {"ok": False, "code": "bad_password",
                "message": "password must be 4-128 characters"}
    with _lock:
        if not _ensure_system_user(username):
            return {"ok": False, "code": "io_error",
                    "message": "could not create system user"}
        rc, out = _run(["smbpasswd", "-s", "-a", username], timeout=15,
                       stdin_data=password + "\n" + password + "\n")
    if rc != 0:
        return {"ok": False, "code": "smbpasswd_failed",
                "message": out.strip() or "smbpasswd failed"}
    _samba_reload(restart=True)
    return {"ok": True}


def delete_smb_user(username):
    if not _USERNAME_RE.match(username or ""):
        return {"ok": False, "code": "bad_user",
                "message": "invalid user name"}
    with _lock:
        _run(["smbpasswd", "-x", username], timeout=15)
        # Remove the system user only if we created it (uid in our range),
        # and strip it from every share's user list.
        try:
            with open(PASSWD_PATH) as f:
                text = f.read()
            users = _parse_passwd(text)
            if SMB_UID_MIN <= users.get(username, -1) <= SMB_UID_MAX:
                kept = [l for l in text.splitlines()
                        if not l.startswith(username + ":")]
                tmp = PASSWD_PATH + ".pcat_tmp"
                with open(tmp, "w") as f:
                    f.write("\n".join(kept) + "\n")
                os.replace(tmp, PASSWD_PATH)
        except OSError as e:
            pcat_logger.logger.warning(
                "nas_manager: passwd cleanup failed: {}".format(e))
        changed = False
        for section, opts in _samba_sections().items():
            share_users = (opts.get("users") or "").split()
            if username in share_users:
                share_users.remove(username)
                if share_users:
                    uci_set("samba4.{}.users={}".format(
                        section, " ".join(share_users)))
                else:
                    uci_delete("samba4.{}.users".format(section))
                changed = True
        if changed:
            uci_commit("samba4")
    _samba_reload(restart=True)
    return {"ok": True}


# ── samba service ────────────────────────────────────────────────────────────

_samba_reload_lock = threading.Lock()
_samba_reload_pending = {"timer": None, "restart": False}
_SAMBA_RELOAD_DEBOUNCE = 2.0


def samba_state():
    installed = _which("smbd")
    running = False
    enabled = False
    if installed:
        rc, _out = _run(["pgrep", "smbd"], timeout=5)
        running = rc == 0
        enabled = os.path.exists("/etc/rc.d/S98samba4") or bool(
            [f for f in _safe_listdir("/etc/rc.d") if "samba4" in f])
    return {"installed": installed, "running": running, "enabled": enabled}


def _safe_listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _samba_reload(restart=False):
    """Debounced: bulk share edits collapse to one service kick. A restart
    request wins over reload (user database changes need it)."""
    def _fire():
        with _samba_reload_lock:
            restart_now = _samba_reload_pending["restart"]
            _samba_reload_pending.update(timer=None, restart=False)
        action = "restart" if restart_now else "reload"
        _run(["/etc/init.d/samba4", action], timeout=30)

    with _samba_reload_lock:
        _samba_reload_pending["restart"] |= restart
        if _samba_reload_pending["timer"] is None:
            t = threading.Timer(_SAMBA_RELOAD_DEBOUNCE, _fire)
            t.daemon = True
            _samba_reload_pending["timer"] = t
            t.start()


def samba_service(action):
    if action not in ("enable", "disable", "restart"):
        return {"ok": False, "code": "bad_action", "message": "bad action"}
    if action == "enable":
        _run(["/etc/init.d/samba4", "enable"], timeout=15)
        _run(["/etc/init.d/samba4", "start"], timeout=30)
    elif action == "disable":
        _run(["/etc/init.d/samba4", "stop"], timeout=30)
        _run(["/etc/init.d/samba4", "disable"], timeout=15)
    else:
        _run(["/etc/init.d/samba4", "restart"], timeout=30)
    return {"ok": True}


def _assert_samba_lan_bind():
    """The stock config binds samba to the lan interface; make sure ours
    does too so shares never face the WAN. Log-only if the config is absent
    (samba not installed)."""
    if uci_get("samba4.@samba[0]") != "samba":
        return
    if uci_get("samba4.@samba[0].interface") != "lan":
        uci_set("samba4.@samba[0].interface=lan")
        uci_commit("samba4")
        pcat_logger.logger.info(
            "nas_manager: pinned samba bind interface to lan")


# ── status / keepers / startup ───────────────────────────────────────────────

def status():
    from pcat_util import nas_jobs
    return {
        "tools": tools_available(),
        "disks": list_disks(),
        "samba": samba_state(),
        "shares": list_shares(),
        "smb_users": list_smb_users(),
        "jobs_active": nas_jobs.active_count(),
    }


def reconcile():
    """30 s keeper: re-assert the samba lan bind (drift guard), keep remote
    mounts alive (P3), and report a one-line summary for change-only
    logging."""
    _assert_samba_lan_bind()
    remotes = reconcile_remotes()
    disks = list_disks()
    mounted = sum(1 for d in disks for p in d["partitions"] if p["mounted"])
    return "disks:{} mounted:{} shares:{} remotes:{}".format(
        len(disks), mounted, len(list_shares()), remotes)


def startup():
    """Boot hook (App thread, next to wan_manager.startup()): reload the job
    table, mount everything persisted, pin the samba bind, seed hotplug."""
    global _hotplug_snapshot
    from pcat_util import nas_jobs
    nas_jobs.startup()
    _run(["block", "mount"], timeout=60)
    _assert_samba_lan_bind()
    try:
        _hotplug_snapshot = {
            n for n in os.listdir("/sys/block")
            if not n.startswith(("loop", "ram", "zram", "mtd", "ubi"))}
    except OSError:
        _hotplug_snapshot = set()
    invalidate_disk_cache()


# ═════════════════════════ Phase 2/3 additions ═══════════════════════════════

import hashlib
import secrets as _secrets

# ── partition table (parted -m machine output) ───────────────────────────────

MIN_PART_BYTES = 64 * 1024 * 1024          # refuse sub-64MB creations
FREE_REGION_FLOOR = 1024 * 1024 * 1024     # ignore free scraps under 1 GB
DATA_PART_LABEL = "pcat-data"


def parse_parted_free(output):
    """Parse `parted -sm <dev> unit B print free` into
    {table, parts:[{num,start,end,size,fs}], free:[{start,end,size}]}.
    Pure — unit tested against canned output."""
    table = "none"
    parts, free = [], []
    for line in output.strip().splitlines():
        line = line.strip().rstrip(";")
        if not line or line == "BYT":
            continue
        cols = line.split(":")
        if line.startswith("/dev/"):
            table = cols[5] if len(cols) > 5 else "none"
            continue
        if len(cols) < 5:
            continue

        def _b(v):
            return int(v.rstrip("B")) if v.rstrip("B").isdigit() else 0
        if cols[4] == "free" or (len(cols) > 4 and "free" in cols[4]):
            region = {"start": _b(cols[1]), "end": _b(cols[2]),
                      "size": _b(cols[3])}
            if region["size"] >= FREE_REGION_FLOOR:
                free.append(region)
        elif cols[0].isdigit():
            parts.append({"num": int(cols[0]), "start": _b(cols[1]),
                          "end": _b(cols[2]), "size": _b(cols[3]),
                          "fs": cols[4] or None})
    return {"table": table, "parts": parts, "free": free}


def partition_table(devpath):
    rc, out = _run(["parted", "-sm", devpath, "unit", "B", "print", "free"],
                   timeout=15)
    if rc != 0:
        return None
    return parse_parted_free(out)


class GuardError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def partition_guard(devpath, op, confirm=None, disks=None):
    """The checklist every partition operation passes at submit AND at job
    start. `op` ∈ create/delete/format/resize/create_free. create_free is
    the ONE operation allowed on a protected disk: carving a new partition
    out of free space never touches the system partitions — everything else
    on the boot disk is refused unconditionally."""
    name = os.path.basename(devpath)
    disk_name = parent_disk(name)
    disk_path = "/dev/" + disk_name
    if not os.path.exists(devpath):
        raise GuardError("bad_dev", "device does not exist")
    if is_protected(name) and op != "create_free":
        raise GuardError("protected", "system disk — refused")
    if op == "create_free" and classify_bus(disk_name) not in ("emmc", "nvme",
                                                              "usb", "sd"):
        raise GuardError("bad_dev", "unsupported device class")
    if confirm is not None and confirm != disk_name:
        raise GuardError("confirm_mismatch",
                         "confirmation text does not match device name")
    # Nothing on the TARGET partition may be mounted (delete/format/resize);
    # for whole-disk ops nothing on the disk may be mounted — EXCEPT
    # create_free, which coexists with mounted system partitions.
    mounts = mounted_map()
    if op == "create_free":
        pass
    elif op == "create":
        for src in mounts:
            if belongs_to_disk(os.path.basename(src), disk_name):
                raise GuardError("busy", "disk has mounted partitions")
    else:
        if devpath in mounts:
            raise GuardError("mounted", "partition is mounted — unmount first")
    try:
        with open("/proc/swaps") as f:
            if any(l.split()[0] == devpath for l in f.read().splitlines()[1:]):
                raise GuardError("swap", "partition is active swap")
    except OSError:
        pass
    return disk_path


def next_partition_number(table):
    return max([p["num"] for p in table["parts"]], default=0) + 1


def mkpart_argv(disk_path, table_type, start_b, end_b, name=DATA_PART_LABEL):
    """`parted mkpart` argv for the table type at hand.

    The positional after `mkpart` is NOT interchangeable: a GPT table takes a
    partition NAME, an msdos table takes a partition TYPE (primary/logical/
    extended). Passing a name to msdos fails with "invalid token: <name>",
    which is exactly how the first eMMC carve-out died — that disk ships an
    msdos table, not GPT."""
    if table_type == "msdos":
        return ["parted", "-s", "-a", "optimal", disk_path, "mkpart",
                "primary", "ext4",
                "{}B".format(start_b), "{}B".format(end_b)]
    return ["parted", "-s", "-a", "optimal", disk_path, "mkpart", name,
            "ext4", "{}B".format(start_b), "{}B".format(end_b)]


def part_device_path(disk_path, num):
    """/dev/mmcblk0 + 3 -> /dev/mmcblk0p3; /dev/sda + 3 -> /dev/sda3."""
    disk_name = os.path.basename(disk_path)
    sep = "p" if disk_name[-1].isdigit() else ""
    return "{}{}{}".format(disk_path, sep, num)


def blkid_type(part_dev):
    """Filesystem type from blkid, or "" when the partition is genuinely
    blank. Authoritative where parted is not: parted's fs column is empty
    for anything it cannot identify — including this device's squashfs
    rootfs — while blkid reports TYPE="squashfs" for it."""
    rc, out = _run(["blkid", part_dev], timeout=10)
    if rc != 0:
        return ""
    m = re.search(r'\bTYPE="([^"]+)"', out)
    return m.group(1) if m else ""


def find_orphan_data_part(disk_path, table, is_blank=None):
    """The partition a previous half-finished carve-out left behind.

    The plan is mkpart-then-mkfs, and a failure between the two (a missing
    binary, a reboot) leaves a real partition with no filesystem. Re-running
    would then try to mkpart the same range and die on "Can't have
    overlapping partitions" — the job could never recover. Detect that
    partition and resume at the mkfs step instead.

    Every candidate must clear three bars, because getting this wrong means
    reformatting a partition someone's data is on:
      1. it is the LAST partition on the disk (a carve-out always appends
         into the free tail, so nothing else can qualify),
      2. blkid finds no filesystem on it — not parted, which reports an
         empty fs for the squashfs rootfs and would hand us the running
         system to reformat,
      3. it is not itself a protected/system partition.
    """
    if not table["parts"]:
        return None
    blank = is_blank or (lambda dev: not blkid_type(dev))
    last = max(table["parts"], key=lambda p: p["start"])
    if last.get("fs"):
        return None
    dev = part_device_path(disk_path, last["num"])
    if is_protected(os.path.basename(dev)) and \
            classify_bus(os.path.basename(disk_path)) != "emmc":
        return None
    if not blank(dev):
        return None
    return last


def build_use_free_plan(disk_path, table):
    """Command plan for the one-click 'use free space as data storage':
    largest free region -> new ext4 partition labeled pcat-data. Returns
    (argv_steps, new_part_dev). Pure."""
    orphan = find_orphan_data_part(disk_path, table)
    if orphan is not None and orphan["size"] >= MIN_PART_BYTES:
        # Resume a half-finished run: the partition exists, only the
        # filesystem is missing. No parted step at all.
        return [], part_device_path(disk_path, orphan["num"])
    if not table["free"]:
        raise GuardError("no_free", "no usable free space")
    region = max(table["free"], key=lambda r: r["size"])
    num = next_partition_number(table)
    table_type = table["table"]
    steps = []
    if table_type not in ("gpt", "msdos"):
        # Unpartitioned/foreign disk: start clean with GPT. (Never reached
        # on a disk that already carries data — the guard demands nothing
        # mounted, and this branch only fires when there is no table at all.)
        steps.append(["parted", "-s", disk_path, "mklabel", "gpt"])
        table_type = "gpt"
    if table_type == "msdos" and num > 4:
        raise GuardError(
            "msdos_full",
            "msdos partition tables hold at most 4 primary partitions")
    steps.append(mkpart_argv(disk_path, table_type, region["start"],
                             region["end"]))
    return steps, part_device_path(disk_path, num)


def build_format(part_dev, fs, label=None):
    fs = (fs or "ext4").lower()
    if fs not in FORMAT_FILESYSTEMS:
        raise GuardError("bad_fs", "unsupported filesystem")
    # Refuse early when the formatter is missing (UI should grey these out).
    tools = tools_available()
    if fs in tools and not tools[fs]:
        raise GuardError("bad_fs", "mkfs for {} is not installed".format(fs))
    label = label or ""
    # vfat volume labels are max 11 chars; keep a common 16-char cap for others.
    max_label = 11 if fs == "vfat" else 16
    if label and not re.match(r"^[A-Za-z0-9._-]{1,%d}$" % max_label, label):
        raise GuardError("bad_label", "invalid label")
    # wipefs lives in util-linux-wipefs, which this firmware does not ship.
    # It is only a belt-and-braces "clear stale signatures" step — every
    # mkfs below takes a force flag and overwrites the superblock anyway —
    # so skip it rather than fail the whole job on a missing binary.
    steps = [["wipefs", "-a", part_dev]] if _which("wipefs") else []
    if fs == "ext4":
        cmd = ["mkfs.ext4", "-F"] + (["-L", label] if label else []) + [part_dev]
    elif fs == "exfat":
        cmd = ["mkfs.exfat"] + (["-L", label] if label else []) + [part_dev]
    elif fs == "vfat":
        cmd = ["mkfs.vfat", "-F", "32"] + (["-n", label] if label else []) + [part_dev]
    elif fs == "btrfs":
        cmd = ["mkfs.btrfs", "-f"] + (["-L", label] if label else []) + [part_dev]
    elif fs == "f2fs":
        cmd = ["mkfs.f2fs", "-f"] + (["-l", label] if label else []) + [part_dev]
    else:
        raise GuardError("bad_fs", "unsupported filesystem")
    steps.append(cmd)
    return steps


def build_create(disk_path, start_b, end_b, table):
    if end_b - start_b < MIN_PART_BYTES:
        raise GuardError("too_small", "partition too small")
    ok = any(r["start"] <= start_b and end_b <= r["end"]
             for r in table["free"])
    if not ok:
        raise GuardError("overlap", "range is not inside a free region")
    if table["table"] == "msdos" and next_partition_number(table) > 4:
        raise GuardError(
            "msdos_full",
            "msdos partition tables hold at most 4 primary partitions")
    return [mkpart_argv(disk_path, table["table"], start_b, end_b, "pcat")]


def build_delete(disk_path, part_num):
    return [["parted", "-s", disk_path, "rm", str(part_num)]]


def build_mklabel(disk_path, table_type="gpt"):
    """Write a fresh partition table. This DESTROYS every partition on the
    disk — the guard (op='create', i.e. nothing on the disk mounted, disk not
    protected) plus the typed-device-name confirmation are what stand between
    a click and total data loss."""
    if table_type not in ("gpt", "msdos"):
        raise GuardError("bad_table", "unsupported partition table type")
    steps = [["wipefs", "-a", disk_path]] if _which("wipefs") else []
    steps.append(["parted", "-s", disk_path, "mklabel", table_type])
    return steps


def build_resize(disk_path, part_dev, part_num, new_end_b, grow):
    """ext4 only. Grow: parted first then resize2fs. Shrink: fs first, then
    parted (needs interactive Yes — supplied via stdin marker)."""
    if grow:
        return [["parted", "-s", disk_path, "resizepart", str(part_num),
                 "{}B".format(new_end_b)],
                ["e2fsck", "-f", "-y", part_dev],
                ["resize2fs", part_dev]], None
    new_size_k = max(1, (new_end_b) // 1024)  # caller passes SIZE for shrink
    return [["e2fsck", "-f", "-y", part_dev],
            ["resize2fs", part_dev, "{}K".format(new_size_k)],
            ["parted", "---pretend-input-tty", disk_path, "resizepart",
             str(part_num), "{}B".format(new_end_b)],
            ["e2fsck", "-f", "-y", part_dev]], "Yes\n"


def run_partition_job(handle, steps, stdin_for_parted=None, part_dev=None,
                      automount_after=False):
    """Generic runner: execute the step list via the job subprocess watcher,
    partprobe, optionally automount the resulting partition."""
    total = len(steps)
    for i, argv in enumerate(steps):
        handle.check_cancel()
        handle.set_progress(pct=int(i * 90 / total),
                            message=" ".join(argv[:2]))
        stdin_data = None
        if stdin_for_parted and argv[0] == "parted" and \
                "---pretend-input-tty" in argv:
            stdin_data = stdin_for_parted
        rc = handle.watch_subprocess(argv, stdin_data=stdin_data)
        if rc != 0:
            raise RuntimeError("{} failed (rc={})".format(argv[0], rc))
    _run(["partprobe"], timeout=20)
    time.sleep(1)
    invalidate_disk_cache()
    if automount_after and part_dev:
        handle.set_progress(pct=95, message="mounting")
        deadline = time.time() + 10
        while not os.path.exists(part_dev) and time.time() < deadline:
            time.sleep(0.5)
        out = enable_automount(part_dev)
        if not out.get("ok"):
            raise RuntimeError("mount failed: {}".format(out.get("message")))
        handle.set_progress(pct=100, message=out.get("mountpoint", ""))


# ── rsync cron jobs ──────────────────────────────────────────────────────────

CRON_PATH = "/etc/crontabs/root"
_CRON_MARK = "# pcat-nas:"
_SCHEDULES = {"hourly": "15 * * * *", "daily": "30 3 * * *",
              "weekly": "45 3 * * 0"}


def render_cron_line(job):
    sched = job.get("cron") if job.get("schedule") == "cron" else \
        _SCHEDULES.get(job.get("schedule"), _SCHEDULES["daily"])
    delete = "--delete " if job.get("delete") else ""
    return ("{sched} flock -n /tmp/pcat_rsync_{id}.lock rsync -a "
            "--modify-window=2 {delete}'{src}/' '{dst}/' "
            ">/tmp/pcat_rsync_{id}.log 2>&1 {mark}{id}").format(
        sched=sched, delete=delete, src=job["src"].rstrip("/"),
        dst=job["dst"].rstrip("/"), mark=_CRON_MARK, id=job["id"])


def sync_crontab(jobs, crontab_text):
    """Replace our marked lines; every foreign line survives byte-for-byte.
    Pure — unit tested."""
    kept = [l for l in crontab_text.splitlines()
            if _CRON_MARK not in l]
    ours = [render_cron_line(j) for j in jobs if j.get("enabled")]
    out = "\n".join(kept + ours)
    return out + "\n" if out else ""


def apply_cron():
    jobs = list_sync_jobs()
    try:
        with open(CRON_PATH) as f:
            text = f.read()
    except OSError:
        text = ""
    new = sync_crontab(jobs, text)
    if new != text:
        tmp = CRON_PATH + ".pcat_tmp"
        with open(tmp, "w") as f:
            f.write(new)
        os.replace(tmp, CRON_PATH)
        _run(["/etc/init.d/cron", "restart"], timeout=15)


def list_sync_jobs():
    import pcat_config
    jobs = pcat_config.get_user_config_key("nas_rsync_jobs", None)
    return jobs if isinstance(jobs, list) else []


def save_sync_job(job_id, fields):
    import pcat_config
    jobs = list_sync_jobs()
    valid_scheds = set(_SCHEDULES) | {"cron"}
    if fields.get("schedule") not in valid_scheds:
        return {"ok": False, "code": "bad_schedule", "message": "bad schedule"}
    if fields.get("schedule") == "cron":
        cron = fields.get("cron", "")
        if not re.match(r"^[0-9*/, -]{9,64}$", cron) or \
                len(cron.split()) != 5:
            return {"ok": False, "code": "bad_cron",
                    "message": "invalid cron expression"}
    for key in ("src", "dst"):
        real = os.path.realpath(fields.get(key, ""))
        roots = managed_mount_roots()
        if not any(real == r or real.startswith(r + "/") for r in roots):
            return {"ok": False, "code": "bad_path",
                    "message": key + " must be on mounted storage"}
        fields[key] = real
    name = (fields.get("name") or "").strip()[:48] or "sync"
    with _lock:
        if job_id is None:
            job_id = "s" + _secrets.token_hex(4)
            jobs.append({"id": job_id})
        entry = next((j for j in jobs if j.get("id") == job_id), None)
        if entry is None:
            return {"ok": False, "code": "not_found", "message": "unknown job"}
        entry.update(name=name, src=fields["src"], dst=fields["dst"],
                     schedule=fields["schedule"], cron=fields.get("cron", ""),
                     delete=bool(fields.get("delete")),
                     enabled=bool(fields.get("enabled", True)))
        pcat_config.set_user_config_key("nas_rsync_jobs", jobs)
    apply_cron()
    return {"ok": True, "id": job_id}


def delete_sync_job(job_id):
    import pcat_config
    with _lock:
        jobs = [j for j in list_sync_jobs() if j.get("id") != job_id]
        pcat_config.set_user_config_key("nas_rsync_jobs", jobs)
    apply_cron()
    try:
        os.unlink("/tmp/pcat_rsync_{}.log".format(job_id))
    except OSError:
        pass
    return {"ok": True}


def run_rsync_now(handle, job):
    """Run-now job runner: same flock as cron so they never overlap;
    --info=progress2 gives a parseable percent."""
    argv = ["flock", "-n", "/tmp/pcat_rsync_{}.lock".format(job["id"]),
            "rsync", "-a", "--modify-window=2", "--info=progress2"]
    if job.get("delete"):
        argv.append("--delete")
    argv += [job["src"].rstrip("/") + "/", job["dst"].rstrip("/") + "/"]

    def _parse(line):
        m = re.search(r"(\d+)%", line)
        if m:
            handle.set_progress(pct=int(m.group(1)))
    rc = handle.watch_subprocess(argv, line_cb=_parse)
    if rc == 1 and "flock" in " ".join(argv):
        raise RuntimeError("another run of this job is in progress")
    if rc != 0:
        raise RuntimeError("rsync failed (rc={})".format(rc))


def sync_log_tail(job_id, limit=65536):
    if not re.match(r"^s[0-9a-f]{8}$", job_id):
        return ""
    try:
        with open("/tmp/pcat_rsync_{}.log".format(job_id), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def backup_status():
    """SD -> NVMe one-click backup preset state."""
    sd = nvme = None
    for disk in list_disks():
        for part in disk["partitions"]:
            if part["mounted"]:
                if disk["bus"] == "sd" and sd is None:
                    sd = part
                if disk["bus"] == "nvme" and nvme is None:
                    nvme = part
    return {"sd": sd, "nvme": nvme,
            "ready": bool(sd and nvme),
            "dest": (nvme["mountpoint"] + "/photonicat-sd-backup") if nvme else None}


# ── remote mounts (sshfs + rclone/S3) ────────────────────────────────────────

REMOTE_BASE = "/mnt/remote"
RCLONE_CONF = "/etc/pcat-nas/rclone.conf"
_remote_backoff = {}   # id -> {"until": ts, "delay": s}
_REMOTE_BACKOFF_MIN = 30
_REMOTE_BACKOFF_MAX = 600


def list_remotes():
    import pcat_config
    remotes = pcat_config.get_user_config_key("nas_remote_mounts", None)
    return remotes if isinstance(remotes, list) else []


def _remote_public(remote):
    """Secrets never leave the box."""
    out = {k: v for k, v in remote.items()
           if k not in ("password", "secret_key")}
    out["has_secret"] = bool(remote.get("password") or
                             remote.get("secret_key"))
    out["state"] = "mounted" if _remote_alive(remote["mountpoint"]) else \
        ("disconnected" if remote.get("enabled") else "disabled")
    return out


def save_remote(remote_id, fields):
    import pcat_config
    rtype = fields.get("type")
    if rtype not in ("sshfs", "s3"):
        return {"ok": False, "code": "bad_type", "message": "bad type"}
    name = (fields.get("name") or "").strip()
    if not re.match(r"^[A-Za-z0-9._-]{1,24}$", name):
        return {"ok": False, "code": "bad_name",
                "message": "name: letters/digits/._- only"}
    with _lock:
        remotes = list_remotes()
        if remote_id is None:
            remote_id = "r" + _secrets.token_hex(4)
            remotes.append({"id": remote_id})
        entry = next((r for r in remotes if r.get("id") == remote_id), None)
        if entry is None:
            return {"ok": False, "code": "not_found", "message": "unknown"}
        entry.update(
            type=rtype, name=name,
            mountpoint=os.path.join(REMOTE_BASE, name),
            enabled=bool(fields.get("enabled", True)),
            host=(fields.get("host") or "").strip()[:128],
            port=int(fields.get("port") or (22 if rtype == "sshfs" else 443)),
            user=(fields.get("user") or "").strip()[:64],
            path=(fields.get("path") or "/").strip()[:256],
            endpoint=(fields.get("endpoint") or "").strip()[:256],
            bucket=(fields.get("bucket") or "").strip()[:128],
            access_key=(fields.get("access_key") or "").strip()[:128])
        if fields.get("password"):
            entry["password"] = fields["password"]
        if fields.get("secret_key"):
            entry["secret_key"] = fields["secret_key"]
        pcat_config.set_user_config_key("nas_remote_mounts", remotes)
    return {"ok": True, "id": remote_id}


def delete_remote(remote_id):
    import pcat_config
    with _lock:
        remotes = list_remotes()
        entry = next((r for r in remotes if r.get("id") == remote_id), None)
        if entry:
            unmount_remote(entry)
        remotes = [r for r in remotes if r.get("id") != remote_id]
        pcat_config.set_user_config_key("nas_remote_mounts", remotes)
    _remote_backoff.pop(remote_id, None)
    return {"ok": True}


def _write_rclone_remote(remote):
    """Maintain a private rclone.conf section per S3 remote (0600)."""
    os.makedirs(os.path.dirname(RCLONE_CONF), exist_ok=True)
    import configparser
    cp = configparser.ConfigParser()
    cp.read(RCLONE_CONF)
    section = "pcat_" + remote["id"]
    if not cp.has_section(section):
        cp.add_section(section)
    cp.set(section, "type", "s3")
    cp.set(section, "provider", "Other")
    cp.set(section, "endpoint", remote.get("endpoint", ""))
    cp.set(section, "access_key_id", remote.get("access_key", ""))
    if remote.get("secret_key"):
        cp.set(section, "secret_access_key", remote["secret_key"])
    fd = os.open(RCLONE_CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        cp.write(f)
    return section


def mount_remote(remote):
    mp = remote["mountpoint"]
    try:
        os.makedirs(mp, exist_ok=True)
    except OSError as e:
        return {"ok": False, "code": "io_error", "message": str(e)}
    if _remote_alive(mp):
        return {"ok": True}
    _run(["umount", "-l", mp], timeout=10)  # clear any dead fuse handle
    if remote["type"] == "sshfs":
        argv = ["sshfs", "-o",
                "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,"
                "StrictHostKeyChecking=accept-new,password_stdin",
                "-p", str(remote.get("port", 22)),
                "{}@{}:{}".format(remote["user"], remote["host"],
                                  remote.get("path", "/")), mp]
        rc, out = _run(argv, timeout=30,
                       stdin_data=(remote.get("password", "") + "\n"))
    else:
        section = _write_rclone_remote(remote)
        argv = ["rclone", "mount", "--config", RCLONE_CONF, "--daemon",
                "--vfs-cache-mode", "writes",
                "{}:{}".format(section, remote.get("bucket", "")), mp]
        rc, out = _run(argv, timeout=45)
    if rc != 0 or not _wait_mounted(mp):
        return {"ok": False, "code": "mount_failed",
                "message": (out or "").strip()[-300:] or "mount failed"}
    return {"ok": True}


def _wait_mounted(mp, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mp in _read_proc_mounts():
            return True
        time.sleep(0.5)
    return False


def unmount_remote(remote):
    _run(["umount", remote["mountpoint"]], timeout=15)
    if remote["mountpoint"] in _read_proc_mounts():
        _run(["umount", "-l", remote["mountpoint"]], timeout=10)
    return {"ok": True}


def _remote_alive(mp):
    """In /proc/mounts AND statvfs answers within 3s (a dead fuse mount
    hangs statvfs forever — probe from a scratch thread)."""
    if not any(line.split()[1] == mp for line in
               _read_proc_mounts().splitlines() if len(line.split()) > 1):
        return False
    result = {}

    def _probe():
        try:
            os.statvfs(mp)
            result["ok"] = True
        except OSError:
            result["ok"] = False
    t = threading.Thread(target=_probe, daemon=True)
    t.start()
    t.join(3)
    return result.get("ok", False)


def remote_mount_roots():
    """Live remote mountpoints — joined into the file-manager jail."""
    return [r["mountpoint"] for r in list_remotes()
            if r.get("enabled") and _remote_alive(r["mountpoint"])]


def reconcile_remotes():
    """Keeper: remount dead enabled remotes with 30s->10min backoff; reset
    all backoffs when the WAN comes back so recovery is immediate."""
    remotes = [r for r in list_remotes() if r.get("enabled")]
    if not remotes:
        return "no_remotes"
    try:
        import app as pc_app
        wan_up = bool(pc_app.get_active_egress())
    except Exception:
        wan_up = True
    was_up = _remote_backoff.get("_wan", {}).get("up", True)
    _remote_backoff["_wan"] = {"up": wan_up}
    if wan_up and not was_up:
        for key in list(_remote_backoff):
            if key != "_wan":
                _remote_backoff.pop(key)
    summary = []
    for remote in remotes:
        if _remote_alive(remote["mountpoint"]):
            _remote_backoff.pop(remote["id"], None)
            summary.append(remote["name"] + ":ok")
            continue
        if not wan_up:
            summary.append(remote["name"] + ":wan_down")
            continue
        state = _remote_backoff.get(remote["id"],
                                    {"until": 0, "delay": _REMOTE_BACKOFF_MIN})
        if time.time() < state["until"]:
            summary.append(remote["name"] + ":backoff")
            continue
        out = mount_remote(remote)
        if out.get("ok"):
            _remote_backoff.pop(remote["id"], None)
            summary.append(remote["name"] + ":remounted")
            pcat_logger.logger.info(
                "nas_manager: remote {} remounted".format(remote["name"]))
        else:
            state = {"until": time.time() + state["delay"],
                     "delay": min(state["delay"] * 2, _REMOTE_BACKOFF_MAX)}
            _remote_backoff[remote["id"]] = state
            summary.append(remote["name"] + ":failed")
    return " ".join(summary)


# ── public share links (Nextcloud style, optional password) ──────────────────

def _hash_link_password(password):
    salt = _secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 100000).hex()
    return salt + "$" + digest


def check_link_password(link, password):
    stored = link.get("password_hash", "")
    if "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    calc = hashlib.pbkdf2_hmac("sha256", (password or "").encode(),
                               salt.encode(), 100000).hex()
    return _secrets.compare_digest(calc, digest)


def list_links():
    import pcat_config
    links = pcat_config.get_user_config_key("nas_share_links", None)
    return links if isinstance(links, list) else []


def link_public(link):
    out = {k: v for k, v in link.items() if k != "password_hash"}
    out["has_password"] = bool(link.get("password_hash"))
    return out


def create_link(path, password=None):
    import pcat_config
    real = os.path.realpath(path)
    roots = managed_mount_roots() + remote_mount_roots()
    if not any(real == r or real.startswith(r + "/") for r in roots):
        return {"ok": False, "code": "bad_path",
                "message": "path not on mounted storage"}
    if not os.path.exists(real):
        return {"ok": False, "code": "bad_path", "message": "path gone"}
    link = {
        "token": _secrets.token_urlsafe(12),
        "path": real,
        "type": "dir" if os.path.isdir(real) else "file",
        "name": os.path.basename(real.rstrip("/")) or "root",
        "created": int(time.time()),
    }
    if password:
        if len(password) < 4:
            return {"ok": False, "code": "bad_password",
                    "message": "password too short"}
        link["password_hash"] = _hash_link_password(password)
    with _lock:
        links = list_links()
        links.append(link)
        pcat_config.set_user_config_key("nas_share_links", links)
    return {"ok": True, "link": link_public(link)}


def delete_link(token):
    import pcat_config
    with _lock:
        links = [l for l in list_links() if l.get("token") != token]
        pcat_config.set_user_config_key("nas_share_links", links)
    return {"ok": True}


def find_link(token):
    if not isinstance(token, str) or not re.match(r"^[A-Za-z0-9_-]{8,32}$",
                                                  token):
        return None
    return next((l for l in list_links() if l.get("token") == token), None)
