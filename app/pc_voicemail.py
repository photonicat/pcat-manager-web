"""
VoicemailService - server-side answering machine.

When enabled, a daemon thread watches the modem for an incoming call that
nobody answers. After the configured ring time it answers the call itself,
plays the user's greeting (if one was recorded) followed by a beep into the
modem's PCM link, records whatever the caller says, hangs up, and stores the
message next to the call recordings (vm_*.wav -> .opus) where the phone page
lists it.

Runs entirely on the device -- no browser needs to be open. Audio uses the
same ALSA "modem" card as pc_call_audio, via its own short-lived
arecord/aplay processes; the in-call browser bridge is not involved. If the
phone page happens to be open while voicemail answers, its stream may fail to
grab the (busy) capture device -- harmless, the voicemail still records.
"""

import os
import subprocess
import threading
import time

from pcat_util import pcat_logger
import pcat_config
import pc_call_audio

RECORD_DIR = pc_call_audio.RECORD_DIR
GREETING_WAV = os.path.join(RECORD_DIR, "greeting.wav")
BEEP_WAV = os.path.join(RECORD_DIR, "beep.wav")
MAX_MESSAGE_SECS = 120
DEFAULT_ANSWER_SECS = 15
GREETING_MAX_SECS = 30


def _log():
    return pcat_logger.logger


def _device():
    # same card-selection logic as the in-call bridge (UAC card when present)
    return pc_call_audio.call_audio_bridge._device()


def _ensure_beep():
    """Generate the record-now beep once (800Hz, 0.8s) with the device's
    ffmpeg. Missing ffmpeg just means messages start without a beep."""
    if os.path.exists(BEEP_WAV):
        return
    try:
        os.makedirs(RECORD_DIR, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=800:duration=0.8",
             "-ar", str(pc_call_audio.RATE), "-ac", "1",
             "-c:a", "pcm_s16le", BEEP_WAV],
            timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _log().error("voicemail: beep generation failed: %s", e)


class VoicemailService:
    def __init__(self):
        self._get_sms = None
        self._thread = None
        self._ring_seen = None
        self.active = False        # a voicemail session is in progress
        self.active_file = None    # in-flight message wav (hidden from lists)

    def start(self, get_sms_client):
        """get_sms_client: zero-arg callable returning the live sms client
        (or None while the modem stack is still starting)."""
        self._get_sms = get_sms_client
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log().info("voicemail: monitor started")

    def enabled(self):
        return bool(pcat_config.get_user_config_key("phone_voicemail_enabled", False))

    def answer_secs(self):
        try:
            v = int(pcat_config.get_user_config_key(
                "phone_voicemail_answer_secs", DEFAULT_ANSWER_SECS))
        except (TypeError, ValueError):
            v = DEFAULT_ANSWER_SECS
        return min(max(v, 3), 60)

    # ------------------------------------------------------------- monitor

    def _loop(self):
        while True:
            time.sleep(2.0)
            try:
                self._tick()
            except Exception as e:
                _log().error("voicemail: tick failed: %s", e)
                time.sleep(5)

    def _tick(self):
        if not self.enabled() or self.active:
            self._ring_seen = None
            return
        # Gate on the RING URC rather than asking the modem every 2s. AT+CLCC
        # is not free: each command wakes the module's application processor,
        # and this loop runs whenever voicemail is enabled -- 30 wakeups a
        # minute, forever, on a battery-powered box. The URC is also strictly
        # earlier than any poll could be, since it is what the poll would be
        # racing to notice.
        import pc_ringer
        if not pc_ringer.ringer_service.is_ringing():
            self._ring_seen = None
            return
        sms = self._get_sms() if self._get_sms else None
        if sms is None:
            return
        try:
            inc = sms.get_incoming_call_status()
        except Exception:
            return
        if not inc or not inc.get("active"):
            self._ring_seen = None
            return
        now = time.monotonic()
        if self._ring_seen is None:
            self._ring_seen = now
            return
        if now - self._ring_seen < self.answer_secs():
            return
        number = inc.get("number") or ""
        self._ring_seen = None
        self._take_message(sms, number)

    # ------------------------------------------------------------- session

    def _call_alive(self, sms):
        try:
            st = sms.get_call_status()
            return len(st.get("calls", [])) > 0
        except Exception:
            return True   # benefit of the doubt; the duration cap bounds us

    def _take_message(self, sms, number):
        _log().info("voicemail: answering call from %s", number or "unknown")
        self.active = True
        arec = None
        path = None
        name = None
        try:
            # voicemail picking up counts as pickup: silence the ring buzzer
            import pc_ringer
            pc_ringer.ringer_service.stop()
            if not sms.answer_call():
                _log().error("voicemail: answer failed")
                return
            time.sleep(0.7)
            _ensure_beep()
            for f in (GREETING_WAV, BEEP_WAV):
                if os.path.exists(f) and self._call_alive(sms):
                    try:
                        subprocess.run(
                            ["aplay", "-q", "-D", _device(), f],
                            timeout=GREETING_MAX_SECS + 10,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception as e:
                        _log().error("voicemail: greeting playback failed: %s", e)
            if not self._call_alive(sms):
                _log().info("voicemail: caller hung up during greeting")
                return
            safe = "".join(c for c in number if c.isdigit() or c == "+")[:20]
            name = time.strftime("vm_%Y%m%d-%H%M%S")
            if safe:
                name += "_" + safe
            name += ".wav"
            path = os.path.join(RECORD_DIR, name)
            self.active_file = name
            arec = subprocess.Popen(
                ["arecord", "-q", "-D", _device(),
                 "-f", "S16_LE", "-r", str(pc_call_audio.RATE), "-c", "1",
                 "-t", "wav", "-d", str(MAX_MESSAGE_SECS), path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            t0 = time.monotonic()
            while arec.poll() is None:
                time.sleep(1.0)
                if time.monotonic() - t0 > MAX_MESSAGE_SECS + 10:
                    break
                if not self._call_alive(sms):
                    break
        finally:
            if arec is not None and arec.poll() is None:
                # arecord finalizes the WAV header on SIGTERM
                arec.terminate()
                try:
                    arec.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    arec.kill()
            try:
                sms.hangup_call()
            except Exception:
                pass
            self.active_file = None
            self.active = False
        try:
            if path and os.path.exists(path):
                # keep only messages with >0.5s of audio
                if os.path.getsize(path) > 44 + pc_call_audio.RATE:
                    pc_call_audio.call_audio_bridge._compress_async(name)
                    _log().info("voicemail: message from %s saved as %s",
                                number or "unknown", name)
                    try:
                        import pc_transcribe
                        pc_transcribe.transcribe_service.maybe_auto_enqueue(name)
                    except Exception as e:
                        _log().error("voicemail: auto-transcribe hook: %s", e)
                else:
                    os.unlink(path)
                    _log().info("voicemail: empty message from %s discarded",
                                number or "unknown")
        except OSError:
            pass


voicemail_service = VoicemailService()
