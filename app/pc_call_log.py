"""
CallSessionTracker - one call history row per call, with a real duration.

What was here before logged a row per AT command: "outgoing" the moment ATD
was sent, "incoming"/"Unknown" when ATA returned, and an "ended"/"Unknown" row
on hangup that silently failed the table's CHECK constraint every time. So a
call produced a row before anyone knew whether it connected, incoming calls
never carried the caller's number, missed calls were never recorded at all,
and `duration` was never written -- the column and the UI that displays it
have always been there, always empty.

A call is instead recorded when it *ends*, because only then are the two facts
worth storing actually known: whether it connected, and for how long.

Two inputs, and the difference matters for accuracy:

  the AT+CLCC snapshot from each status poll, which is what tells us a call
  exists, who it is with, and when it goes from dialing/alerting to connected;

  the modem's URCs, which end a call the moment the network says so rather
  than up to one poll interval later. Hangups are the common case and land on
  NO CARRIER, so this is most of the duration accuracy.

The elapsed time is also served from here rather than measured in the browser,
so the on-screen timer starts from the instant the call connected instead of
from whenever a poll happened to notice, and survives a page reload.
"""

import threading
import time

from pcat_util import pcat_logger

# 27.007 <stat>: 0 active, 1 held, 2 dialing, 3 alerting, 4 incoming, 5 waiting
_CONNECTED = "active"
_RINGING = ("dialing", "alerting", "incoming", "waiting")


def _log():
    return pcat_logger.logger


class CallSessionTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._get_sms = None
        self._call = None       # the call in progress, if any

    def start(self, get_sms_client):
        """get_sms_client: zero-arg callable returning the live sms client (or
        None while the modem stack is still starting)."""
        self._get_sms = get_sms_client

    # --------------------------------------------------------------- inputs

    def on_status(self, calls):
        """Fed the parsed AT+CLCC list on every status poll (voice calls only
        -- see pc_sms_client.VOICE_CALL_MODES)."""
        now = time.monotonic()
        with self._lock:
            if not calls:
                self._finish_locked(now)
                return
            c = calls[0]
            if self._call is None:
                self._call = {
                    "number": c.get("number") or "",
                    "direction": c.get("direction") or "outgoing",
                    "started": now,
                    "connected": None,
                }
            # A number can arrive later than the call itself: an incoming call
            # is announced before +CLIP delivers the caller ID.
            if not self._call["number"] and c.get("number"):
                self._call["number"] = c["number"]
            if c.get("status") == _CONNECTED and self._call["connected"] is None:
                self._call["connected"] = now

    def on_urc(self, line):
        """Fed every unsolicited modem line. Runs on the serial threads, so it
        must not block -- the database write is the only real work and happens
        once per call."""
        if line.startswith("RING") or line.startswith("+CRING:"):
            # Open the session here, not on the first status poll: an incoming
            # call rings whether or not the phone page is open, and a call
            # nobody was watching is exactly the one worth having in the list.
            if line.startswith("+CRING:") and "VOICE" not in line.upper():
                return
            with self._lock:
                if self._call is None:
                    self._call = {"number": "", "direction": "incoming",
                                  "started": time.monotonic(),
                                  "connected": None}
        elif line.startswith(("NO CARRIER", "BUSY", "NO ANSWER")):
            with self._lock:
                self._finish_locked(time.monotonic())
        elif line.startswith("+CLIP:"):
            try:
                number = line.split('"')[1]
            except (IndexError, ValueError):
                return
            with self._lock:
                if self._call is not None and not self._call["number"]:
                    self._call["number"] = number

    def note_number(self, number):
        """Called when ATD is accepted. This opens the session rather than just
        naming it: nothing else knows about an outgoing call yet, and a call
        that is dialled and rejected before the first status poll would
        otherwise never be recorded -- or be recorded without the number."""
        if not number:
            return
        with self._lock:
            if self._call is None:
                self._call = {"number": number, "direction": "outgoing",
                              "started": time.monotonic(), "connected": None}
            elif not self._call["number"]:
                self._call["number"] = number

    def finish(self):
        """Called when we hang up ourselves. Hanging up with ATH does not
        reliably produce a NO CARRIER to end the session on, and the status
        poll that would otherwise notice only runs while the phone page is
        open -- so without this a call the user ended could go unrecorded."""
        with self._lock:
            self._finish_locked(time.monotonic())

    # --------------------------------------------------------------- output

    def call_seconds(self):
        """Seconds since the call connected, or 0 when nothing is connected.
        The browser's timer reads this instead of counting from the poll that
        first noticed the call."""
        with self._lock:
            if self._call is None or self._call["connected"] is None:
                return 0
            return int(time.monotonic() - self._call["connected"])

    # ---------------------------------------------------------------- write

    def _finish_locked(self, now):
        call = self._call
        self._call = None
        if call is None:
            return
        connected = call["connected"]
        seconds = int(now - connected) if connected is not None else 0
        incoming = call["direction"] == "incoming"
        # An incoming call that never connected is a missed call. An outgoing
        # one that never connected is still an outgoing call -- the user made
        # it, it just was not picked up.
        kind = "missed" if (incoming and connected is None) else \
               ("incoming" if incoming else "outgoing")
        sms = self._get_sms() if self._get_sms else None
        if sms is None:
            return
        try:
            sms.log_call_history(kind, call["number"] or "Unknown",
                                 "completed" if connected else "no_answer",
                                 duration=seconds)
        except Exception as e:
            _log().error("call-log: could not record call: %s", e)


call_session_tracker = CallSessionTracker()
