"""
RingerService - buzz the MCU buzzer while an incoming call rings.

Latency design (why this is event-driven, not polled): the modem announces an
incoming call by writing "RING" on its AT port the moment the network pages
it -- that URC byte stream is the earliest signal that exists on this
hardware, for any language. pc_modem_client surfaces every unsolicited line
through its URC dispatch: immediately when a queued command happens to be
reading the port, and within ~100ms otherwise via its idle-drain thread. So
the buzzer starts within ~150ms of the modem's first RING; a C or Rust
implementation blocking on the same tty could not start meaningfully earlier,
because the RING itself is the bottleneck.

While ringing, the melody loops on the MCU (pmu-beep-play loop=1): the host
does nothing per ring cycle. Stops are instant where it matters: answering or
declining in the UI calls stop() before the ATA/ATH is even sent; a caller
who gives up produces NO CARRIER which stops within the drain interval; and a
watchdog silences the buzzer if RING repetitions cease for RING_TIMEOUT
(ringing modems re-RING every few seconds).
"""

import threading
import time

import pcat_config
from pcat_util import pcat_logger

# Ring melodies for the MCU buzzer. Each is one ring cycle <= 20 steps
# ({"hz","ms"}, hz 0 = rest); the MCU loops the whole cycle, rest included,
# until a new sequence interrupts it. All pitched high (2.6-4.2kHz) where the
# little piezo actually carries.
RING_MELODIES = {
    "classic": (
        [{"hz": h, "ms": 55} for h in [2700, 3400] * 8]
        + [{"hz": 0, "ms": 2000}]),
    "digital": [
        {"hz": 2637, "ms": 110}, {"hz": 0, "ms": 25},
        {"hz": 3136, "ms": 110}, {"hz": 0, "ms": 25},
        {"hz": 3520, "ms": 110}, {"hz": 0, "ms": 25},
        {"hz": 4186, "ms": 170}, {"hz": 0, "ms": 150},
        {"hz": 2637, "ms": 110}, {"hz": 0, "ms": 25},
        {"hz": 3136, "ms": 110}, {"hz": 0, "ms": 25},
        {"hz": 3520, "ms": 110}, {"hz": 0, "ms": 25},
        {"hz": 4186, "ms": 170}, {"hz": 0, "ms": 1800},
    ],
    "chirp": [
        {"hz": 3520, "ms": 180}, {"hz": 2794, "ms": 180}, {"hz": 0, "ms": 120},
        {"hz": 3520, "ms": 180}, {"hz": 2794, "ms": 180}, {"hz": 0, "ms": 2000},
    ],
    "trill": (
        [{"hz": h, "ms": 45} for h in [3000, 3600] * 8]
        + [{"hz": 0, "ms": 1200}]),
}
DEFAULT_MELODY = "classic"
RING_STOP = [{"hz": 0, "ms": 1}]
RING_TIMEOUT = 7.0    # no RING for this long => ringing is over


def _log():
    return pcat_logger.logger


class RingerService:
    def __init__(self):
        self._get_socket = None
        self._lock = threading.Lock()
        self._ringing = False        # buzzer is sounding
        # A call is ringing, tracked separately from _ringing: the buzzer can
        # be switched off in the UI, and other subscribers (voicemail) still
        # need to know a call is coming in. This is the only always-on
        # incoming-call signal there is once the browser is closed.
        self._ring_active = False
        # The user asked for quiet on this particular call. Kept separate from
        # the buzzer being off, because RING repeats every few seconds and
        # would otherwise just start it again.
        self._silenced = False
        self._last_ring = 0.0
        self._watchdog = None
        self.incoming_number = None

    def start(self, get_socket_client):
        """get_socket_client: zero-arg callable returning the live
        pc_socket_client (or None while it is still connecting)."""
        self._get_socket = get_socket_client
        if self._watchdog is None:
            self._watchdog = threading.Thread(target=self._watch, daemon=True)
            self._watchdog.start()
            _log().info("ringer: armed")

    def enabled(self):
        return bool(pcat_config.get_user_config_key("phone_ring_buzzer_enabled", True))

    def melody(self):
        m = pcat_config.get_user_config_key("phone_ring_melody", DEFAULT_MELODY)
        return m if m in RING_MELODIES else DEFAULT_MELODY

    # ------------------------------------------------------------- events

    def on_urc(self, line):
        """Fed every unsolicited modem line by pc_modem_client. Runs on the
        serial threads -- must never block (beep_play only enqueues)."""
        # "+CRING: VOICE" is the same event as bare "RING" -- which of the two
        # the module emits is AT+CRC's doing, and RM500U-CNV ships with the
        # extended form on. Data/fax calls (+CRING: SYNC / FAX / ...) are not
        # ours to answer, so only VOICE rings.
        if line.startswith("RING") or line.startswith("+CRING:"):
            if line.startswith("+CRING:") and "VOICE" not in line.upper():
                return
            self._last_ring = time.monotonic()
            self._ring_active = True
            self._ring()
        elif line.startswith("+CLIP:"):
            try:
                self.incoming_number = line.split('"')[1]
            except (IndexError, ValueError):
                pass
        elif line.startswith(("NO CARRIER", "BUSY", "NO ANSWER")):
            self.stop()

    def _ring(self):
        if not self.enabled() or self._silenced:
            return
        with self._lock:
            if self._ringing:
                return
            sc = self._get_socket() if self._get_socket else None
            if sc is None:
                return
            try:
                sc.beep_play(RING_MELODIES[self.melody()], loop=True)
                self._ringing = True
                _log().info("ringer: ringing (%s)", self.melody())
            except Exception as e:
                _log().error("ringer: start failed: %s", e)

    def silence(self):
        """Stop the buzzer but leave the call ringing. The caller still hears
        ringback, voicemail still answers on schedule, and is_ringing() stays
        true -- this only means "stop making noise at me", not "hang up". The
        flag is what makes it stick: RING repeats every few seconds and would
        otherwise start the buzzer straight back up."""
        with self._lock:
            self._silenced = True
            if not self._ringing:
                return
            self._ringing = False
            sc = self._get_socket() if self._get_socket else None
            if sc is None:
                return
            try:
                sc.beep_play(RING_STOP, loop=False)
                _log().info("ringer: silenced")
            except Exception as e:
                _log().error("ringer: silence failed: %s", e)

    def is_ringing(self):
        """True while an incoming call is ringing, whether or not the buzzer
        is switched on. Lets subscribers react to a call without polling the
        modem -- see the note on _ring_active."""
        return self._ring_active

    def stop(self):
        """Silence the buzzer and clear the ringing state. Safe to call from
        anywhere, any time."""
        with self._lock:
            self.incoming_number = None
            self._ring_active = False
            self._silenced = False        # next call rings again
            if not self._ringing:
                return
            self._ringing = False
            sc = self._get_socket() if self._get_socket else None
            if sc is None:
                return
            try:
                sc.beep_play(RING_STOP, loop=False)
                _log().info("ringer: stopped")
            except Exception as e:
                _log().error("ringer: stop failed: %s", e)

    def preview(self, melody):
        """Play one cycle of a melody (no loop) for the settings UI."""
        if melody not in RING_MELODIES:
            return False
        sc = self._get_socket() if self._get_socket else None
        if sc is None:
            return False
        sc.beep_play(RING_MELODIES[melody], loop=False)
        return True

    def _watch(self):
        while True:
            time.sleep(1.0)
            try:
                # keyed off _ring_active, not _ringing: with the buzzer
                # switched off nothing is sounding, but the ringing state
                # still has to expire or it would latch on forever
                if self._ring_active and \
                        time.monotonic() - self._last_ring > RING_TIMEOUT:
                    _log().info("ringer: RING ceased, silencing")
                    self.stop()
            except Exception as e:
                _log().error("ringer: watchdog: %s", e)


ringer_service = RingerService()
