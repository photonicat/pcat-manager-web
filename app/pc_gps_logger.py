# -*- coding: utf-8 -*-
"""Smart GNSS position logger behind the /gps page's settings panel.

Samples the live fix from pc_gps_client every second and keeps a point when
the recording profile says to: the "smart" profiles record on movement past
a distance threshold, a turn past a bearing threshold, or a heartbeat; the
fixed-interval profiles record every N seconds (1 s .. 1 h), fix permitting.

Storage is built for router flash and long recordings:

  * Fixed-width binary records at a user-chosen verbosity (12/16/20 bytes:
    FORMATS), appended to an ACTIVE segment file `trk-<YYYYMMDD-HHMMSS>.bin`
    named by its (UTC) start time. Each file starts with an 8-byte header
    (magic + format code); header-less files from the first release parse
    as the 16-byte standard format.
  * A segment CLOSES — and is compressed to `.bin.zst` (zstd CLI when the
    device has it) or `.bin.gz` (stdlib fallback) — when the rotation period
    rolls over (weekly/monthly, user setting), when the raw file hits
    MAX_SEGMENT_BYTES, or on app start. The startup close is what gives
    "every reboot starts a new file".
  * The whole store lives in TRACK_DIR — outside the app directory so
    redeploys (`rsync --delete`) don't wipe it, same reasoning as
    pcat_util/osm_tiles.py — and is capped by a user setting (8 MB .. 1 GB);
    oldest CLOSED segments are evicted first, the active one never.
  * `index.json` caches {points, start_t, end_t} per closed segment so
    listing tracks never has to decompress anything; a missing entry (crash
    between compress and index write) is rebuilt lazily.

Readers tolerate a torn final record — appends happen while the page reads —
by decoding only complete records. The logger runs whether or not anyone
watches the page; enablement lives in user config so it survives reboots,
like the GNSS power flag.
"""

import gzip
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time

import pcat_config
import pc_gps_client
from pcat_util import pcat_logger

TRACK_DIR = "/etc/pcat-manager-web/tracks"
# External media (SD/NVMe/USB mounted under /mnt) get their own store in
# this subdirectory of the mount point. Each store is self-contained (files
# + index), so tracks survive firmware flashes with the card and the two
# stores never mix.
EXT_SUBDIR = "pcat-tracks"
INDEX_NAME = "index.json"

# Write-amplification control: records buffer in RAM and reach the flash as
# page-sized appends (or once a minute, whichever comes first). eMMC/SD
# program whole pages whatever the write size, so 16-byte writes every
# second would burn erase cycles ~250x faster than needed. The cost: a
# power cut loses at most FLUSH_SECS of buffered points.
FLUSH_BYTES = 4096
FLUSH_SECS = 60

# Record verbosity. Common prefix: t (unix s), lat/lon (deg * 1e7 — ~1 cm,
# well past GNSS accuracy). standard adds alt (m, int16 covers anywhere a
# router goes) + speed (0.1 km/h); verbose adds course (0.1°, 0xFFFF = no
# data), hdop (0.1, 0xFF = no data) and satellites used (0xFF = no data).
FORMATS = {
    "minimal":  {"code": 0, "rec": struct.Struct("<Iii")},
    "standard": {"code": 1, "rec": struct.Struct("<IiihH")},
    "verbose":  {"code": 2, "rec": struct.Struct("<IiihHHBB")},
}
CODE_TO_FMT = {v["code"]: k for k, v in FORMATS.items()}
DEFAULT_FMT = "standard"
# File header: magic + format code + 3 pad bytes. Files without it (the
# first release wrote bare records) are read as the standard format — a
# record's first field is a current unix time (~1.7e9), which can never
# equal MAGIC read as uint32 (~1.2e9, mid-2010).
MAGIC = b"PCTK"
HEADER = struct.Struct("<4sB3x")

# 1 s so the fastest recording profile ("every second") sees every fix.
SAMPLE_SECS = 1.0

# A raw segment that reaches this rolls over to a new file even mid-period
# ("if a single file gets too big, make a new one"). 4 MB = 262k points,
# about 3 days of 1 s recording.
MAX_SEGMENT_BYTES = 4 * 1024 * 1024

CAP_MB_CHOICES = (32, 128, 512)   # user setting; 512 MB max on internal
DEFAULT_CAP_MB = 32
ROTATE_CHOICES = ("weekly", "monthly")
DEFAULT_ROTATE = "weekly"

ENABLED_KEY = "gps_logger_enabled"
MODE_KEY = "gps_logger_mode"
CAP_KEY = "gps_logger_cap_mb"
ROTATE_KEY = "gps_logger_rotate"
FMT_KEY = "gps_logger_fmt"
STORE_KEY = "gps_logger_store"

# Smart profiles: min_m always earns a point, turn_deg earns one early
# (with a move gate so jitter can't fake a turn), beat_s leaves a sparse
# presence trail. Interval profiles: one point per N seconds, movement or
# not (still needs a fix).
MODES = {
    "fine":     {"min_m": 5.0,  "turn_deg": 15.0, "beat_s": 60},
    "normal":   {"min_m": 15.0, "turn_deg": 25.0, "beat_s": 180},
    "coarse":   {"min_m": 50.0, "turn_deg": 40.0, "beat_s": 600},
    "int_1":    {"interval": 1},
    "int_10":   {"interval": 10},
    "int_60":   {"interval": 60},
    "int_600":  {"interval": 600},
    "int_3600": {"interval": 3600},
}
DEFAULT_MODE = "normal"

# Viewer default: cap points sent to the browser; the polyline looks the
# same at screen resolution and the JSON stays small.
VIEW_MAX_POINTS = 2000

_SEG_ID_RE = re.compile(r"\d{8}-\d{6}")
# Preference order for reading a segment id off disk.
_SEG_SUFFIXES = (".bin", ".bin.zst", ".bin.gz")

_ZSTD = shutil.which("zstd")


def _dist_m(lat1, lon1, lat2, lon2):
    """Haversine distance in meters."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _bearing_deg(lat1, lon1, lat2, lon2):
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(rlat2)
    x = (math.cos(rlat1) * math.sin(rlat2)
         - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _ang_diff(a, b):
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d


def _mount_kind(dev):
    base = os.path.basename(dev)
    if base.startswith("nvme"):
        return "nvme"
    if base.startswith("mmcblk"):
        return "sd"
    return "usb"


def storage_options():
    """Places tracks can live: internal flash plus block devices mounted
    under /mnt. Each entry: key (the config value), kind, free bytes."""
    opts = []
    try:
        st = os.statvfs(os.path.dirname(TRACK_DIR))
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = 0
    opts.append({"key": "internal", "kind": "internal", "free": free})
    try:
        with open("/proc/mounts") as f:
            mounts = f.read().splitlines()
    except OSError:
        return opts
    seen = set()
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        if not dev.startswith("/dev/") or not mnt.startswith("/mnt/"):
            continue
        if fstype in ("squashfs", "overlay", "ramfs", "tmpfs") or mnt in seen:
            continue
        seen.add(mnt)
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        opts.append({"key": mnt, "kind": _mount_kind(dev),
                     "free": st.f_bavail * st.f_frsize})
    return opts


def _valid_seg_id(seg_id):
    return isinstance(seg_id, str) and bool(_SEG_ID_RE.fullmatch(seg_id))


def _seg_id_for(t):
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(t))


def _compress_file(path):
    """Compress a closed raw segment in place; returns the stored path.
    Any failure leaves the raw file as-is — a fat segment beats a lost one."""
    try:
        if _ZSTD:
            subprocess.run([_ZSTD, "-q", "-f", "--rm", path],
                           timeout=120, check=True)
            return path + ".zst"
        with open(path, "rb") as src, gzip.open(path + ".gz", "wb",
                                                compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        os.remove(path)
        return path + ".gz"
    except Exception as e:
        pcat_logger.logger.warning(f"gps-logger: compress {path} failed: {e}")
        for leftover in (path + ".zst", path + ".gz"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        return path


def _read_stored(path):
    """Decompressed record bytes of a stored segment, or None."""
    try:
        if path.endswith(".zst"):
            if not _ZSTD:
                return None
            out = subprocess.run([_ZSTD, "-dc", path], timeout=120,
                                 check=True, capture_output=True)
            return out.stdout
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                return f.read()
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        pcat_logger.logger.warning(f"gps-logger: read {path} failed: {e}")
        return None


def _blob_layout(blob):
    """(format name, payload offset) of a segment blob."""
    if len(blob) >= HEADER.size and blob[:4] == MAGIC:
        return CODE_TO_FMT.get(blob[4], DEFAULT_FMT), HEADER.size
    return "standard", 0   # header-less first-release file


def _parse_blob(blob):
    """Records normalized to (t, lat7, lon7, alt, spd10, course10, hdop10,
    sats) with None where the format doesn't carry the field."""
    fmt, off = _blob_layout(blob)
    rec = FORMATS[fmt]["rec"]
    n = (len(blob) - off) // rec.size
    out = []
    for i in range(n):
        v = rec.unpack_from(blob, off + i * rec.size)
        if fmt == "minimal":
            out.append((v[0], v[1], v[2], None, None, None, None, None))
        elif fmt == "standard":
            out.append(v + (None, None, None))
        else:
            out.append(v[:5] + (v[5] if v[5] != 0xFFFF else None,
                                v[6] if v[6] != 0xFF else None,
                                v[7] if v[7] != 0xFF else None))
    return out


def _scan_meta(blob):
    """(points, start_t, end_t) of a segment blob, decoding only the first
    and last record."""
    fmt, off = _blob_layout(blob)
    rec = FORMATS[fmt]["rec"]
    n = (len(blob) - off) // rec.size
    if n <= 0:
        return 0, None, None
    first = rec.unpack_from(blob, off)
    last = rec.unpack_from(blob, off + (n - 1) * rec.size)
    return n, first[0], last[0]


def _full_meta(blob):
    """Index entry for a segment: {points, start_t, end_t, dist_m, max_kmh}.
    Walks every record (unlike _scan_meta) — run once per closed segment when
    it is indexed, so the tracks table can show distance/top speed without
    ever decompressing anything again. max_kmh is None for the minimal
    format, which carries no speed."""
    recs = _parse_blob(blob)
    if not recs:
        return None
    dist = 0.0
    max_spd = None
    for i in range(1, len(recs)):
        dist += _dist_m(recs[i - 1][1] / 1e7, recs[i - 1][2] / 1e7,
                        recs[i][1] / 1e7, recs[i][2] / 1e7)
    for r in recs:
        if r[4] is not None and (max_spd is None or r[4] > max_spd):
            max_spd = r[4]
    return {"points": len(recs), "start_t": recs[0][0], "end_t": recs[-1][0],
            "dist_m": round(dist),
            "max_kmh": max_spd / 10.0 if max_spd is not None else None}


class GpsLogger:
    def __init__(self):
        self._thread = None
        self._lock = threading.Lock()   # keep-chain + active segment state
        self._ilock = threading.Lock()  # index.json read-modify-write
        self._last = None               # (t, lat, lon) of last kept point
        self._last_bearing = None       # bearing into the last kept point
        self._seg = None                # {"id","path","points","start_t","end_t"}
        # brief() is polled every 2 s by the page; cache the answer.
        self._brief_cache = {"t": 0.0, "value": None}

    # ---------------- config ----------------

    def enabled(self):
        return bool(pcat_config.get_user_config_key(ENABLED_KEY, False))

    def mode(self):
        m = pcat_config.get_user_config_key(MODE_KEY, DEFAULT_MODE)
        return m if m in MODES else DEFAULT_MODE

    def cap_mb(self):
        """0 = no limit (external media only)."""
        mb = pcat_config.get_user_config_key(CAP_KEY, DEFAULT_CAP_MB)
        return mb if mb in CAP_MB_CHOICES or mb == 0 else DEFAULT_CAP_MB

    def cap_bytes(self):
        """Byte cap for eviction, or None for uncapped. "No limit" is only
        honored on removable media — on the shared internal eMMC it clamps
        to the largest choice so tracks can't crowd out the overlay."""
        mb = self.cap_mb()
        if mb == 0:
            if self.store_key() == "internal":
                return CAP_MB_CHOICES[-1] * 1024 * 1024
            return None
        return mb * 1024 * 1024

    def rotate(self):
        r = pcat_config.get_user_config_key(ROTATE_KEY, DEFAULT_ROTATE)
        return r if r in ROTATE_CHOICES else DEFAULT_ROTATE

    def fmt(self):
        f = pcat_config.get_user_config_key(FMT_KEY, DEFAULT_FMT)
        return f if f in FORMATS else DEFAULT_FMT

    def set_fmt(self, fmt):
        if fmt not in FORMATS:
            return False
        # The active segment keeps its own format; _append rolls over to a
        # new file when the setting no longer matches it.
        pcat_config.set_user_config_key(FMT_KEY, fmt)
        return True

    def store_key(self):
        s = pcat_config.get_user_config_key(STORE_KEY, "internal")
        return s if isinstance(s, str) and s else "internal"

    def _dir(self):
        """Track directory for the configured storage, or None when the
        chosen external medium is not mounted (recording then pauses —
        silently falling back to eMMC would defeat the point of choosing
        removable storage)."""
        key = self.store_key()
        if key == "internal":
            return TRACK_DIR
        if os.path.ismount(key):
            return os.path.join(key, EXT_SUBDIR)
        return None

    def set_store(self, key):
        if key not in {o["key"] for o in storage_options()}:
            return False
        if key == self.store_key():
            return True
        # Seal the active segment in the old location first — stores are
        # self-contained, a segment never straddles two media.
        self._close_active()
        pcat_config.set_user_config_key(STORE_KEY, key)
        # "No limit" is an external-only concept; coming back to the shared
        # internal eMMC turns it into the largest real limit.
        if key == "internal" and self.cap_mb() == 0:
            pcat_config.set_user_config_key(CAP_KEY, CAP_MB_CHOICES[-1])
        try:
            self._startup_sweep()
        except Exception as e:
            pcat_logger.logger.warning(
                f"gps-logger: sweep after store change: {e}")
        self._brief_cache["t"] = 0.0
        return True

    def _close_active(self):
        with self._lock:
            seg = self._seg
            self._seg = None
            if seg is None:
                return
            self._flush_seg_locked(seg)
        self._close_segment_file(seg["path"], seg["id"])

    def set_enabled(self, on):
        pcat_config.set_user_config_key(ENABLED_KEY, bool(on))
        if not on:
            # Next enable starts a fresh chain: no phantom straight line
            # bridging the gap (the viewer splits on time gaps anyway).
            with self._lock:
                self._last = None
                self._last_bearing = None

    def set_mode(self, mode):
        if mode not in MODES:
            return False
        pcat_config.set_user_config_key(MODE_KEY, mode)
        return True

    def set_cap_mb(self, mb):
        if mb not in CAP_MB_CHOICES and mb != 0:
            return False
        pcat_config.set_user_config_key(CAP_KEY, mb)
        # Shrinking the cap takes effect now, not at the next rollover.
        self._enforce_cap()
        return True

    def set_rotate(self, rotate):
        if rotate not in ROTATE_CHOICES:
            return False
        pcat_config.set_user_config_key(ROTATE_KEY, rotate)
        return True

    def _period_key(self, t):
        # %G-%V = ISO year-week, so the weekly boundary is Monday 00:00 UTC.
        fmt = "%G-%V" if self.rotate() == "weekly" else "%Y%m"
        return time.strftime(fmt, time.gmtime(t))

    # ---------------- sampling thread ----------------

    def ensure_started(self):
        if self._thread and self._thread.is_alive():
            return
        try:
            self._startup_sweep()
        except Exception as e:
            pcat_logger.logger.warning(f"gps-logger: startup sweep failed: {e}")
        self._thread = threading.Thread(target=self._loop,
                                        name="gps-logger", daemon=True)
        self._thread.start()

    def _startup_sweep(self):
        """Close whatever a previous boot (or a previous stint on this
        medium) left open, so recording starts a fresh segment file. Also
        migrates the short-lived daily-file layout (YYYYMMDD.bin)."""
        d = self._dir()
        if d is None or not os.path.isdir(d):
            return
        for f in os.listdir(d):
            if re.fullmatch(r"\d{8}\.bin", f):
                os.rename(os.path.join(d, f),
                          os.path.join(d, f"trk-{f[:8]}-000000.bin"))
        for f in sorted(os.listdir(d)):
            m = re.fullmatch(r"trk-(\d{8}-\d{6})\.bin", f)
            if m:
                self._close_segment_file(os.path.join(d, f), m.group(1))

    def _loop(self):
        while True:
            time.sleep(SAMPLE_SECS)
            try:
                self._tick()
            except Exception as e:
                pcat_logger.logger.warning(f"gps-logger: tick failed: {e}")

    def _tick(self):
        # Drain an aging write buffer even when nothing new is recorded
        # (stopped moving, receiver off, logger just disabled).
        with self._lock:
            seg = self._seg
            if (seg is not None and seg.get("buf")
                    and time.time() - seg["flushed_t"] >= FLUSH_SECS):
                self._flush_seg_locked(seg)
        if not self.enabled():
            return
        client = pc_gps_client.gps_client
        if not client.powered:
            return
        st = client.get_status()
        fix = st.get("fix") or {}
        if not fix.get("has_fix"):
            return
        lat, lon = fix.get("lat"), fix.get("lon")
        if lat is None or lon is None:
            return
        now = int(time.time())
        with self._lock:
            keep, bearing = self._decide(now, lat, lon)
            if not keep:
                return
            self._last = (now, lat, lon)
            if bearing is not None:
                self._last_bearing = bearing
        self._append(now, lat, lon, fix)

    def _decide(self, now, lat, lon):
        """(keep?, new bearing). Caller holds the lock."""
        if self._last is None:
            return True, None
        last_t, last_lat, last_lon = self._last
        p = MODES[self.mode()]
        if "interval" in p:
            return now - last_t >= p["interval"], None
        d = _dist_m(last_lat, last_lon, lat, lon)
        bearing = _bearing_deg(last_lat, last_lon, lat, lon) if d >= 2.0 else None
        if d >= p["min_m"]:
            return True, bearing
        # A real turn shows up long before min_m on slow, twisty movement;
        # the d gate keeps stationary jitter from minting fake corners.
        if (bearing is not None and self._last_bearing is not None
                and d >= max(3.0, p["min_m"] / 3.0)
                and _ang_diff(bearing, self._last_bearing) >= p["turn_deg"]):
            return True, bearing
        # Heartbeat: parked or crawling, leave a sparse presence trail —
        # but not for pure jitter right on top of the last point.
        if now - last_t >= p["beat_s"] and d >= 2.0:
            return True, bearing
        return False, None

    # ---------------- segment lifecycle ----------------

    def _open_segment(self, t, d):
        """New active segment named by start time. Caller holds the lock."""
        seg_id = _seg_id_for(t)
        # Same-second collision with an existing file (rapid restarts):
        # nudge forward rather than appending to a foreign segment.
        while any(os.path.exists(os.path.join(d, f"trk-{seg_id}{s}"))
                  for s in _SEG_SUFFIXES):
            t += 1
            seg_id = _seg_id_for(t)
        self._seg = {"id": seg_id,
                     "path": os.path.join(d, f"trk-{seg_id}.bin"),
                     "points": 0, "start_t": t, "end_t": t,
                     "fmt": self.fmt(),
                     "buf": bytearray(), "flushed_t": float(t)}

    def _close_segment_file(self, path, seg_id):
        """Index + compress a raw segment file that is done growing."""
        blob = _read_stored(path)
        meta = _full_meta(blob or b"")
        if meta is None:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        stored = _compress_file(path)
        self._index_update(os.path.dirname(path), seg_id, meta)
        pcat_logger.logger.info(
            f"gps-logger: closed segment {seg_id}: {meta['points']} pts, "
            f"{os.path.getsize(stored)} bytes on disk")
        self._enforce_cap()

    def _pack_record(self, fmt, t, lat, lon, fix):
        lat7, lon7 = int(round(lat * 1e7)), int(round(lon * 1e7))
        if fmt == "minimal":
            return FORMATS[fmt]["rec"].pack(t, lat7, lon7)
        alt_m = fix.get("alt_m")
        alt = int(max(-32768, min(32767, round(alt_m)))) if alt_m is not None else 0
        spd = int(max(0, min(65535, round((fix.get("speed_kmh") or 0.0) * 10))))
        if fmt == "standard":
            return FORMATS[fmt]["rec"].pack(t, lat7, lon7, alt, spd)
        course = fix.get("course_deg")
        course10 = int(round(course * 10)) % 3600 if course is not None else 0xFFFF
        hdop = fix.get("hdop")
        hdop10 = min(0xFE, int(round(hdop * 10))) if hdop is not None else 0xFF
        sats = fix.get("sats_used")
        sats = min(0xFE, int(sats)) if sats else 0xFF
        return FORMATS[fmt]["rec"].pack(t, lat7, lon7, alt, spd,
                                        course10, hdop10, sats)

    def _flush_seg_locked(self, seg):
        """One append write for everything buffered. Caller holds the lock."""
        buf = seg.get("buf")
        seg["flushed_t"] = time.time()
        if not buf:
            return
        try:
            with open(seg["path"], "ab") as f:
                if f.tell() == 0:
                    f.write(HEADER.pack(MAGIC, FORMATS[seg["fmt"]]["code"]))
                f.write(buf)
            seg["buf"] = bytearray()
        except OSError as e:
            pcat_logger.logger.warning(f"gps-logger: flush failed: {e}")
            # Unwritable storage must not grow the buffer forever; dropping
            # whole records keeps the file format aligned.
            if len(buf) > 16 * FLUSH_BYTES:
                seg["buf"] = bytearray()

    def flush_pending(self):
        """Push buffered points to disk now — called before reads so the
        viewer and downloads see everything recorded so far."""
        with self._lock:
            if self._seg is not None:
                self._flush_seg_locked(self._seg)

    def _append(self, t, lat, lon, fix):
        fmt = self.fmt()
        rec = self._pack_record(fmt, t, lat, lon, fix)
        d = self._dir()
        if d is None:
            return   # chosen medium unmounted; status() surfaces the pause
        os.makedirs(d, exist_ok=True)
        with self._lock:
            to_close = None
            if self._seg is not None:
                rec_size = FORMATS[self._seg.get("fmt", "standard")]["rec"].size
                full = (HEADER.size + (self._seg["points"] + 1) * rec_size
                        > MAX_SEGMENT_BYTES)
                rolled = (self._period_key(self._seg["start_t"])
                          != self._period_key(t))
                refmt = self._seg.get("fmt") != fmt
                if full or rolled or refmt:
                    to_close = self._seg
                    self._flush_seg_locked(to_close)
                    self._seg = None
            if self._seg is None:
                self._open_segment(t, d)
            seg = self._seg
            seg["buf"] += rec
            seg["points"] += 1
            seg["end_t"] = t
            if (len(seg["buf"]) >= FLUSH_BYTES
                    or t - seg["flushed_t"] >= FLUSH_SECS):
                self._flush_seg_locked(seg)
        self._brief_cache["t"] = 0.0
        if to_close is not None:
            self._close_segment_file(to_close["path"], to_close["id"])

    # ---------------- index ----------------

    def _index_load(self, d):
        try:
            with open(os.path.join(d, INDEX_NAME)) as f:
                idx = json.load(f)
            return idx if isinstance(idx, dict) else {}
        except (OSError, ValueError):
            return {}

    def _index_update(self, d, seg_id, entry):
        with self._ilock:
            idx = self._index_load(d)
            if entry is None:
                idx.pop(seg_id, None)
            else:
                idx[seg_id] = entry
            path = os.path.join(d, INDEX_NAME)
            try:
                with open(path + ".tmp", "w") as f:
                    json.dump(idx, f)
                os.replace(path + ".tmp", path)
            except OSError as e:
                pcat_logger.logger.warning(f"gps-logger: index write: {e}")

    # ---------------- store walking ----------------

    def _stored_files(self, d=None):
        """{seg_id: stored filename} for everything on disk, including the
        active raw file."""
        d = d or self._dir()
        if d is None:
            return {}
        try:
            names = os.listdir(d)
        except OSError:
            return {}
        out = {}
        for f in names:
            m = re.fullmatch(r"trk-(\d{8}-\d{6})\.bin(\.zst|\.gz)?", f)
            if m:
                out[m.group(1)] = f
        return out

    def _seg_path_for(self, seg_id):
        d = self._dir()
        if d is None or not _valid_seg_id(seg_id):
            return None
        f = self._stored_files(d).get(seg_id)
        return os.path.join(d, f) if f else None

    def _enforce_cap(self):
        d = self._dir()
        if d is None:
            return
        cap = self.cap_bytes()
        if cap is None:
            return
        with self._lock:
            active_id = self._seg["id"] if self._seg else None
        files = self._stored_files(d)
        sizes = {}
        for seg_id, f in files.items():
            try:
                sizes[seg_id] = os.path.getsize(os.path.join(d, f))
            except OSError:
                sizes[seg_id] = 0
        total = sum(sizes.values())
        # Oldest first (ids sort chronologically); the active segment and
        # size-capped raw files never count as evictable.
        for seg_id in sorted(files):
            if total <= cap:
                break
            if seg_id == active_id:
                continue
            try:
                os.remove(os.path.join(d, files[seg_id]))
                total -= sizes[seg_id]
                self._index_update(d, seg_id, None)
                pcat_logger.logger.info(
                    f"gps-logger: evicted segment {seg_id} (store cap)")
            except OSError:
                pass

    def _store_bytes(self):
        d = self._dir()
        if d is None:
            return 0
        try:
            return sum(os.path.getsize(os.path.join(d, f))
                       for f in self._stored_files(d).values())
        except OSError:
            return 0

    # ---------------- read side ----------------

    def brief(self):
        """Tiny summary piggybacked on every /api/v1/gps.json poll."""
        now = time.time()
        if self._brief_cache["value"] is None or now - self._brief_cache["t"] > 5:
            with self._lock:
                pts = self._seg["points"] if self._seg else 0
            self._brief_cache["value"] = {"on": self.enabled(), "points": pts}
            self._brief_cache["t"] = now
        else:
            self._brief_cache["value"]["on"] = self.enabled()
        return dict(self._brief_cache["value"])

    def status(self):
        return {
            "enabled": self.enabled(),
            "mode": self.mode(),
            "modes": list(MODES.keys()),
            "fmt": self.fmt(),
            "fmt_choices": list(FORMATS.keys()),
            "store_bytes": self._store_bytes(),
            "cap_mb": self.cap_mb(),
            "cap_choices_mb": list(CAP_MB_CHOICES),
            "rotate": self.rotate(),
            "rotate_choices": list(ROTATE_CHOICES),
            "compression": "zstd" if _ZSTD else "gzip",
            "store": self.store_key(),
            "store_options": storage_options(),
            "store_error": self.store_key() != "internal" and self._dir() is None,
        }

    def list_tracks(self):
        """Newest first. Closed segments come from the index (rebuilt on a
        miss); the active one from in-memory state — nothing decompresses."""
        self.flush_pending()
        d = self._dir()
        if d is None:
            return []
        files = self._stored_files(d)
        idx = self._index_load(d)
        with self._lock:
            active = dict(self._seg) if self._seg else None
        out = []
        for seg_id in sorted(files, reverse=True):
            path = os.path.join(d, files[seg_id])
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if active and seg_id == active["id"]:
                meta = {"points": active["points"],
                        "start_t": active["start_t"], "end_t": active["end_t"]}
                is_active = True
            else:
                meta = idx.get(seg_id)
                if meta is None or "dist_m" not in meta:
                    # Index miss, or an entry written before stats existed:
                    # decompress once, upgrade the entry, never again.
                    blob = _read_stored(path)
                    meta = _full_meta(blob or b"")
                    if meta is None:
                        continue
                    self._index_update(d, seg_id, meta)
                is_active = False
            out.append({"id": seg_id, "points": meta["points"],
                        "bytes": size, "start_t": meta["start_t"],
                        "end_t": meta["end_t"],
                        # Stats land when the segment closes; the active row
                        # shows a dash rather than re-parsing a growing file
                        # on every settings-panel open.
                        "dist_m": meta.get("dist_m"),
                        "max_kmh": meta.get("max_kmh"),
                        "compressed": not files[seg_id].endswith(".bin"),
                        "active": is_active})
        return out

    def _read_records(self, seg_id):
        self.flush_pending()
        path = self._seg_path_for(seg_id)
        if path is None:
            return None
        blob = _read_stored(path)
        if blob is None:
            return None
        return _parse_blob(blob)

    def read_track(self, seg_id, max_points=VIEW_MAX_POINTS):
        """Decoded track for the viewer: compact [t, lat, lon, alt, kmh]
        arrays, stride-decimated to max_points (first/last always kept),
        plus stats computed over the FULL record set."""
        recs = self._read_records(seg_id)
        if not recs:
            return None
        dist = 0.0
        max_spd = 0
        for i in range(1, len(recs)):
            dist += _dist_m(recs[i - 1][1] / 1e7, recs[i - 1][2] / 1e7,
                            recs[i][1] / 1e7, recs[i][2] / 1e7)
            if recs[i][4] is not None and recs[i][4] > max_spd:
                max_spd = recs[i][4]
        total = len(recs)
        if total > max_points > 2:
            stride = (total - 1) / (max_points - 1)
            idxs = sorted({int(round(i * stride)) for i in range(max_points)})
            idxs[-1] = total - 1
            recs = [recs[i] for i in idxs]
        points = [[r[0], round(r[1] / 1e7, 7), round(r[2] / 1e7, 7), r[3],
                   r[4] / 10.0 if r[4] is not None else None]
                  for r in recs]
        return {
            "id": seg_id,
            "points": points,
            "total_points": total,
            "stats": {
                "distance_m": round(dist),
                "duration_s": recs[-1][0] - recs[0][0] if total > 1 else 0,
                "max_kmh": max_spd / 10.0,
                "start_t": recs[0][0],
                "end_t": recs[-1][0],
            },
        }

    def _gpx_gen(self, seg_ids, name):
        """Generator of GPX 1.1 chunks at full resolution: one <trk>, one
        <trkseg> per segment (the gap between segments is real — receiver
        off, reboot — and merging them would draw a straight line across
        it). Chunked so a quarter-million-point segment doesn't become one
        giant string; each segment is read only when reached, so peak memory
        stays one segment regardless of how many a day holds."""
        yield ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<gpx version="1.1" creator="pcat-manager-web" '
               'xmlns="http://www.topografix.com/GPX/1/1">\n'
               f'<trk><name>photonicat {name}</name>\n')
        for seg_id in seg_ids:
            recs = self._read_records(seg_id)
            if not recs:
                continue
            yield '<trkseg>\n'
            buf = []
            for (t, la, lo, alt, spd, course10, hdop10, sats) in recs:
                iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
                # <ele>/<sat>/<hdop> only when the record format carried them
                # (GPX 1.1 puts sat/hdop after time; no standard speed slot).
                mid = "" if alt is None else f"<ele>{alt}</ele>"
                tail = ""
                if sats is not None:
                    tail += f"<sat>{sats}</sat>"
                if hdop10 is not None:
                    tail += f"<hdop>{hdop10 / 10:.1f}</hdop>"
                buf.append(f'<trkpt lat="{la / 1e7:.7f}" lon="{lo / 1e7:.7f}">'
                           f'{mid}<time>{iso}</time>{tail}</trkpt>')
                if len(buf) >= 500:
                    yield "\n".join(buf) + "\n"
                    buf = []
            if buf:
                yield "\n".join(buf) + "\n"
            yield '</trkseg>\n'
        yield '</trk></gpx>\n'

    def export_gpx_iter(self, seg_id):
        """GPX generator for one segment, or None if it doesn't exist."""
        if self._read_records(seg_id) is None:
            return None
        return self._gpx_gen([seg_id], seg_id)

    def day_seg_ids(self, day):
        """Segment ids starting on `day` (YYYYMMDD, UTC), oldest first —
        segment ids are named by start time, so this is a prefix match."""
        if not re.fullmatch(r"\d{8}", day or ""):
            return []
        d = self._dir()
        if d is None:
            return []
        self.flush_pending()
        return sorted(s for s in self._stored_files(d) if s.startswith(day))

    def export_day_gpx_iter(self, day):
        """GPX generator combining every segment of one UTC day into a single
        <trk> (one <trkseg> each), or None when the day has no tracks."""
        seg_ids = self.day_seg_ids(day)
        if not seg_ids:
            return None
        return self._gpx_gen(seg_ids, day)

    def raw_file(self, seg_id):
        """(absolute path, download filename) of the stored segment file —
        served as-is, compressed or not, for efficient backup."""
        self.flush_pending()
        path = self._seg_path_for(seg_id)
        if path is None:
            return None
        return path, os.path.basename(path)

    def delete_track(self, seg_id):
        path = self._seg_path_for(seg_id)
        if path is None:
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        self._index_update(os.path.dirname(path), seg_id, None)
        with self._lock:
            if self._seg and self._seg["id"] == seg_id:
                self._seg = None
        self._brief_cache["t"] = 0.0
        return True

    def clear_all(self):
        removed = 0
        d = self._dir()
        for seg_id in list(self._stored_files()):
            if self.delete_track(seg_id):
                removed += 1
        if d is not None:
            try:
                os.remove(os.path.join(d, INDEX_NAME))
            except OSError:
                pass
        with self._lock:
            self._last = None
            self._last_bearing = None
        return removed


gps_logger = GpsLogger()
