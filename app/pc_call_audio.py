"""
CallAudioBridge - stream voice-call PCM between the modem ALSA card and a
browser.

The board wires the cellular module's PCM voice interface (Quectel RM500U /
EM05; the FM350 does not bond these pins) straight to the SoC's SAI2, exposed
as an ALSA card named "modem" via a bt-sco dummy codec. During a voice call
the module clocks 8kHz mono S16_LE PCM on that link in both directions.

This bridge is deliberately plain HTTP, not WebRTC and not socket.io:

  - downlink: one GET returns an unbounded chunked stream of raw PCM read
    from arecord. Werkzeug serves streaming generators fine and the reader
    (fetch + ReadableStream) works on every modern browser, over plain HTTP
    too -- so "hear the caller" works even before TLS is set up.
  - uplink: the page POSTs ~250ms chunks of raw PCM which are written into a
    persistent aplay's stdin. A few requests per second is nothing.

The microphone itself needs a secure context in the browser; that is handled
by the TLS listener in pcat_manager_web, not here.

arecord/aplay come from alsa-utils, which the firmware already ships. Using
subprocesses instead of a python ALSA binding keeps this dependency-free.

One browser at a time: starting a new downlink stream kills the previous
arecord. Everything stops on stop(), which the hangup/status paths call.
"""

import array
import subprocess
import threading
import time
import os
from collections import deque

from pcat_util import pcat_logger

CARD_NAME = "modem"
# ALSA id of the Quectel UAC function (USB audio) -- how RM500Q call audio
# reaches the host. /proc/asound/<id> exists whenever the card is enumerated.
UAC_CARD_NAME = "RM500QGL"
# Plain 16kHz mono, one sample per 128-bit frame. Confirmed against the
# network's ringback tone rather than the modem's own loopback (that path runs
# through the voice codec and cannot prove bit-exactness): the capture is a
# smooth continuous waveform with adjacent sample pairs equal only 4% of the
# time, i.e. chance. No slot duplication, no rate error. Wire rate measured
# 15974 Hz against 16000 nominal.
RATE = 16000
# One ALSA period. This is the downlink's quantum: audio is handed to the
# browser as soon as a period lands rather than being pooled into a bigger
# block, so it contributes 20ms of delay instead of 100ms. Smaller costs
# syscalls and HTTP chunk framing for no audible gain.
CHUNK_BYTES = 640           # 20ms of wire audio
TX_IDLE_TIMEOUT = 10.0      # kill aplay this long after the last uplink chunk
# How far the downlink may run ahead of the wire clock before we treat the
# excess as buffered backlog and discard it. Generous enough that ordinary
# scheduling jitter and the initial buffer fill do not trip it, tight enough
# that a stall cannot leave more than this much permanent delay behind.
RX_MAX_LAG = 0.25

# Debug taps: every stream is also recorded raw so a rate/format problem can
# be analysed offline instead of guessed at. Overwritten at each stream start;
# capped so a forgotten call cannot fill /tmp.
DEBUG_RX_FILE = "/tmp/call-rx.raw"
DEBUG_TX_FILE = "/tmp/call-tx.raw"
DEBUG_CAP = 16 * 1024 * 1024

# Uplink jitter buffer. The browser delivers ~250ms of audio per HTTP POST but
# ALSA wants a continuous feed, so POSTs only enqueue and a writer thread feeds
# aplay. Writes to aplay's stdin block once the ALSA buffer is full, which
# paces the writer at exactly real time for free.
#
# The writer WAITS for data when the queue is empty; it must not substitute
# silence just because a POST is momentarily late. Doing that chopped speech
# into alternating audio/silence fragments and made the far-end pulsing worse,
# not better. An empty queue is normal and harmless -- the ALSA buffer still
# has audio in flight. Silence is only injected after a long genuine gap, to
# keep the stream from underrunning while nobody is talking.
# Startup cost of the uplink, which the caller hears as the first word being
# eaten. aplay sets start_threshold to the whole buffer, so nothing is clocked
# out until that much has been written; the preroll below is paid on top. At
# the original 300ms preroll + 400ms buffer that was 700ms of speech gone
# before the far end heard anything, and every later underrun cost another
# 400ms because stop_threshold is the buffer too (XRUN -> refill). Both are
# now 100/200ms, which the browser's 100ms POST cadence keeps fed.
TX_PREROLL_BYTES = 1280     # 40ms at 16k mono S16 before playback starts
# ~250ms. This is the hard ceiling on uplink delay: anything queued beyond it
# is audio the far end would hear late, so the oldest is dropped instead. Was
# 1s, which let a brief network hiccup put a quarter-second of permanent lag
# into the call and kept it there for the duration.
TX_MAX_QUEUE_BYTES = 8000
# Grace for a late POST before the writer fills. This has to be well under one
# fill: waiting T and then writing F of silence produces F per T of wall clock,
# so any T > F starves the sink no matter how big the buffer is, and the stream
# XRUNs anyway. Writing is paced by aplay blocking once ALSA is full, so a
# short grace costs nothing when audio is arriving normally.
TX_WAIT_TIMEOUT = 0.03
# One ALSA period. Filling a whole CHUNK_BYTES on every gap would push 100ms of
# silence in front of speech that was only a few ms late.
TX_SILENCE_BYTES = 1280     # 40ms at 16k mono S16

# When the sink cannot be opened at all -- a module that clocks PCM in but not
# out, say -- aplay exits the moment it is written to, and the browser posting
# every 100ms would otherwise respawn it ten times a second for the length of
# the call. Measured on an RM500Q: 70 restarts and 67 identical error lines in
# one call. Back off instead, and say so once.
TX_START_FAIL_LIMIT = 3     # consecutive instant deaths before backing off
TX_START_FAIL_QUIET = 30.0  # seconds to stop retrying for
TX_ALIVE_SECS = 1.0         # lived at least this long => not an instant death

# Call recording. Both directions of a call already pass through this bridge
# (downlink through rx_stream's read loop, uplink through tx_write), so the
# recorder is a pair of taps on those paths: downlink drives the clock, and
# whatever uplink audio is pending gets mixed over it sample-by-sample. That
# means a recording only captures audio while a browser has the call's audio
# connected -- which is always, in practice, since calls can only be placed
# and answered from the phone page.
RECORD_DIR = "/root/call_recordings"
RECORD_MAX_FILES = 100          # prune oldest beyond this at record start
RECORD_MAX_BYTES = 3 * 3600 * RATE * 2   # 3h safety cap per file
REC_TX_PEND_MAX = RATE * 2 * 5  # keep at most 5s of unpaired mic audio


def _log():
    return pcat_logger.logger


def _drain_stderr(proc, what):
    """Log whatever arecord/aplay writes to stderr, in the background.

    These used to run with stderr=DEVNULL, so an ALSA failure surfaced only as
    "downlink stream ended (no data)" with no reason -- and every real problem
    on this link (the modem not clocking PCM, a rate the card will not take, a
    device already in use) is reported by ALSA on exactly that channel. A
    thread rather than proc.communicate() because the stream must keep being
    read while these processes are alive.
    """
    def run():
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "ignore").strip()
                if line:
                    _log().error("call-audio: %s: %s", what, line)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def _wav_header(data_bytes):
    import struct
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
            + b"data" + struct.pack("<I", data_bytes))


def _mix_s16(base, over):
    """Saturating add of two S16_LE byte strings; len(over) <= len(base)."""
    a = array.array("h")
    a.frombytes(base)
    b = array.array("h")
    b.frombytes(over)
    for i in range(len(b)):
        s = a[i] + b[i]
        if s > 32767:
            s = 32767
        elif s < -32768:
            s = -32768
        a[i] = s
    return a.tobytes()


class CallAudioBridge:
    def __init__(self):
        self._lock = threading.Lock()
        self._arecord = None
        self._aplay = None
        self._rx_generation = 0     # bumped to tell an old stream to die
        self._last_tx = 0.0
        self._reaper = None
        # wire-rate measurement + debug taps
        self._rx_t0 = None
        self._rx_bytes = 0
        self._rx_t_last = None
        self._tx_t0 = None
        self._tx_bytes = 0
        self._tx_t_last = None
        self._rx_tap = None
        self._tx_tap = None
        self._tx_q = deque()
        self._tx_queued = 0
        self._tx_writer = None
        self._tx_started_at = 0.0
        self._tx_fail_streak = 0
        self._tx_retry_after = 0.0
        self._tx_running = False
        self._tx_cv = threading.Condition()
        # call recording (see note above RECORD_DIR)
        self._rec_lock = threading.Lock()
        self._rec_file = None
        self._rec_name = None
        self._rec_bytes = 0
        self._rec_tx_pend = bytearray()

    # ---------------------------------------------------------------- card

    def card_present(self):
        try:
            with open("/proc/asound/cards") as fh:
                names = fh.read()
            return CARD_NAME in names or UAC_CARD_NAME in names
        except OSError:
            return False

    def _device(self):
        # The module's own USB audio card wins when it exists. RM500Q-GL
        # cannot do call audio over the board's PCM/SAI link at all -- its
        # frame clocking makes the SoC's SAI transmit wedge (EIO on write,
        # which then kills the receive stream too) -- but with UAC enabled in
        # its USB composition it exposes a 16k mono S16 sound card that
        # carries both directions. Verified end to end: a tone played into
        # that card arrived in the far end's voicemail over a live call.
        # Checked per call, not cached, so a module re-enumeration mid-run
        # changes nothing but the next stream. See _arm_voice_urcs() in
        # pc_modem_client for the provisioning side (usbcfg + QPCMV=1,2).
        if os.path.exists("/proc/asound/" + UAC_CARD_NAME):
            return "plughw:CARD=" + UAC_CARD_NAME
        # plughw so ALSA inserts rate/format conversion if a future module
        # runs the link at 16k while the browser side stays at 8k.
        return "plughw:CARD=" + CARD_NAME

    # ------------------------------------------------------------ downlink

    def rx_stream(self):
        """Generator yielding raw S16_LE mono PCM chunks. One client only:
        a newer stream, or stop(), makes this one exit."""
        with self._lock:
            self._rx_generation += 1
            generation = self._rx_generation
            self._kill_arecord_locked()
            try:
                self._arecord = subprocess.Popen(
                    ["arecord", "-q", "-D", self._device(),
                     "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw",
                     # Without these arecord takes the card's default capture
                     # buffer, which on the RM500Q's UAC endpoint is deep
                     # enough to hold seconds of audio. Capture latency is
                     # whatever sits in that buffer, so cap it hard: 80ms of
                     # buffer in 20ms periods, matching the read size below.
                     "--buffer-time=80000", "--period-time=20000"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    # Unbuffered: io's default 8KB read-ahead on stdout would
                    # sit on up to 256ms of audio waiting to fill a block,
                    # on top of whatever the pipe already holds.
                    bufsize=0)
            except OSError as e:
                _log().error("call-audio: cannot start arecord: %s", e)
                return
            _drain_stderr(self._arecord, "arecord")
            proc = self._arecord
            self._rx_t0 = None
            self._rx_bytes = 0
            try:
                self._rx_tap = open(DEBUG_RX_FILE, "wb")
            except OSError:
                self._rx_tap = None
        _log().info("call-audio: downlink stream started")
        dropped = 0
        lead = 0.0                      # seconds of audio drained faster than real time
        last_read = time.monotonic()
        try:
            while True:
                data = proc.stdout.read(CHUNK_BYTES)
                if not data:
                    break
                if generation != self._rx_generation:
                    break
                now = time.monotonic()
                if self._rx_t0 is None:
                    self._rx_t0 = now
                self._rx_t_last = now
                self._rx_bytes += len(data)
                if self._rx_tap and self._rx_bytes <= DEBUG_CAP:
                    self._rx_tap.write(data)
                    self._rx_tap.flush()
                self._rec_feed_rx(data)
                # Shed backlog rather than deliver it. Everything between
                # arecord and the browser is a pure FIFO -- ALSA buffer, the
                # 64KB pipe, the socket -- so any stall (a slow client, the
                # GIL, a hidden tab) becomes permanent added delay: the audio
                # is still played, just late, and nothing ever catches up.
                # That is how a call drifts seconds behind and stays there.
                #
                # A healthy read blocks until the wire clocks the next period,
                # so it takes about one period of wall time. A read that
                # returns much faster than that got data which was already
                # queued -- i.e. we are draining a backlog. Track that credit
                # and once it exceeds RX_MAX_LAG, drop chunks (still feeding
                # the recorder above) until it is repaid. Costs a click and
                # buys back the entire accumulated delay.
                chunk_secs = len(data) / 2.0 / RATE
                elapsed = now - last_read
                last_read = now
                lead += chunk_secs - elapsed
                if lead < 0:
                    lead = 0.0          # running at or behind the wire: fine
                if lead > RX_MAX_LAG:
                    lead -= chunk_secs
                    dropped += 1
                    if dropped in (1, 10, 100, 1000):
                        _log().info(
                            "call-audio: downlink shedding buffered backlog "
                            "(%d chunks) to stay live", dropped)
                    continue
                yield data
        finally:
            with self._lock:
                if proc is self._arecord:
                    self._kill_arecord_locked()
                elif proc.poll() is None:
                    proc.kill()
            if self._rx_t0 and self._rx_t_last and self._rx_t_last > self._rx_t0:
                dur = self._rx_t_last - self._rx_t0
                _log().info(
                    "call-audio: downlink ended: %d bytes in %.2fs "
                    "= %d Hz measured (asked %d)",
                    self._rx_bytes, dur, int(self._rx_bytes / 2 / dur), RATE)
            else:
                _log().info("call-audio: downlink stream ended (no data), "
                            "arecord rc=%s", proc.poll())
            if self._rx_tap:
                try:
                    self._rx_tap.close()
                except OSError:
                    pass
                self._rx_tap = None

    # -------------------------------------------------------------- uplink

    def tx_write(self, data):
        """Enqueue uplink audio. Never writes to ALSA directly -- see the
        jitter-buffer note at the top of this file."""
        if not data:
            return True
        with self._lock:
            if self._aplay is not None and self._aplay.poll() is not None:
                # It died. An immediate death means it never opened the device;
                # count those so a sink that cannot work stops being respawned.
                if time.monotonic() - self._tx_started_at < TX_ALIVE_SECS:
                    self._tx_fail_streak += 1
                    if self._tx_fail_streak == TX_START_FAIL_LIMIT:
                        _log().error(
                            "call-audio: uplink sink will not start (%d attempts); "
                            "pausing uplink for %ds -- the module is not accepting "
                            "playback on this card",
                            self._tx_fail_streak, int(TX_START_FAIL_QUIET))
                    if self._tx_fail_streak >= TX_START_FAIL_LIMIT:
                        self._tx_retry_after = time.monotonic() + TX_START_FAIL_QUIET
                else:
                    self._tx_fail_streak = 0
                self._aplay = None
            if self._aplay is None and time.monotonic() < self._tx_retry_after:
                return True          # dropped on the floor, quietly, for now
            if self._aplay is None:
                try:
                    self._aplay = subprocess.Popen(
                        ["aplay", "-q", "-D", self._device(),
                         "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw",
                         # 120ms buffer / 20ms periods: aplay makes
                         # start_threshold and stop_threshold the buffer size,
                         # so this is both how long the first word is delayed
                         # and how much silence an underrun costs. See the
                         # note on TX_PREROLL_BYTES. The browser posts every
                         # 40ms, comfortably inside this.
                         "--buffer-time=120000", "--period-time=20000"],
                        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                except OSError as e:
                    _log().error("call-audio: cannot start aplay: %s", e)
                    self._aplay = None
                    return False
                _drain_stderr(self._aplay, "aplay")
                self._tx_started_at = time.monotonic()
                _log().info("call-audio: uplink sink started")
                self._tx_q.clear()
                self._tx_queued = 0
                self._start_writer_locked()
                self._start_reaper_locked()

            now = time.monotonic()
            if self._tx_t0 is None or now - self._last_tx > TX_IDLE_TIMEOUT:
                self._tx_t0 = now
                self._tx_bytes = 0
                try:
                    if self._tx_tap:
                        self._tx_tap.close()
                    self._tx_tap = open(DEBUG_TX_FILE, "wb")
                except OSError:
                    self._tx_tap = None
            self._tx_t_last = now
            self._tx_bytes += len(data)
            if self._tx_tap and self._tx_bytes <= DEBUG_CAP:
                self._tx_tap.write(data)
                self._tx_tap.flush()
            self._last_tx = now

        self._rec_feed_tx(data)

        with self._tx_cv:
            self._tx_q.append(data)
            self._tx_queued += len(data)
            # Bound added latency: if the browser ever runs ahead of the wire,
            # throw away the oldest audio rather than drift further behind.
            while self._tx_queued > TX_MAX_QUEUE_BYTES and self._tx_q:
                self._tx_queued -= len(self._tx_q.popleft())
            self._tx_cv.notify()
        return True

    def _start_writer_locked(self):
        if self._tx_writer is not None and self._tx_writer.is_alive():
            return
        self._tx_running = True
        proc = self._aplay
        silence = b"\x00" * TX_SILENCE_BYTES

        def write_loop():
            # Pre-roll so the first burst does not start a stream that is
            # already starving.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with self._tx_cv:
                    if self._tx_queued >= TX_PREROLL_BYTES or not self._tx_running:
                        break
                    self._tx_cv.wait(0.05)
            while True:
                with self._tx_cv:
                    if not self._tx_running or proc is not self._aplay:
                        return
                    if not self._tx_q:
                        # Wait for the next POST rather than papering over the
                        # gap with silence; the ALSA buffer covers us meanwhile.
                        self._tx_cv.wait(TX_WAIT_TIMEOUT)
                    if not self._tx_running or proc is not self._aplay:
                        return
                    if self._tx_q:
                        chunk = self._tx_q.popleft()
                        self._tx_queued -= len(chunk)
                    else:
                        chunk = silence   # genuine idle, keep the sink alive
                try:
                    proc.stdin.write(chunk)   # blocks when ALSA is full = pacing
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    return

        self._tx_writer = threading.Thread(target=write_loop, daemon=True)
        self._tx_writer.start()

    def _start_reaper_locked(self):
        """aplay is only ever stopped explicitly or after TX_IDLE_TIMEOUT of
        silence; a browser that navigates away mid-call must not leave a
        process holding the playback device open forever."""
        if self._reaper is not None and self._reaper.is_alive():
            return

        def reap():
            while True:
                time.sleep(2.0)
                with self._lock:
                    if self._aplay is None:
                        return
                    if time.monotonic() - self._last_tx > TX_IDLE_TIMEOUT:
                        self._kill_aplay_locked()
                        return

        self._reaper = threading.Thread(target=reap, daemon=True)
        self._reaper.start()

    # ----------------------------------------------------------- recording

    def start_record(self, number=""):
        """Open a WAV file and start capturing the call. Returns the file
        name, or the current one if a recording is already running."""
        with self._rec_lock:
            if self._rec_file is not None:
                return self._rec_name
            try:
                os.makedirs(RECORD_DIR, exist_ok=True)
                self._prune_recordings_locked()
                safe_num = "".join(c for c in (number or "") if c.isdigit() or c == "+")[:20]
                name = time.strftime("call_%Y%m%d-%H%M%S")
                if safe_num:
                    name += "_" + safe_num
                name += ".wav"
                path = os.path.join(RECORD_DIR, name)
                f = open(path, "wb")
                f.write(_wav_header(0))
            except OSError as e:
                _log().error("call-audio: cannot start recording: %s", e)
                return None
            self._rec_file = f
            self._rec_name = name
            self._rec_bytes = 0
            self._rec_tx_pend = bytearray()
            _log().info("call-audio: recording to %s", name)
            return name

    def stop_record(self):
        """Close the recording. An empty file (audio never flowed) is
        removed rather than kept as a 44-byte husk; a real one is handed to
        ffmpeg for Opus compression in the background."""
        with self._rec_lock:
            if self._rec_file is None:
                return
            f, name, nbytes = self._rec_file, self._rec_name, self._rec_bytes
            self._rec_file = None
            self._rec_name = None
            self._rec_tx_pend = bytearray()
            try:
                f.seek(0)
                f.write(_wav_header(nbytes))
                f.close()
            except OSError:
                pass
            if nbytes == 0:
                try:
                    os.unlink(os.path.join(RECORD_DIR, name))
                    _log().info("call-audio: recording %s empty, removed", name)
                    return
                except OSError:
                    pass
            _log().info("call-audio: recording %s closed (%.1fs)",
                        name, nbytes / 2.0 / RATE)
        self._compress_async(name)

    def _compress_async(self, name):
        """WAV -> Opus in a background thread. Voice at 16k mono compresses
        ~60x (24kbps VoIP profile vs 256kbps raw) with no audible loss for
        speech. The WAV is only removed once ffmpeg succeeds, so a missing or
        failing encoder simply leaves the recording as WAV."""
        import shutil
        if shutil.which("ffmpeg") is None:
            return

        def run():
            src = os.path.join(RECORD_DIR, name)
            dst = os.path.join(RECORD_DIR, name[:-4] + ".opus")
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                     "-c:a", "libopus", "-b:a", "24k", "-vbr", "on",
                     "-application", "voip", dst],
                    timeout=600, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE)
                if r.returncode == 0 and os.path.getsize(dst) > 0:
                    os.unlink(src)
                    _log().info("call-audio: compressed %s -> opus", name)
                else:
                    try:
                        os.unlink(dst)
                    except OSError:
                        pass
                    _log().error("call-audio: opus encode of %s failed: %s",
                                 name, r.stderr.decode(errors="replace")[:200])
            except Exception as e:
                _log().error("call-audio: opus encode of %s failed: %s", name, e)

        threading.Thread(target=run, daemon=True).start()

    def record_status(self):
        with self._rec_lock:
            return {
                "recording": self._rec_file is not None,
                "file": self._rec_name,
                "seconds": round(self._rec_bytes / 2.0 / RATE, 1),
            }

    def _prune_recordings_locked(self):
        try:
            # only call/voicemail recordings count -- never the voicemail
            # greeting or beep assets that live in the same directory
            names = [n for n in os.listdir(RECORD_DIR)
                     if (n.startswith("call_") or n.startswith("vm_"))
                     and (n.endswith(".wav") or n.endswith(".opus"))]
            if len(names) < RECORD_MAX_FILES:
                return
            names.sort()   # filenames start with the timestamp: oldest first
            for n in names[:len(names) - RECORD_MAX_FILES + 1]:
                try:
                    os.unlink(os.path.join(RECORD_DIR, n))
                except OSError:
                    pass
        except OSError:
            pass

    def _rec_feed_rx(self, data):
        """Downlink tap: the wire clock. Mixes in any pending mic audio."""
        with self._rec_lock:
            if self._rec_file is None:
                return
            if self._rec_bytes >= RECORD_MAX_BYTES:
                return
            n = min(len(self._rec_tx_pend), len(data)) & ~1
            if n:
                data = _mix_s16(data, bytes(self._rec_tx_pend[:n]))
                del self._rec_tx_pend[:n]
            try:
                self._rec_file.write(data)
                self._rec_bytes += len(data)
            except OSError:
                pass

    def _rec_feed_tx(self, data):
        """Uplink tap: buffer mic audio until the downlink clock consumes it."""
        with self._rec_lock:
            if self._rec_file is None:
                return
            self._rec_tx_pend += data
            if len(self._rec_tx_pend) > REC_TX_PEND_MAX:
                del self._rec_tx_pend[:len(self._rec_tx_pend) - REC_TX_PEND_MAX]

    # ------------------------------------------------------------- control

    def stop(self):
        with self._lock:
            self._rx_generation += 1
            self._kill_arecord_locked()
            self._kill_aplay_locked()
        self.stop_record()

    def status(self):
        with self._lock:
            st = {
                "card_present": self.card_present(),
                "rx_active": self._arecord is not None
                             and self._arecord.poll() is None,
                "tx_active": self._aplay is not None
                             and self._aplay.poll() is None,
            }
            # Wire-rate measurement: bytes actually delivered over wall time.
            # In clock-consumer mode the modem sets the true pace no matter
            # what rate ALSA negotiated, so this is the ground truth for any
            # pitch mismatch (S16 mono: rate = bytes / 2 / seconds).
            if self._rx_t0 is not None and self._rx_t_last is not None \
                    and self._rx_t_last - self._rx_t0 > 1.0:
                dur = self._rx_t_last - self._rx_t0
                st["rx_seconds"] = round(dur, 2)
                st["rx_bytes"] = self._rx_bytes
                st["rx_measured_rate"] = int(self._rx_bytes / 2 / dur)
            if self._tx_t0 is not None and self._tx_t_last is not None \
                    and self._tx_t_last - self._tx_t0 > 1.0:
                dur = self._tx_t_last - self._tx_t0
                st["tx_seconds"] = round(dur, 2)
                st["tx_bytes"] = self._tx_bytes
                st["tx_nominal_rate"] = int(self._tx_bytes / 2 / dur)
            return st

    # ------------------------------------------------------------ internal

    def _kill_arecord_locked(self):
        if self._arecord is not None:
            if self._arecord.poll() is None:
                self._arecord.kill()
            self._arecord = None

    def _kill_aplay_locked(self):
        with self._tx_cv:
            self._tx_running = False
            self._tx_q.clear()
            self._tx_queued = 0
            self._tx_cv.notify_all()
        if self._aplay is not None:
            try:
                if self._aplay.stdin:
                    self._aplay.stdin.close()
            except OSError:
                pass
            if self._aplay.poll() is None:
                self._aplay.kill()
            self._aplay = None


call_audio_bridge = CallAudioBridge()
