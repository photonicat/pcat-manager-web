# -*- coding: utf-8 -*-
"""Serving-cell identification and tower lookup for the /gps map.

The modem always knows exactly which cell it is camped on — QENG reports the
full global identity (MCC/MNC/TAC/CID) even SIM-less in limited service.
Turning that identity into map coordinates needs a tower database:

  * BeaconDB (api.beacondb.net, keyless, MLS-compatible) is tried first. Its
    geolocate endpoint falls back to IP geolocation when it doesn't know the
    cell; those responses carry "fallback" and are treated as a miss — a
    25 km IP guess drawn as a "tower" would be a lie.
  * OpenCelliD is tried when the user configured a (free) API key; its
    coverage is broader in many regions.

Both are crowd-sourced: dense in EU/NA, thin for e.g. Thai 3G (verified on
hardware — the camped dtac WCDMA and TrueMove LTE cells resolved in
neither). The UI therefore always shows the cell identity and only draws a
tower when a database actually knows it.

AT traffic goes through pc_modem_client's shared port, injected the same way
as pc_gps_client's sender.
"""

import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from pcat_util import pcat_logger

BEACONDB_URL = "https://api.beacondb.net/v1/geolocate"
OPENCELLID_URL = ("https://opencellid.org/cell/get?key={key}&mcc={mcc}"
                  "&mnc={mnc}&lac={tac}&cellid={cid}&format=json")
LOOKUP_TIMEOUT_S = 12
# A tower doesn't move: positive lookups cache for the process lifetime.
# Misses retry after this long, so a DB outage doesn't wedge the button.
MISS_TTL_S = 300

OPENCELLID_KEY_CONFIG = "opencellid_key"

# Offline tower database (SQLite built by misc/build_celldb.py from the
# public-domain Mozilla Location Service final cell export, or any CSV in
# that schema). Searched on external mounts first — the same disks that hold
# the offline map packs — then internal flash. When present, lookups are
# instant, keyless and work with no internet at all.
CELLDB_SUBPATH = os.path.join("pcat-celldb", "cells.sqlite")
CELLDB_INTERNAL = "/etc/pcat-manager-web/celldb.sqlite"
# MLS radio names vs QENG tech names.
_RADIO_TO_DB = {"WCDMA": "UMTS", "LTE": "LTE", "GSM": "GSM"}


def celldb_path():
    """First existing offline DB, external mounts before internal, or None."""
    candidates = []
    try:
        for m in sorted(os.listdir("/mnt")):
            candidates.append(os.path.join("/mnt", m, CELLDB_SUBPATH))
    except OSError:
        pass
    candidates.append(CELLDB_INTERNAL)
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def nearby_towers(lat, lon, radius_m=3000, limit=300):
    """Every known tower within ~radius_m of (lat, lon) from the offline DB,
    most-surveyed first. Returns a list, or None when no DB is installed.
    Bounding-box query on the (lat, lon) index — plenty fast for the ≤300
    rows a map overlay can usefully show."""
    path = celldb_path()
    if path is None:
        return None
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        try:
            rows = con.execute(
                "SELECT radio, net, area, cell, unit, lat, lon, range,"
                " samples FROM cells WHERE lat BETWEEN ? AND ?"
                " AND lon BETWEEN ? AND ? ORDER BY samples DESC LIMIT ?",
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon,
                 int(limit))).fetchall()
        finally:
            con.close()
    except Exception as e:
        pcat_logger.logger.warning("cell_locate: nearby query: %s", e)
        return None
    return [{"radio": r[0], "net": r[1], "area": r[2], "cell": r[3],
             "unit": r[4], "lat": r[5], "lon": r[6],
             "range": r[7], "samples": r[8]} for r in rows]


# ── per-country DB download from photonicat's server ─────────────────────────
# Country slices of the public-domain MLS export, split by misc/
# mls_split_celldb.py and hosted on the photonicat download server.
CELLDB_URL = "https://dl.photonicat.com/celldb/{mcc}.sqlite"

_dl_lock = threading.Lock()
_dl = {"running": False, "mcc": None, "bytes": 0, "total": None,
       "error": None}


def _celldb_target():
    """Where a downloaded DB lands: the same store the offline map packs
    prefer (external disk when present, internal flash otherwise)."""
    from pcat_util import osm_tiles
    store = osm_tiles.active_store()
    if store["key"] == "internal":
        return CELLDB_INTERNAL
    return os.path.join(store["key"], CELLDB_SUBPATH)


def celldb_status():
    """Presence + download progress for the UI."""
    path = celldb_path()
    size = None
    if path:
        try:
            size = os.path.getsize(path)
        except OSError:
            path = None
    with _dl_lock:
        st = dict(_dl)
    st.update(path=path, size=size)
    return st


def start_celldb_download(mcc):
    """Fetch the country DB for `mcc` in the background. (ok, message)."""
    try:
        mcc = int(mcc)
    except (TypeError, ValueError):
        return False, "bad mcc"
    with _dl_lock:
        if _dl["running"]:
            return False, "A download is already running."
        _dl.update(running=True, mcc=mcc, bytes=0, total=None, error=None)
    threading.Thread(target=_celldb_worker, args=(mcc,),
                     name="celldb-dl", daemon=True).start()
    return True, "started"


def _celldb_worker(mcc):
    target = _celldb_target()
    tmp = target + ".tmp"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        req = urllib.request.Request(CELLDB_URL.format(mcc=mcc),
                                     headers={"User-Agent": "pcat-manager-web"})
        src = urllib.request.urlopen(req, timeout=30)
        total = src.headers.get("Content-Length")
        with _dl_lock:
            _dl["total"] = int(total) if total else None
        got = 0
        with open(tmp, "wb") as out:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                with _dl_lock:
                    _dl["bytes"] = got
        # Sanity: must be a SQLite file, not an error page.
        with open(tmp, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                raise ValueError("not a SQLite database")
        os.replace(tmp, target)
        pcat_logger.logger.info(
            "cell_locate: celldb mcc=%s installed (%d bytes) at %s",
            mcc, got, target)
        with _cache_lock:
            _cache.clear()   # cells unresolvable before may resolve now
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        with _dl_lock:
            _dl["error"] = str(e)
        pcat_logger.logger.warning("cell_locate: celldb download failed: %s", e)
    finally:
        with _dl_lock:
            _dl["running"] = False


def _query_local_db(cell):
    path = celldb_path()
    radio = _RADIO_TO_DB.get(cell["tech"])
    if path is None or radio is None:   # NR postdates the MLS-era data
        return None
    sibling = False
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        try:
            rows = con.execute(
                "SELECT area, lat, lon, range, samples FROM cells "
                "WHERE radio=? AND mcc=? AND net=? AND cell=?",
                (radio, cell["mcc"], cell["mnc"], cell["cid"])).fetchall()
            if not rows and radio == "LTE":
                # An LTE cell id is eNodeB*256 + sector, and sister sectors
                # share the mast — a sector added after the survey still
                # lands on the right tower via its siblings.
                enb = cell["cid"] // 256
                rows = con.execute(
                    "SELECT area, lat, lon, range, samples FROM cells "
                    "WHERE radio=? AND mcc=? AND net=? AND cell BETWEEN ? AND ?",
                    (radio, cell["mcc"], cell["mnc"],
                     enb * 256, enb * 256 + 255)).fetchall()
                sibling = bool(rows)
        finally:
            con.close()
    except Exception as e:
        pcat_logger.logger.warning("cell_locate: offline db: %s", e)
        return None
    if not rows:
        return None
    # Prefer the row whose area matches too; TACs get renumbered over the
    # years while the cell id itself is stable, so a cell-only match still
    # names the right tower.
    exact = [r for r in rows if r[0] == cell["tac"]]
    row = max(exact or rows, key=lambda r: r[4] or 0)   # most-sampled wins
    return {"lat": row[1], "lon": row[2],
            "accuracy": int(row[3] or 1000),
            "source": "offline-db-enb" if sibling else "offline-db"}

_at_sender = None


def set_at_sender(fn):
    """`fn(cmd, timeout) -> str | None` on the modem's shared AT port."""
    global _at_sender
    _at_sender = fn


def _hex(v):
    try:
        return int(v, 16)
    except (TypeError, ValueError):
        return None


def parse_serving(resp):
    """Parse +QENG: "servingcell" for LTE / WCDMA / NR5G into
    {state, tech, mcc, mnc, tac, cid, rsrp, band...}, or None when the modem
    is still searching / answered garbage. Field orders per the RM520N AT
    manual, verified against live output."""
    line = next((l for l in (resp or "").splitlines()
                 if "+QENG" in l and "servingcell" in l), "")
    if not line:
        return None
    f = [p.strip().strip('"') for p in line.split(":", 1)[1].split(",")]
    if len(f) < 3:
        return None
    cell = {"state": f[1], "tech": f[2], "mcc": None, "mnc": None,
            "tac": None, "cid": None, "rsrp": None, "band": ""}
    try:
        if f[2] == "LTE" and len(f) >= 14:
            # "LTE",<fdd/tdd>,<mcc>,<mnc>,<cid hex>,<pci>,<earfcn>,<band>,
            # <ulbw>,<dlbw>,<tac hex>,<rsrp>,...
            cell.update(mcc=int(f[4]), mnc=int(f[5]), cid=_hex(f[6]),
                        band="B" + f[9], tac=_hex(f[12]), rsrp=int(f[13]))
        elif f[2] == "WCDMA" and len(f) >= 11:
            # "WCDMA",<mcc>,<mnc>,<lac hex>,<cid hex>,<uarfcn>,<psc>,<rac>,
            # <rscp>,...
            cell.update(mcc=int(f[3]), mnc=int(f[4]), tac=_hex(f[5]),
                        cid=_hex(f[6]), rsrp=int(f[10]))
        elif f[2].startswith("NR5G") and len(f) >= 13:
            # "NR5G-SA",<duplex>,<mcc>,<mnc>,<cid hex>,<pci>,<tac hex>,
            # <arfcn>,<band>,<dlbw>,<rsrp>,...
            cell.update(mcc=int(f[4]), mnc=int(f[5]), cid=_hex(f[6]),
                        tac=_hex(f[8]), band="n" + f[10], rsrp=int(f[12]))
        elif f[1] == "SEARCH":
            return None
    except (ValueError, IndexError):
        return None
    return cell


def serving_cell():
    """Live serving cell from the modem, or None."""
    if _at_sender is None:
        return None
    try:
        resp = _at_sender('AT+QENG="servingcell"', 6.0)
    except Exception as e:
        pcat_logger.logger.warning("cell_locate: QENG failed: %s", e)
        return None
    return parse_serving(resp or "")


def _radio_type(tech):
    if tech.startswith("NR5G"):
        return "nr"
    return {"LTE": "lte", "WCDMA": "wcdma", "GSM": "gsm"}.get(tech, "lte")


def _query_beacondb(cell):
    body = json.dumps({"cellTowers": [{
        "radioType": _radio_type(cell["tech"]),
        "mobileCountryCode": cell["mcc"],
        "mobileNetworkCode": cell["mnc"],
        "locationAreaCode": cell["tac"],
        "cellId": cell["cid"]}]}).encode()
    req = urllib.request.Request(
        BEACONDB_URL, data=body,
        headers={"Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=LOOKUP_TIMEOUT_S).read())
    if "fallback" in r or "location" not in r:
        return None   # IP-based guess, not a cell hit
    return {"lat": r["location"]["lat"], "lon": r["location"]["lng"],
            "accuracy": round(r.get("accuracy", 1000)), "source": "beacondb"}


def _query_opencellid(cell, key):
    url = OPENCELLID_URL.format(key=urllib.parse.quote(key),
                                mcc=cell["mcc"], mnc=cell["mnc"],
                                tac=cell["tac"], cid=cell["cid"])
    r = json.loads(urllib.request.urlopen(url, timeout=LOOKUP_TIMEOUT_S).read())
    if r.get("error") or "lat" not in r:
        return None
    return {"lat": float(r["lat"]), "lon": float(r["lon"]),
            "accuracy": round(float(r.get("range", 1000))),
            "source": "opencellid"}


_cache_lock = threading.Lock()
_cache = {}   # (mcc,mnc,tac,cid) -> {"tower": ... | None, "t": monotonic}


def resolve(cell, key=""):
    """Tower {lat, lon, accuracy, source} for a parsed cell, or None. Cached:
    hits forever, misses for MISS_TTL_S."""
    if not cell or None in (cell.get("mcc"), cell.get("mnc"),
                            cell.get("tac"), cell.get("cid")):
        return None
    ck = (cell["mcc"], cell["mnc"], cell["tac"], cell["cid"])
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(ck)
        if hit and (hit["tower"] is not None or now - hit["t"] < MISS_TTL_S):
            return hit["tower"]
    tower = None
    # Offline DB first (instant, keyless, works with no internet), then the
    # web services.
    for attempt in (lambda: _query_local_db(cell),
                    (lambda: _query_opencellid(cell, key)) if key else None,
                    lambda: _query_beacondb(cell)):
        if attempt is None:
            continue
        try:
            tower = attempt()
        except Exception as e:
            pcat_logger.logger.warning("cell_locate: lookup failed: %s", e)
            tower = None
        if tower is not None:
            break
    with _cache_lock:
        _cache[ck] = {"tower": tower, "t": now}
    return tower
