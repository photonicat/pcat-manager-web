# -*- coding: utf-8 -*-
"""Local-first OSM tile store for the /gps map.

Serving order: region tiles the user downloaded (persistent, survives
redeploys) -> the builtin world pack shipped in static/tiles (z0-z4, ~2 MB)
-> live fetch from the first reachable upstream in REMOTE_URLS, which is also
written back into the download store (bounded) so every remote tile is
fetched at most once.

The download store deliberately lives OUTSIDE the app directory: deploys are
`rsync --delete` into /usr/share/pcat-manager-web, which would wipe anything
kept there.
"""

import math
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from pcat_util import pcat_logger

BUILTIN_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__),
                                            "..", "static", "tiles"))
# Internal-flash store (the original location; still searched for reads so
# tiles downloaded before external storage existed keep serving).
INTERNAL_DIR = "/etc/pcat-manager-web/tiles"
# External block devices mounted under /mnt get their own store in this
# subdirectory of the mount point, same pattern as the GPS track logger.
EXT_SUBDIR = "pcat-tiles"

# Where NEW tiles are written. "auto" (the default) prefers the external
# mount with the most free space — offline map packs are exactly the data
# that belongs on a big SD/NVMe/USB disk, not on the router's small eMMC.
STORE_KEY = "osm_tiles_store"       # "auto" | "internal" | "/mnt/<mount>"
STORE_CAP_INTERNAL_MB = 100
STORE_CAP_EXTERNAL_MB = 4096

# More than one upstream because tile.openstreetmap.org is not reachable from
# every uplink this router uses. On a Chinese mobile carrier (the modem, i.e.
# "5G mode") connections to it are RST-injected within ~0.2s -- on both 443
# and 80, so it is host-based, not a TLS problem -- while the same link
# fetches every other host fine. The map therefore went blank whenever egress
# moved to the modem, and only for tiles not already in the local store.
#
# All three serve the same OSM z/x/y scheme in WGS84, so they interchange
# tile-for-tile. That rules out the Chinese tile services (Amap, Tianditu),
# which are reachable but serve GCJ-02 -- their tiles would land the GPS
# marker a few hundred metres off the road it is actually on.
#
# Measured over the modem: .fr ~1s, .jp 3-7s, .org reset. Order is by
# preference, and .org stays first because it is the fastest and the least
# rate-limited everywhere the block does not apply.
REMOTE_URLS = (
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "https://a.tile.openstreetmap.fr/osmfr/{z}/{x}/{y}.png",
    "https://tile.openstreetmap.jp/{z}/{x}/{y}.png",
)
# OSM's usage policy requires an identifying User-Agent.
USER_AGENT = "pcat-manager-web/1.0 (photonicat router; +https://photonicat.com)"

MAX_Z = 19
# One region download at a time, at most this many tiles per request.
REGION_TILE_CAP = 2500
FETCH_PACING_S = 0.12
# Per attempt, not per tile. Two attempts cap a tile at 2x this, which is the
# 12s a single attempt used to be allowed on its own.
FETCH_TIMEOUT_S = 6
# At most this many upstreams tried for one tile. Without it, a view full of
# missing tiles on a link where every upstream is dead would pay
# len(REMOTE_URLS) x FETCH_TIMEOUT_S each, on a granian request thread.
FETCH_ATTEMPTS = 2
# How long a failed upstream is skipped. This is what keeps the modem case
# cheap: the first tile discovers .org is blocked, and every tile after it
# goes straight to the mirror instead of re-paying the reset. Short enough
# that moving back to a normal uplink recovers on its own.
SOURCE_COOLDOWN_S = 300

# Region downloads go through aria2c when the firmware ships it: two parallel
# downloads (the "2 threads" OSM's tile policy allows), each tile line naming
# every upstream as a mirror so a blocked .org still falls through to .fr/.jp
# per file. The single-connection Python loop below stays as the fallback for
# builds without aria2c.
ARIA2C = shutil.which("aria2c")
ARIA2_JOBS = 2

_dl_lock = threading.Lock()
_dl_state = {"running": False, "total": 0, "done": 0, "failed": 0,
             # progress detail for the UI: bytes fetched so far, smoothed
             # rate, remaining-time estimate and projected total size
             # (avg tile size so far x total tiles).
             "bytes": 0, "rate_bps": 0, "eta_s": None, "est_bytes": None,
             "engine": ""}


def _tile_rel(z, x, y):
    return os.path.join(str(z), str(x), f"{y}.png")


def storage_options():
    """Places tiles can live: internal flash + /mnt block devices. Reuses the
    GPS track logger's mount scan (same shape: key, kind, free). Lazy import
    keeps plain `import osm_tiles` free of the logger's dependency tree."""
    try:
        import pc_gps_logger
        return pc_gps_logger.storage_options()
    except Exception:
        return [{"key": "internal", "kind": "internal", "free": 0}]


def _store_pref():
    try:
        import pcat_config
        return pcat_config.get_user_config_key(STORE_KEY, "auto") or "auto"
    except Exception:
        return "auto"


def set_store(key):
    """Persist the write-store choice ('auto', 'internal', or a mount path
    present in storage_options()). Returns True when accepted."""
    if key not in ("auto", "internal") and \
            key not in {o["key"] for o in storage_options()}:
        return False
    import pcat_config
    pcat_config.set_user_config_key(STORE_KEY, key)
    _active_cache["t"] = 0.0
    return True


# Tile serving is hot (a map pan asks for dozens of tiles); the mount scan
# behind active_store() must not run per tile.
_active_cache = {"t": 0.0, "value": None}


def active_store(refresh=False):
    """{dir, key, kind, cap_mb} of the store new tiles are written to.
    'auto' picks the external mount with the most free space, falling back
    to internal flash when nothing is mounted — and a configured mount that
    has since been unplugged falls back the same way instead of erroring."""
    now = time.monotonic()
    if not refresh and _active_cache["value"] is not None \
            and now - _active_cache["t"] < 10:
        return _active_cache["value"]
    pref = _store_pref()
    ext = [o for o in storage_options() if o["kind"] != "internal"]
    choice = None
    if pref not in ("auto", "internal"):
        choice = next((o for o in ext if o["key"] == pref), None)
    if choice is None and pref != "internal" and ext:
        choice = max(ext, key=lambda o: o["free"])
    if choice is not None:
        value = {"dir": os.path.join(choice["key"], EXT_SUBDIR),
                 "key": choice["key"], "kind": choice["kind"],
                 "cap_mb": STORE_CAP_EXTERNAL_MB}
    else:
        value = {"dir": INTERNAL_DIR, "key": "internal", "kind": "internal",
                 "cap_mb": STORE_CAP_INTERNAL_MB}
    _active_cache.update(t=now, value=value)
    return value


def _active_dir():
    return active_store()["dir"]


def local_tile_path(z, x, y):
    """Path of a locally available tile, or None. Reads search every store
    (active, internal, builtin) so switching the write store never makes
    already-downloaded tiles disappear from the map."""
    rel = _tile_rel(z, x, y)
    bases = []
    for base in (_active_dir(), INTERNAL_DIR, BUILTIN_DIR):
        if base not in bases:
            bases.append(base)
    for base in bases:
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            return p
    return None


_store_cache = {"t": 0.0, "bytes": 0, "dir": ""}


def store_size_mb():
    """Active-store size; walked at most every 20 s (this is polled per
    status request and checked per tile fetch), kept fresh incrementally by
    _save_tile in between."""
    now = time.time()
    d = _active_dir()
    if d != _store_cache["dir"] or now - _store_cache["t"] > 20:
        total = 0
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        _store_cache.update(t=now, bytes=total, dir=d)
    return round(_store_cache["bytes"] / (1024 * 1024), 1)


def _save_tile(z, x, y, data):
    p = os.path.join(_active_dir(), _tile_rel(z, x, y))
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        _store_cache["bytes"] += len(data)
        return True
    except OSError as e:
        pcat_logger.logger.warning(f"osm_tiles: cannot save {p}: {e}")
        return False


# url -> monotonic deadline until which that upstream is skipped.
_source_lock = threading.Lock()
_source_penalty = {}


def _live_sources():
    """REMOTE_URLS minus the ones still in cooldown, preference order kept.
    Falls back to the full list rather than returning nothing, so a spell
    where every upstream has failed still retries instead of hard-failing
    until the cooldowns lapse."""
    now = time.monotonic()
    with _source_lock:
        live = [u for u in REMOTE_URLS if _source_penalty.get(u, 0.0) <= now]
    return live or list(REMOTE_URLS)


def _penalise(url, reason):
    with _source_lock:
        first = _source_penalty.get(url, 0.0) <= time.monotonic()
        _source_penalty[url] = time.monotonic() + SOURCE_COOLDOWN_S
    # Only the transition is worth a log line; the tiles behind it would
    # otherwise write one entry per tile.
    if first:
        pcat_logger.logger.warning(
            f"osm_tiles: {url.split('/')[2]} unusable ({reason}), "
            f"skipping it for {SOURCE_COOLDOWN_S}s")


def _fetch_one(url, z, x, y):
    """One upstream, one tile. Returns bytes or None.

    Only failures that say something about the *upstream* put it in cooldown.
    A 404 is the ordinary answer for a tile outside a source's coverage (the
    .fr and .jp mirrors render less of the world at high zoom than .org
    does); blacklisting a healthy mirror over one such tile would push every
    later tile onto a slower source for five minutes."""
    req = urllib.request.Request(url.format(z=z, x=x, y=y),
                                 headers={"User-Agent": USER_AGENT})
    try:
        data = urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S).read()
    except urllib.error.HTTPError as e:
        # 403/429 are the upstream refusing *us* (blocked UA, rate limit), so
        # they do condemn the source. Anything else is about this one tile.
        if e.code in (403, 429):
            _penalise(url, f"HTTP {e.code}")
        return None
    except Exception as e:
        # Reset, timeout, DNS, no route: the path to this host is broken, and
        # it will be just as broken for the next tile.
        _penalise(url, f"{type(e).__name__}: {e}")
        return None
    if data[:4] != b"\x89PNG":
        # 200 but not an image: an error page or a captive-portal splash. The
        # source is wrong for us, not the tile.
        _penalise(url, "response is not a PNG")
        return None
    return data


def fetch_remote(z, x, y, write_back=True):
    """Fetch one tile, trying upstreams in preference order. Returns bytes or
    None."""
    for url in _live_sources()[:FETCH_ATTEMPTS]:
        data = _fetch_one(url, z, x, y)
        if data is None:
            continue
        if write_back and store_size_mb() < active_store()["cap_mb"]:
            _save_tile(z, x, y, data)
        return data
    pcat_logger.logger.debug(f"osm_tiles: remote {z}/{x}/{y} failed on every "
                             f"upstream tried")
    return None


def deg2tile(lat, lon, z):
    lat = max(-85.0511, min(85.0511, lat))
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def region_tiles(min_lat, min_lon, max_lat, max_lon, min_z, max_z):
    """All (z, x, y) covering the bbox for the zoom range."""
    out = []
    for z in range(min_z, max_z + 1):
        x0, y1 = deg2tile(min_lat, min_lon, z)   # south-west: max y
        x1, y0 = deg2tile(max_lat, max_lon, z)   # north-east: min y
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                out.append((z, x, y))
    return out


# ── country packs ─────────────────────────────────────────────────────────────
# Curated bounding boxes (min_lat, min_lon, max_lat, max_lon), deliberately a
# little generous. A pack downloads z0..max_z for the box, where max_z is the
# deepest zoom whose cumulative tile count fits COUNTRY_TILE_CAP — big
# countries land at z9-z10, small ones reach z11-z12. Deeper zooms keep
# filling on demand through the live write-back cache as the map is used, so
# the pack is the base layer, not the whole world in detail.
COUNTRY_TILE_CAP = 20000
COUNTRY_MAX_Z = 12
COUNTRIES = (
    ("th", "Thailand", "泰国", (5.6, 97.3, 20.5, 105.7)),
    ("cn", "China", "中国", (18.0, 73.5, 53.6, 135.1)),
    ("jp", "Japan", "日本", (24.0, 122.9, 45.6, 146.0)),
    ("kr", "South Korea", "韩国", (33.1, 124.6, 38.7, 129.6)),
    ("tw", "Taiwan", "台湾", (21.8, 119.3, 25.4, 122.1)),
    ("hk", "Hong Kong & Macau", "港澳", (22.1, 113.5, 22.6, 114.5)),
    ("sg", "Singapore", "新加坡", (1.1, 103.6, 1.5, 104.1)),
    ("my", "Malaysia", "马来西亚", (0.8, 99.6, 7.4, 119.3)),
    ("vn", "Vietnam", "越南", (8.4, 102.1, 23.4, 109.5)),
    ("la", "Laos", "老挝", (13.9, 100.1, 22.5, 107.7)),
    ("kh", "Cambodia", "柬埔寨", (10.4, 102.3, 14.7, 107.6)),
    ("mm", "Myanmar", "缅甸", (9.8, 92.2, 28.5, 101.2)),
    ("ph", "Philippines", "菲律宾", (4.6, 116.9, 21.1, 126.6)),
    ("id", "Indonesia", "印度尼西亚", (-11.0, 95.0, 6.1, 141.0)),
    ("in", "India", "印度", (6.7, 68.1, 35.5, 97.4)),
    ("np", "Nepal", "尼泊尔", (26.3, 80.0, 30.4, 88.2)),
    ("ae", "UAE", "阿联酋", (22.6, 51.5, 26.1, 56.4)),
    ("tr", "Turkey", "土耳其", (35.8, 26.0, 42.1, 44.8)),
    ("kz", "Kazakhstan", "哈萨克斯坦", (40.6, 46.5, 55.4, 87.3)),
    ("ru", "Russia", "俄罗斯", (41.2, 27.3, 77.7, 180.0)),
    ("de", "Germany", "德国", (47.3, 5.9, 55.1, 15.0)),
    ("fr", "France", "法国", (41.3, -5.1, 51.1, 9.6)),
    ("gb", "United Kingdom", "英国", (49.9, -8.6, 60.9, 1.8)),
    ("it", "Italy", "意大利", (36.6, 6.6, 47.1, 18.5)),
    ("es", "Spain", "西班牙", (36.0, -9.3, 43.8, 3.3)),
    ("pt", "Portugal", "葡萄牙", (36.9, -9.5, 42.2, -6.2)),
    ("nl", "Netherlands", "荷兰", (50.8, 3.4, 53.6, 7.2)),
    ("be", "Belgium", "比利时", (49.5, 2.5, 51.5, 6.4)),
    ("ch", "Switzerland", "瑞士", (45.8, 5.9, 47.8, 10.5)),
    ("at", "Austria", "奥地利", (46.4, 9.5, 49.0, 17.2)),
    ("pl", "Poland", "波兰", (49.0, 14.1, 54.8, 24.2)),
    ("cz", "Czechia", "捷克", (48.5, 12.1, 51.1, 18.9)),
    ("se", "Sweden", "瑞典", (55.3, 11.1, 69.1, 24.2)),
    ("no", "Norway", "挪威", (57.9, 4.6, 71.2, 31.1)),
    ("fi", "Finland", "芬兰", (59.8, 20.5, 70.1, 31.6)),
    ("dk", "Denmark", "丹麦", (54.5, 8.0, 57.8, 12.7)),
    ("gr", "Greece", "希腊", (34.8, 19.4, 41.8, 28.3)),
    ("us", "United States", "美国", (24.4, -125.0, 49.4, -66.9)),
    ("ca", "Canada", "加拿大", (41.7, -141.0, 70.0, -52.6)),
    ("mx", "Mexico", "墨西哥", (14.5, -117.1, 32.7, -86.7)),
    ("br", "Brazil", "巴西", (-33.8, -73.9, 5.3, -34.8)),
    ("ar", "Argentina", "阿根廷", (-55.1, -73.6, -21.8, -53.6)),
    ("au", "Australia", "澳大利亚", (-43.7, 112.9, -10.6, 153.7)),
    ("nz", "New Zealand", "新西兰", (-47.3, 166.4, -34.3, 178.6)),
    ("eg", "Egypt", "埃及", (22.0, 24.7, 31.7, 36.9)),
    ("za", "South Africa", "南非", (-34.9, 16.4, -22.1, 32.9)),
)
# Rough average tile size (measured on this store) for the size estimate.
AVG_TILE_BYTES = 15000


def _bbox_tile_count(bbox, z):
    """Tile count of one zoom level over a bbox — arithmetic, no lists."""
    min_lat, min_lon, max_lat, max_lon = bbox
    x0, y1 = deg2tile(min_lat, min_lon, z)
    x1, y0 = deg2tile(max_lat, max_lon, z)
    return (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)


def country_plan(code):
    """(bbox, max_z, tiles) for a country pack: the deepest max_z whose
    cumulative z0..max_z count fits COUNTRY_TILE_CAP. None for unknown."""
    entry = next((c for c in COUNTRIES if c[0] == code), None)
    if entry is None:
        return None
    bbox = entry[3]
    total = 0
    max_z = 0
    for z in range(COUNTRY_MAX_Z + 1):
        n = _bbox_tile_count(bbox, z)
        if z > 0 and total + n > COUNTRY_TILE_CAP:
            break
        total += n
        max_z = z
    return bbox, max_z, total


def country_catalog():
    """UI list: every country with its plan and a size estimate."""
    out = []
    for code, en, cn, _bbox in COUNTRIES:
        bbox, max_z, tiles = country_plan(code)
        out.append({"code": code, "en": en, "cn": cn, "max_z": max_z,
                    "tiles": tiles,
                    "est_mb": round(tiles * AVG_TILE_BYTES / 1048576, 1)})
    return out


def start_country_download(code):
    """Kick a background country-pack download. Returns (ok, message_or_total)."""
    plan = country_plan(code)
    if plan is None:
        return False, "Unknown country."
    bbox, max_z, tiles = plan
    store = active_store(refresh=True)
    try:
        st = os.statvfs(os.path.dirname(store["dir"]) or "/")
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = 0
    # Refuse a pack the disk can't hold, with a third margin for overhead.
    if free and tiles * AVG_TILE_BYTES * 1.3 > free:
        return False, "Not enough free space on {}.".format(store["key"])
    min_lat, min_lon, max_lat, max_lon = bbox
    return start_region_download(min_lat, min_lon, max_lat, max_lon,
                                 0, max_z, tile_cap=COUNTRY_TILE_CAP)


def download_status():
    with _dl_lock:
        st = dict(_dl_state)
    st["store_mb"] = store_size_mb()
    store = active_store()
    st["store_key"] = store["key"]
    st["store_kind"] = store["kind"]
    st["store_cap_mb"] = store["cap_mb"]
    return st


def start_region_download(min_lat, min_lon, max_lat, max_lon, min_z, max_z,
                          tile_cap=REGION_TILE_CAP):
    """Kick a background download. Returns (ok, message_or_total)."""
    min_z = max(0, min(int(min_z), MAX_Z))
    max_z = max(min_z, min(int(max_z), MAX_Z))
    tiles = region_tiles(min_lat, min_lon, max_lat, max_lon, min_z, max_z)
    tiles = [t for t in tiles if local_tile_path(*t) is None]
    if len(tiles) > tile_cap:
        return False, f"Region too large: {len(tiles)} tiles (max {tile_cap}). Zoom in or reduce depth."
    cap = active_store(refresh=True)["cap_mb"]
    if store_size_mb() >= cap:
        return False, f"Offline store is full ({cap} MB cap)."
    with _dl_lock:
        if _dl_state["running"]:
            return False, "A download is already running."
        _dl_state.update({"running": True, "total": len(tiles),
                          "done": 0, "failed": 0, "bytes": 0, "rate_bps": 0,
                          "eta_s": None, "est_bytes": None,
                          "engine": "aria2c" if ARIA2C else "python"})
    threading.Thread(target=_download_worker, args=(tiles,),
                     name="osm-tile-dl", daemon=True).start()
    return True, len(tiles)


def _update_progress(done, size, rate_bps, total):
    """Fold one progress sample into the shared state (caller computes the
    smoothed rate). ETA extrapolates the remaining tiles at the average size
    seen so far — honest once a handful of tiles are in."""
    est = round(size / done * total) if done else None
    eta = None
    if rate_bps > 0 and done and total > done:
        eta = round((total - done) * (size / done) / rate_bps)
    with _dl_lock:
        _dl_state.update({"done": done, "bytes": size,
                          "rate_bps": round(rate_bps), "eta_s": eta,
                          "est_bytes": est})


def _aria2_input(tiles):
    """aria2 input-file text: one line per tile listing every upstream as a
    mirror URI (tab-separated -> tried in order per file), then the target
    dir/out options."""
    sources = _live_sources()
    lines = []
    for (z, x, y) in tiles:
        lines.append("\t".join(u.format(z=z, x=x, y=y) for u in sources))
        lines.append("  dir=" + os.path.join(_active_dir(), str(z), str(x)))
        lines.append("  out={}.png".format(y))
    return "\n".join(lines) + "\n"


def _scan_tiles(tiles):
    """(complete, bytes) of the tiles already fully on disk. An .aria2
    control file next to a tile means it is still in flight."""
    done = 0
    size = 0
    for t in tiles:
        p = os.path.join(_active_dir(), _tile_rel(*t))
        try:
            st = os.stat(p)
        except OSError:
            continue
        if os.path.exists(p + ".aria2"):
            continue
        done += 1
        size += st.st_size
    return done, size


def _download_via_aria2(tiles):
    """Run the region through aria2c, tracking progress off the filesystem
    (simpler and more robust than parsing aria2's console output)."""
    os.makedirs(_active_dir(), exist_ok=True)
    listpath = os.path.join(_active_dir(), ".aria2-input")
    with open(listpath, "w") as f:
        f.write(_aria2_input(tiles))
    cmd = [ARIA2C, "-i", listpath, "-j", str(ARIA2_JOBS),
           "-x", str(ARIA2_JOBS), "-s", "1",
           "--auto-file-renaming=false", "--allow-overwrite=true",
           "--connect-timeout=6", "--timeout=15",
           "--max-tries=2", "--retry-wait=1",
           "--user-agent=" + USER_AGENT,
           "--summary-interval=0", "--quiet=true"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        last_size = 0
        last_t = time.monotonic()
        rate = 0.0
        while proc.poll() is None:
            time.sleep(1)
            done, size = _scan_tiles(tiles)
            now = time.monotonic()
            inst = (size - last_size) / max(now - last_t, 0.1)
            # EWMA so the readout doesn't jitter tile-by-tile.
            rate = inst if rate == 0 else 0.7 * rate + 0.3 * inst
            last_size, last_t = size, now
            _update_progress(done, size, rate, len(tiles))
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            os.remove(listpath)
        except OSError:
            pass
    # Post-pass: aria2 can't judge content. A 200 that isn't a PNG (error
    # page, captive-portal splash) must not poison the store.
    done = 0
    size = 0
    for t in tiles:
        p = os.path.join(_active_dir(), _tile_rel(*t))
        try:
            with open(p, "rb") as f:
                magic = f.read(4)
        except OSError:
            continue
        if magic != b"\x89PNG":
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        done += 1
        size += os.path.getsize(p)
    for t in tiles:   # stray control files from aborted transfers
        try:
            os.remove(os.path.join(_active_dir(), _tile_rel(*t)) + ".aria2")
        except OSError:
            pass
    _store_cache["t"] = 0.0   # force a real recount
    with _dl_lock:
        _dl_state.update({"done": len(tiles), "bytes": size,
                          "failed": len(tiles) - done,
                          "eta_s": None})


def _download_via_python(tiles):
    """Single-connection fallback for firmwares without aria2c: the original
    politely-paced loop, now also feeding the progress fields."""
    t0 = time.monotonic()
    size = 0
    for i, (z, x, y) in enumerate(tiles, 1):
        data = fetch_remote(z, x, y, write_back=True)
        with _dl_lock:
            if data is None:
                _dl_state["failed"] += 1
        size += len(data or b"")
        rate = size / max(time.monotonic() - t0, 0.1)
        _update_progress(i, size, rate, len(tiles))
        time.sleep(FETCH_PACING_S)


def _download_worker(tiles):
    try:
        if ARIA2C:
            _download_via_aria2(tiles)
        else:
            _download_via_python(tiles)
    except Exception as e:
        pcat_logger.logger.warning(f"osm_tiles: region download crashed: {e}")
    finally:
        with _dl_lock:
            _dl_state["running"] = False
        pcat_logger.logger.info(
            f"osm_tiles: region download finished ({_dl_state['engine']}), "
            f"{_dl_state['done']} tiles, {_dl_state['failed']} failed, "
            f"store {store_size_mb()} MB")
