# -*- coding: utf-8 -*-
"""GNSS client for Quectel modems (EM05 / RM500Q / EM520 class).

AT traffic goes through pc_modem_client's port — the app keeps ONE serial
port for every AT conversation, injected via set_at_sender(). This module
used to open ttyUSB2 itself, which cost us twice on real hardware: a second
hardcoded tty index to break when a module reset re-enumerated USB (ttyUSB3
became ttyUSB4 and the app went blind), and two writers whose exclusive
opens raced, surfacing in the UI as "Could not exclusively lock port ...
Resource temporarily unavailable" when a setting refused to save.

The one port we still open directly is the NMEA output (ttyUSB1 by default):
it is a continuous read-only stream, not an AT conversation, so it cannot
share a command/response port. Its absence is not fatal — the reader reports
the error and the page shows the receiver as producing nothing.

Do NOT use this on RM500U boxes: there quectel-cm dials over AT and owns the
AT port, and interleaving our commands with the dialer's corrupts both
conversations. The web layer gates power_on() on the modem model.

The NMEA reader is a daemon thread that parses GGA/RMC/GSA/GSV/VTG into a
state dict guarded by a lock; get_status() returns a snapshot for the API.
"""

import datetime
import math
import re
import threading
import time
import urllib.request
from collections import deque

import serial

from pcat_util import pcat_logger

# How long the NMEA stream may be silent while we believe GNSS is on
# before the session is treated as dead and re-armed. Comfortably longer
# than a cold start's first sentence (which arrives within a second or
# two of AT+QGPS=1, well before any fix).
NMEA_STALL_SECS = 30

GPS_NMEA_PORT = "/dev/ttyUSB1"

# User-config key remembering that the user left the receiver on, so a reboot
# (or web app restart) brings it back without a visit to the page.
GPS_POWER_KEY = "gps_power"

# NMEA talker prefix -> constellation name shown in the UI. GN* (multi-GNSS
# combined solutions) keeps position/DOP sentences but has no satellites of
# its own.
TALKER_GNSS = {
    "GP": "GPS",
    "GL": "GLONASS",
    "GA": "Galileo",
    "GB": "BeiDou",
    "BD": "BeiDou",
    "GQ": "QZSS",
    "QZ": "QZSS",
    "GI": "NavIC",
}

# Estimated User Range Error in meters; accuracy ~= HDOP * UERE. The usual
# rule-of-thumb figure for a consumer receiver — labelled "estimated" in the UI.
UERE_M = 5.0

# ---- time sync ----
# How long after the whole-second epoch an RMC naming that second finishes
# arriving at our reader. Measured on the RM520N-GL against NTP (2026-08,
# stationary 3D fix, n=25): median +41 ms, spread 35-42 ms — the modem emits
# its NMEA burst nearly on the second, NOT the half-second folklore figure
# (that perception is the UI's poll interval plus whole-second display).
# Added to the reported time when setting the system clock from GNSS.
GPS_TIME_LATENCY_S = 0.04
# A time reference older than this is refused for a clock set: the extrapolation
# t_ref + elapsed-monotonic stays honest over seconds, not over a stall whose
# cause we don't know (reader died, receiver lost its fix mid-write, ...).
GPS_TIME_MAX_AGE_S = 10.0

# ---- AGPS (gpsOneXTRA assist) ----
# Predicted-ephemeris files from Qualcomm's public servers. Injecting one
# spares a cold-starting receiver the 30 s-per-satellite broadcast download
# that dominates time-to-first-fix, and it works with no SIM — the ROUTER
# downloads the file over whatever WAN it has and pushes it over the AT port.
# xtra3grcej covers GPS+GLONASS+BeiDou+Galileo+QZSS (RM5xx class); the plain
# grc file is the fallback for firmwares without the ej variant.
XTRA_URLS = (
    "https://xtrapath1.izatcloud.net/xtra3grcej.bin",
    "https://xtrapath2.izatcloud.net/xtra3grcej.bin",
    "https://xtrapath3.izatcloud.net/xtra3grcej.bin",
    "https://xtrapath1.izatcloud.net/xtra3grc.bin",
)
# Sanity bounds for a downloaded file: a captive portal's HTML page must not
# end up injected into the modem as "ephemeris".
XTRA_MIN_BYTES = 10 * 1024
XTRA_MAX_BYTES = 300 * 1024


def _nmea_epoch(t, d):
    """UTC epoch seconds from NMEA hhmmss[.sss] + ddmmyy fields, keeping the
    fractional second (the whole point of a time sync). None if malformed."""
    try:
        base = datetime.datetime(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]),
                                 int(t[0:2]), int(t[2:4]),
                                 tzinfo=datetime.timezone.utc)
        sec = float(t[4:])
    except (ValueError, IndexError):
        return None
    if not 0 <= sec < 61:
        return None
    return base.timestamp() + sec

# ---- trip odometer ----
# A stationary receiver wanders. Summing every 1 Hz GGA delta would clock up
# kilometres overnight in a parked car, so a step only counts once it is
# longer than the fix's own error estimate -- and never shorter than this,
# which covers the case where HDOP is optimistic.
TRIP_MIN_STEP_M = 8.0
# The distance floor alone is not enough: jitter of a few metres either side
# of the truth eventually swings far enough in one sample to clear it, and a
# receiver left parked for a day takes that bet thousands of times. NMEA
# speed comes from Doppler, not from differencing positions, so it reads
# honestly near zero when stationary -- it is the reliable "is it actually
# moving" signal. Below this, fixes only re-anchor and never accumulate.
TRIP_MIN_SPEED_KMH = 1.5
# Ceiling on how far the receiver is believed to have moved between two
# fixes, as a speed. Reacquisition after a tunnel, or a solution jumping
# between constellations, otherwise lands as one instant 20 km "trip".
# 100 m/s is 360 km/h: faster than anything this router rides in.
TRIP_MAX_SPEED_MS = 100.0


def _nmea_checksum_ok(line):
    """Validate `$...*hh`. Sentences without a checksum are rejected."""
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, csum = line[1:].rpartition("*")
    try:
        want = int(csum[:2], 16)
    except (ValueError, IndexError):
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _f(field):
    """NMEA float field, '' -> None."""
    try:
        return float(field)
    except (ValueError, TypeError):
        return None


def _i(field):
    try:
        return int(field)
    except (ValueError, TypeError):
        return None


def _latlon(value, hemi):
    """ddmm.mmmm / dddmm.mmmm + N/S/E/W -> signed decimal degrees."""
    v = _f(value)
    if v is None or not hemi:
        return None
    deg = int(v // 100)
    minutes = v - deg * 100
    dec = deg + minutes / 60.0
    if hemi in ("S", "W"):
        dec = -dec
    return round(dec, 7)


def _dist_m(lat1, lon1, lat2, lon2):
    """Haversine distance in meters."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


class GpsClient:
    def __init__(self, nmea_port=GPS_NMEA_PORT):
        self.nmea_port = nmea_port
        # AT goes through the modem client's single serial port; set by
        # set_at_sender() once that client exists. No second tty of our own.
        self._at_sender = None
        self._at_payload_sender = None
        # XTRA state, cached: the 2 s status poll must never put AT queries
        # on the shared port; only the settings panel refreshes this.
        self._agps_cache = {"t": 0.0, "value": None}
        self._lock = threading.Lock()
        self._reader = None
        self._stop_evt = threading.Event()
        self.powered = False
        self._powered_since = 0.0
        self.last_error = ""
        # Rolling AT/lifecycle log for the /gps page's collapsible panel.
        # deque appends are atomic, so writers don't need the state lock.
        self._at_log = deque(maxlen=200)
        self._last_logged_err = None
        self._reset_fix_state()

    def _log(self, kind, text):
        """kind: 'tx' (command sent), 'rx' (modem reply), 'evt', 'err'."""
        self._at_log.append({"t": round(time.time(), 2), "kind": kind,
                             "text": text})

    def _log_err_once(self, text):
        """Errors from retry loops repeat every few seconds; log only on
        change so they don't flush the interesting entries out."""
        if text != self._last_logged_err:
            self._last_logged_err = text
            self._log("err", text)

    def set_at_sender(self, fn):
        """Point AT traffic at the modem client's port.

        `fn(cmd, timeout) -> str | None`. Injected rather than imported so
        this module keeps no dependency on pc_modem_client (which owns real
        hardware and is awkward to construct in tests).
        """
        self._at_sender = fn

    def _reset_fix_state(self):
        self._fix = {}
        self._sats = {}          # constellation -> {prn: sat dict}
        self._gsv_partial = {}   # talker -> accumulating list across GSV parts
        self._used_prns = {}     # talker -> set of PRNs used in fix
        self._last_nmea_ts = 0.0
        self._sentence_count = 0
        # Trip odometer. Zeroed here rather than in __init__ so it restarts
        # with the GNSS session -- power off/on and a module restart both come
        # through here, and a device reboot gets it for free by being a new
        # process. That is the whole definition of the number the page shows:
        # distance since this session started.
        self._trip_m = 0.0
        self._trip_since = None   # epoch of the first fix that counted
        self._trip_last = None    # (t, lat, lon) of the last counted fix
        # Last valid RMC time: {"epoch": GPS UTC, "mono": time.monotonic() at
        # receive, "wall": time.time() at receive}. mono anchors the clock set
        # (immune to the very step it performs); wall only feeds the displayed
        # system-vs-GPS offset.
        self._time_ref = None
        # monotonic stamp of the last GGA carrying a real fix; feeds the
        # map's uncertainty circle, which grows while the fix is lost.
        self._last_fix_mono = None

    # ---------------- AT control ----------------

    def _send_at(self, cmd, timeout=3.0):
        """One-shot command on the GNSS AT port. Returns the full response
        text ('' on port trouble, with last_error set).

        Serialized and retried: the port is opened exclusively (TIOCEXCL) for
        each command, so two overlapping callers — or anyone else poking the
        same tty — made the loser fail outright with
        "Could not exclusively lock port ... Resource temporarily
        unavailable". The lock removes our own races and the retry rides out
        someone else's brief hold instead of surfacing it as an error.
        """
        self._log("tx", cmd)
        sender = self._at_sender
        if sender is None:
            self.last_error = "no modem AT channel"
            self._log("err", self.last_error)
            return ""
        try:
            resp = sender(cmd, timeout)
        except Exception as e:
            self.last_error = f"AT {cmd}: {e}"
            pcat_logger.logger.warning(f"gps: AT {cmd} failed: {e}")
            self._log("err", self.last_error)
            return ""
        if resp is None:
            # Modem client not connected, or the port stayed busy past the
            # timeout. Transient by nature — the caller retries on its own
            # schedule, so this must not be logged as a hard failure.
            self.last_error = "modem AT port busy"
            self._log("err", self.last_error)
            return ""
        lines = [l for l in resp.replace("\r", "\n").split("\n")
                 if l.strip() and l.strip() != cmd]
        self._log("rx", " | ".join(lines) if lines else "(no response)")
        self.last_error = ""
        return resp

    # Constellation profiles, as AT+QGPSCFG="gnssconfig" values. The module
    # reports the accepted range as 0-7 (AT+QGPSCFG=? on an RM500Q-GL), and
    # this is Quectel's documented layout for the family: GPS is always on,
    # the value selects which of the other three ride along.
    #
    # Measured caveat, do not "fix" by removing the note in the UI: writing a
    # new value while a session is running does NOT take effect — the module
    # kept emitting GLONASS for minutes after being set to 0, through a
    # QGPSEND/QGPS=1 cycle. It is applied by the module at startup, so the
    # page tells the user a modem restart is needed.
    GNSS_PROFILES = [
        {"value": 1, "key": "auto",     "glonass": True,  "beidou": True,  "galileo": True},
        {"value": 0, "key": "gps",      "glonass": False, "beidou": False, "galileo": False},
        {"value": 4, "key": "glonass",  "glonass": True,  "beidou": False, "galileo": False},
        {"value": 6, "key": "beidou",   "glonass": False, "beidou": True,  "galileo": False},
        {"value": 7, "key": "galileo",  "glonass": False, "beidou": False, "galileo": True},
        {"value": 2, "key": "gl_bd",    "glonass": True,  "beidou": True,  "galileo": False},
        {"value": 3, "key": "gl_ga",    "glonass": True,  "beidou": False, "galileo": True},
        {"value": 5, "key": "bd_ga",    "glonass": False, "beidou": True,  "galileo": True},
    ]

    def _enable_all_nmea_talkers(self):
        """Let every enabled constellation report its satellites.

        The module ships with gpsnmeatype=31 but glonass/galileo/beidou
        nmeatype=0, so it tracks those satellites and then emits no GSV for
        them — the page could only ever draw GPS, even with gnssconfig=1
        (all constellations). Verified on an RM500Q-GL: flipping these to 1
        was what made seven GLONASS satellites appear. Best effort; a module
        that rejects one just keeps its default.
        """
        for cmd in ('AT+QGPSCFG="glonassnmeatype",1',
                    'AT+QGPSCFG="galileonmeatype",1',
                    'AT+QGPSCFG="beidounmeatype",1'):
            self._send_at(cmd)

    def get_constellation(self):
        """Current gnssconfig value, or None when the module won't say."""
        resp = self._send_at('AT+QGPSCFG="gnssconfig"')
        m = re.search(r'"gnssconfig",\s*(\d+)', resp or "")
        return int(m.group(1)) if m else None

    def set_constellation(self, value):
        """Write gnssconfig. Returns (ok, detail)."""
        if not any(p["value"] == value for p in self.GNSS_PROFILES):
            return False, "unsupported value"
        resp = self._send_at('AT+QGPSCFG="gnssconfig",{}'.format(value))
        if "OK" not in (resp or ""):
            return False, (resp or "no reply").strip()
        # Re-assert the per-constellation NMEA output: a profile change is
        # pointless if the newly enabled system stays silent.
        self._enable_all_nmea_talkers()
        return True, ""

    def nmea_stalled(self):
        """True when we believe GNSS is on but the stream has gone quiet.

        Distinguishes "just started, nothing yet" from "was flowing and
        stopped": both are reported, because a session that never produces a
        sentence is just as dead as one that stops. The reader is given
        NMEA_STALL_SECS of grace either way."""
        if not self.powered:
            return False
        with self._lock:
            last = self._last_nmea_ts
        started = self._powered_since or 0
        ref = max(last, started)
        return bool(ref) and (time.time() - ref) > NMEA_STALL_SECS

    def restart_modem(self):
        """Bounce the module (AT+CFUN=1,1) so a new gnssconfig takes effect.

        The constellation profile is only read by the module at startup, so
        without this the UI can save a setting that never applies. Costs a
        brief cellular outage while the module re-registers; the caller
        confirms first. Returns (ok, detail)."""
        resp = self._send_at("AT+CFUN=1,1", timeout=6.0)
        if "OK" not in (resp or ""):
            return False, (resp or "no reply").strip()
        # The module drops off the USB bus for a while; the reader thread
        # would just log read errors against a dead tty until it returns.
        self._stop_reader()
        # The module comes back with the GNSS session OFF. Leaving
        # self.powered set would make us believe GNSS is running when
        # AT+QGPS? says 0 -- and _gps_restore_if_wanted() skips a client
        # that already claims to be powered, so nothing ever re-armed it and
        # the page sat at "0 sentences" until someone toggled by hand.
        self.powered = False
        self._powered_since = 0.0
        with self._lock:
            self._reset_fix_state()
        self._log("evt", "modem restarting; GNSS will be re-armed when it returns")
        return True, ""

    def power_on(self):
        """Start the GNSS engine and the NMEA reader. Idempotent."""
        resp = self._send_at("AT+QGPS=1")
        # "+CME ERROR: 504" = session already active — that's still success.
        ok = "OK" in resp or "504" in resp
        if not ok:
            if resp:
                self.last_error = f"AT+QGPS=1: {resp.strip()}"
            self.powered = False
            return False
        self.last_error = ""
        self.powered = True
        self._powered_since = time.time()
        # Without this the module tracks GLONASS/Galileo/BeiDou but emits no
        # GSV for them, so the page shows an empty sky next to a live fix.
        self._enable_all_nmea_talkers()
        self._start_reader()
        return True

    def power_off(self):
        """Stop the reader and shut the engine down."""
        self.powered = False
        self._powered_since = 0.0
        self._stop_reader()
        resp = self._send_at("AT+QGPSEND")
        with self._lock:
            self._reset_fix_state()
        # "+CME ERROR: 505" = session not active — already off is fine.
        return "OK" in resp or "505" in resp

    # ---------------- AGPS (gpsOneXTRA) ----------------

    def set_at_payload_sender(self, fn):
        """Point XTRA uploads at the modem client's CONNECT-then-raw-bytes
        channel: `fn(cmd, payload_bytes, timeout) -> str | None`. Injected
        for the same reason as set_at_sender()."""
        self._at_payload_sender = fn

    def agps_status(self, refresh=False):
        """{enabled, valid_min, injected_utc} of the module's XTRA state,
        cached. AT queries run only on refresh (or the very first call) —
        callers on the 2 s poll get the cache for free."""
        if not refresh and self._agps_cache["value"] is not None:
            return self._agps_cache["value"]
        st = {"enabled": None, "valid_min": None, "injected_utc": None}
        m = re.search(r"\+QGPSXTRA:\s*(\d)", self._send_at("AT+QGPSXTRA?"))
        if m:
            st["enabled"] = m.group(1) == "1"
        m = re.search(r'\+QGPSXTRADATA:\s*(\d+),"([^"]*)"',
                      self._send_at("AT+QGPSXTRADATA?"))
        if m:
            st["valid_min"] = int(m.group(1))
            # The modem answers "2026/08/16,04:00:00"; the UI shows ISO
            # yyyy-mm-dd everywhere, so normalize before it leaves here.
            st["injected_utc"] = (m.group(2).replace("/", "-")
                                  .replace(",", " "))
        self._agps_cache = {"t": time.time(), "value": st}
        return st

    def _download_xtra(self):
        """(filename, data) of the freshest assist file, or (None, error)."""
        last_err = "no assist URL reachable"
        for url in XTRA_URLS:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "pcat-manager-web"})
                data = urllib.request.urlopen(req, timeout=20).read(
                    XTRA_MAX_BYTES + 1)
                if not XTRA_MIN_BYTES <= len(data) <= XTRA_MAX_BYTES:
                    last_err = "{}: unexpected size {}".format(url, len(data))
                    continue
                return url.rsplit("/", 1)[-1], data
            except Exception as e:
                last_err = "{}: {}".format(url, e)
        return None, last_err

    def agps_update(self):
        """Download gpsOneXTRA assist data and inject it into the module,
        cutting a cold start's time-to-first-fix from minutes to seconds.

        Sequence per Quectel's GNSS application note: enable XTRA, upload the
        file to the module FS (QFUPL, binary phase), inject the current UTC
        (QGPSXTRATIME — the system clock, which NTP or our own GPS sync keeps
        honest), then activate the file (QGPSXTRADATA). The GNSS session is
        stopped for the injection and re-armed after; the whole operation is
        SIM-independent. Returns (fresh status, None) or (None, error)."""
        sender = self._at_payload_sender
        if sender is None:
            return None, "modem AT channel not ready"
        name, data = self._download_xtra()
        if name is None:
            return None, "assist download failed — is the router online? ({})".format(data)
        if "+QGPSXTRA: 1" not in self._send_at("AT+QGPSXTRA?"):
            if "OK" not in self._send_at("AT+QGPSXTRA=1"):
                return None, "module refused to enable XTRA"
        was_powered = self.powered
        if was_powered:
            self.power_off()
        try:
            # Two-arg XTRATIME (time, uncertainty ms): the five-arg form from
            # older app notes gets +CME ERROR: 501 on the RM520N (verified on
            # hardware; AT+QGPSXTRATIME=? reports <XTRA_time>,<uncrtn>). Time
            # goes in BEFORE the file so the engine doesn't judge the
            # ephemeris against its 1980 RTC and reject it as expired.
            utc = time.strftime("%Y/%m/%d,%H:%M:%S", time.gmtime())
            if "OK" not in self._send_at(
                    'AT+QGPSXTRATIME="{}",3500'.format(utc), timeout=10.0):
                return None, "time injection refused"
            # UFS:, not RAM: — the GNSS engine only reads assist files from
            # UFS (a RAM: upload succeeds but QGPSXTRADATA then errors;
            # verified on hardware).
            fname = "UFS:" + name
            self._send_at('AT+QFDEL="{}"'.format(fname))  # stale copy, if any
            cmd = 'AT+QFUPL="{}",{},60'.format(fname, len(data))
            self._log("tx", "{} (+{} B payload)".format(cmd, len(data)))
            try:
                resp = sender(cmd, data, 90.0)
            except Exception as e:
                resp = None
                pcat_logger.logger.warning("gps: XTRA upload failed: %s", e)
            self._log("rx", (resp or "(no response)").replace("\n", " | "))
            if not resp or "OK" not in resp:
                return None, "assist file upload failed: {}".format(
                    (resp or "no response").strip())
            # The engine validates the whole file before answering — takes a
            # few seconds, well past the default AT timeout.
            resp = self._send_at('AT+QGPSXTRADATA="{}"'.format(fname),
                                 timeout=20.0)
            if "OK" not in resp:
                return None, ("assist data injection refused: {}".format(
                    resp.strip() or "no response"))
            self._send_at('AT+QFDEL="{}"'.format(fname))
        finally:
            if was_powered:
                self.power_on()
        # The validity query can transiently ERROR right after an injection;
        # give the engine a moment before trusting the readback.
        st = self.agps_status(refresh=True)
        if not st.get("valid_min"):
            time.sleep(2)
            st = self.agps_status(refresh=True)
        pcat_logger.logger.info(
            "gps: AGPS assist injected (%s, %d bytes, valid %s min)",
            name, len(data), st.get("valid_min"))
        return st, None

    # ---------------- NMEA reader ----------------

    def _start_reader(self):
        if self._reader and self._reader.is_alive():
            return
        self._stop_evt.clear()
        self._reader = threading.Thread(target=self._reader_loop,
                                        name="gps-nmea-reader", daemon=True)
        self._reader.start()

    def _stop_reader(self):
        self._stop_evt.set()
        reader = self._reader
        if reader and reader.is_alive():
            reader.join(timeout=3)
        self._reader = None

    def _reader_loop(self):
        while not self._stop_evt.is_set():
            try:
                port = serial.Serial(self.nmea_port, 115200, timeout=1)
                self._log("evt", f"NMEA reader listening on {self.nmea_port}")
                self._last_logged_err = None
            except Exception as e:
                self.last_error = f"NMEA port {self.nmea_port}: {e}"
                self._log_err_once(self.last_error)
                if self._stop_evt.wait(3):
                    return
                continue
            try:
                while not self._stop_evt.is_set():
                    line = port.readline().decode("ascii", "replace").strip()
                    if not line:
                        continue
                    if _nmea_checksum_ok(line):
                        with self._lock:
                            self._handle_sentence(line)
            except Exception as e:
                self.last_error = f"NMEA read: {e}"
                pcat_logger.logger.warning(f"gps: NMEA reader error: {e}")
                self._log_err_once(self.last_error)
            finally:
                try:
                    port.close()
                except Exception:
                    pass
            self._stop_evt.wait(2)
        self._log("evt", "NMEA reader stopped")

    def _handle_sentence(self, line):
        body = line[1:line.rindex("*")]
        fields = body.split(",")
        head = fields[0]
        if len(head) < 5:
            return
        talker, stype = head[:2], head[2:]
        self._last_nmea_ts = time.time()
        self._sentence_count += 1
        if stype == "GGA":
            self._parse_gga(fields)
        elif stype == "RMC":
            self._parse_rmc(fields)
        elif stype == "GSA":
            self._parse_gsa(talker, fields)
        elif stype == "GSV":
            self._parse_gsv(talker, fields)
        elif stype == "VTG":
            self._parse_vtg(fields)

    def _parse_gga(self, f):
        # GGA: time, lat, NS, lon, EW, quality, numsat, hdop, alt, M, geoid, M
        if len(f) < 12:
            return
        self._fix["utc_time"] = f[1][:6] if f[1] else self._fix.get("utc_time")
        quality = _i(f[6]) or 0
        self._fix["quality"] = quality
        if quality > 0:
            self._fix["lat"] = _latlon(f[2], f[3])
            self._fix["lon"] = _latlon(f[4], f[5])
            self._fix["alt_m"] = _f(f[9])
        self._fix["sats_used"] = _i(f[7])
        hdop = _f(f[8])
        if hdop is not None:
            self._fix["hdop"] = hdop
            self._fix["accuracy_m"] = round(hdop * UERE_M, 1)
        # After accuracy_m, so the step is judged against this fix's own error
        # estimate rather than the previous cycle's.
        if quality > 0 and self._fix.get("lat") is not None:
            self._last_fix_mono = time.monotonic()
            self._accumulate_trip(self._fix["lat"], self._fix["lon"],
                                  self._fix.get("accuracy_m"),
                                  self._fix.get("speed_kmh"))

    def _accumulate_trip(self, lat, lon, accuracy_m, speed_kmh=None):
        """Integrate travelled distance across fixes. Caller holds the lock.

        Straight-line between counted fixes, which reads a little short on a
        winding road and would read absurdly long if it summed noise. The
        gates below choose the first error over the second."""
        now = time.time()
        if self._trip_since is None:
            self._trip_since = now
        if self._trip_last is None:
            self._trip_last = (now, lat, lon)
            return
        # Standing still: keep the anchor under the receiver so drift never
        # builds up a gap that later clears the distance floor in one sample,
        # but count none of it. Speed is absent on receivers sending neither
        # RMC nor VTG, and then the distance gates carry this alone.
        if speed_kmh is not None and speed_kmh < TRIP_MIN_SPEED_KMH:
            self._trip_last = (now, lat, lon)
            return
        last_t, last_lat, last_lon = self._trip_last
        d = _dist_m(last_lat, last_lon, lat, lon)
        # Inside the fix's own error bars, so it says nothing about movement.
        # The anchor stays put here: re-anchoring on a sample that might be
        # pure noise would throw away the real displacement it is hiding.
        if d < max(TRIP_MIN_STEP_M, accuracy_m or 0.0):
            return
        dt = max(now - last_t, 1.0)
        if d <= TRIP_MAX_SPEED_MS * dt:
            self._trip_m += d
        # Anchor moves either way. On a rejected jump the receiver really is
        # over there, and holding the old anchor would only re-reject forever.
        self._trip_last = (now, lat, lon)

    def _parse_rmc(self, f):
        # RMC: time, status, lat, NS, lon, EW, spd_kn, course, date, ...
        if len(f) < 10:
            return
        self._fix["rmc_valid"] = (f[2] == "A")
        if f[9] and len(f[9]) == 6:
            self._fix["utc_date"] = f[9]
        # Time reference for clock sync: RMC is the one sentence carrying both
        # time and date, and only its A (valid) form is trusted — a V sentence
        # may carry the receiver's free-running RTC guess.
        if f[2] == "A" and f[1] and f[9] and len(f[9]) == 6:
            epoch = _nmea_epoch(f[1], f[9])
            if epoch is not None:
                self._time_ref = {"epoch": epoch,
                                  "mono": time.monotonic(),
                                  "wall": time.time()}
        if f[2] == "A":
            kn = _f(f[7])
            if kn is not None:
                self._fix["speed_kmh"] = round(kn * 1.852, 2)
            course = _f(f[8])
            if course is not None:
                self._fix["course_deg"] = course

    def _parse_gsa(self, talker, f):
        # GSA: mode, fixtype(1/2/3), 12x sat id, pdop, hdop, vdop[, systemId]
        if len(f) < 18:
            return
        # GN GSA repeats once per constellation each cycle; track per talker
        # and let get_status() take the max, so a lost fix decays to 1 instead
        # of latching at the best value ever seen.
        self._fix.setdefault("fix_type_by_talker", {})[talker] = _i(f[2]) or 1
        pdop, vdop = _f(f[15]), _f(f[17])
        if pdop is not None:
            self._fix["pdop"] = pdop
        if vdop is not None:
            self._fix["vdop"] = vdop
        used = {p for p in (_i(x) for x in f[3:15]) if p}
        self._used_prns[talker] = used

    def _parse_gsv(self, talker, f):
        # GSV: total_msgs, msg_no, sats_in_view, then 4x (prn, elev, az, snr)
        if len(f) < 4:
            return
        total, num = _i(f[1]), _i(f[2])
        if not total or not num:
            return
        if num == 1:
            self._gsv_partial[talker] = []
        part = self._gsv_partial.setdefault(talker, [])
        # Groups of 4 after f[3]; NMEA 4.10 appends a lone signal-ID field at
        # the end, which the range bound below leaves out naturally.
        for idx in range(4, len(f) - 3, 4):
            chunk = f[idx:idx + 4]
            prn = _i(chunk[0])
            if prn:
                part.append({
                    "prn": prn,
                    "elev": _i(chunk[1]),
                    "az": _i(chunk[2]),
                    "snr": _i(chunk[3]),
                })
        if num == total:
            gnss = TALKER_GNSS.get(talker, talker)
            self._sats[gnss] = {s["prn"]: s for s in part}
            self._gsv_partial.pop(talker, None)

    def _parse_vtg(self, f):
        # VTG: course, T, course_mag, M, spd_kn, N, spd_kmh, K
        if len(f) < 8:
            return
        kmh = _f(f[7])
        if kmh is not None:
            self._fix["speed_kmh"] = round(kmh, 2)
        course = _f(f[1])
        if course is not None:
            self._fix["course_deg"] = course

    # ---------------- time sync ----------------

    def sync_system_time(self):
        """Set the system clock from the last valid RMC time.

        The target is extrapolated from the receive instant on the MONOTONIC
        clock (epoch + measured report latency + elapsed-since-receive), so
        the result does not depend on the wall clock being anywhere near
        right — that is the clock being fixed. Returns (result, error): on
        success result = {"adjusted_s": step applied, "utc": ISO of the new
        time}, error None; on failure result None and error a short reason.
        """
        with self._lock:
            ref = self._time_ref
        if not ref:
            return None, "No GPS time received yet"
        age = time.monotonic() - ref["mono"]
        if age > GPS_TIME_MAX_AGE_S:
            return None, "GPS time is stale ({:.0f}s old)".format(age)
        target = ref["epoch"] + GPS_TIME_LATENCY_S + (time.monotonic() - ref["mono"])
        adjust = target - time.time()
        try:
            time.clock_settime(time.CLOCK_REALTIME, target)
        except Exception as e:
            return None, "Setting clock failed: {}".format(e)
        with self._lock:
            # Re-stamp the reference's wall time as it would read under the
            # clock just set: after a successful set the system agrees with
            # GPS by construction, so the receive instant maps to exactly
            # epoch + latency. (Not old-wall + adjust: that silently assumes
            # nothing else stepped the wall clock between receive and now.)
            if self._time_ref is ref:
                self._time_ref = dict(ref, wall=ref["epoch"] + GPS_TIME_LATENCY_S)
        pcat_logger.logger.info(
            "gps: system clock set from GNSS (%+.3fs adjustment)", adjust)
        self._log("evt", "system clock set from GNSS ({:+.3f}s)".format(adjust))
        return {"adjusted_s": round(adjust, 3),
                "utc": datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ")}, None

    # ---------------- status ----------------

    def get_status(self):
        with self._lock:
            fix = dict(self._fix)
            used = set()
            for prns in self._used_prns.values():
                used |= prns
            sats = []
            for gnss, by_prn in self._sats.items():
                for sat in by_prn.values():
                    entry = dict(sat)
                    entry["gnss"] = gnss
                    entry["used"] = sat["prn"] in used
                    sats.append(entry)
            nmea_age = (time.time() - self._last_nmea_ts) if self._last_nmea_ts else None
            sentence_count = self._sentence_count
            trip_m, trip_since = self._trip_m, self._trip_since
            time_ref = self._time_ref
            last_fix_mono = self._last_fix_mono
        # A clock set (ours or NTP's) can step wall time past stamps taken
        # before it; clamp so ages never render negative.
        if nmea_age is not None:
            nmea_age = max(0.0, nmea_age)
        time_sync = None
        if time_ref:
            ref_age = time.monotonic() - time_ref["mono"]
            time_sync = {
                # system - GPS at the receive instant, latency already counted:
                # ~0 means the clocks agree.
                "offset_s": round(
                    time_ref["wall"] - (time_ref["epoch"] + GPS_TIME_LATENCY_S), 3),
                "age_s": round(ref_age, 1),
                "latency_s": GPS_TIME_LATENCY_S,
                "usable": ref_age <= GPS_TIME_MAX_AGE_S,
            }
        sats.sort(key=lambda s: (s["gnss"], s["prn"]))
        quality = fix.get("quality", 0)
        fix_type = max(fix.get("fix_type_by_talker", {}).values(), default=1)
        has_fix = bool(quality) and fix.get("lat") is not None
        utc = None
        t, d = fix.get("utc_time"), fix.get("utc_date")
        if t and len(t) >= 6 and d:
            utc = (f"20{d[4:6]}-{d[2:4]}-{d[0:2]}"
                   f"T{t[0:2]}:{t[2:4]}:{t[4:6]}Z")
        return {
            "powered": self.powered,
            "error": self.last_error or None,
            "nmea_age_s": round(nmea_age, 1) if nmea_age is not None else None,
            "sentences": sentence_count,
            "fix": {
                "has_fix": has_fix,
                "quality": quality,
                "fix_type": {2: "2D", 3: "3D"}.get(fix_type, "—"),
                "lat": fix.get("lat"),
                "lon": fix.get("lon"),
                "alt_m": fix.get("alt_m"),
                "accuracy_m": fix.get("accuracy_m") if has_fix else None,
                "hdop": fix.get("hdop"),
                "pdop": fix.get("pdop"),
                "vdop": fix.get("vdop"),
                "speed_kmh": fix.get("speed_kmh") if has_fix else None,
                "course_deg": fix.get("course_deg") if has_fix else None,
                "sats_used": len(used) or fix.get("sats_used") or 0,
                "utc": utc,
                # seconds since the last GGA that carried a real fix; the map
                # grows the uncertainty circle with this while has_fix is off
                "age_s": (round(max(0.0, time.monotonic() - last_fix_mono), 1)
                          if last_fix_mono is not None else None),
            },
            "satellites": {
                "in_view": len(sats),
                "list": sats,
            },
            # Outside "fix" on purpose: losing the fix in a tunnel must not
            # blank the distance already travelled, the way speed and course
            # (correctly) blank.
            "trip": {
                "distance_m": round(trip_m, 1),
                "since": round(trip_since, 1) if trip_since else None,
            },
            # None until a valid (status A) RMC has been seen this session.
            "time_sync": time_sync,
            "at_log": list(self._at_log),
        }


gps_client = GpsClient()
