"""Server→client event fan-out over short polling.

This replaces flask_socketio, which could not be served safely from this app.
granian runs WSGI handlers on a fixed pool of --blocking-threads (4 on the
device), and websockets are force-disabled under WSGI, so socket.io fell back
to XHR long-polling: engine.io holds each poll open until the next PING, one
full ping_interval (25s). The client re-polls immediately, so *every connected
client permanently occupied one of the four request slots*. Four open tabs
meant no thread was ever free and every other request -- including a bare 302
-- waited out a ping cycle. That is issue #113's 18-24s freeze, and it is why
the transport here must be short polls that return immediately.

The model is a sequence-numbered ring buffer. Publishers append and never
block; each client polls with the last sequence number it saw and gets
everything newer. No request is ever held open, so N watching clients cost N
short requests per second rather than N permanently-blocked threads.

Events published with a `room` go only to clients that have joined it (the
terminal's per-session PTY output); `room=None` is a broadcast, matching what
socketio.emit() did before.

Capacity is finite by design: a client that falls far enough behind for its
events to be evicted is told to resync rather than shown a silently truncated
stream. That matters for the firmware-upgrade log, where a missing chunk
would otherwise read as a stalled upgrade.
"""

import threading
from collections import deque

# ~2000 events is several minutes of the busiest stream we have (opkg install
# log lines, a few per second) at the 1s client poll cadence, and costs well
# under a megabyte. Overflow is reported, not hidden, so the ceiling is a
# resync trigger rather than data loss.
DEFAULT_CAPACITY = 2000


class EventBus:
    """Fan-out buffer shared by all polling clients. Thread-safe."""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        self._lock = threading.Lock()
        self._events = deque(maxlen=capacity)
        self._seq = 0
        # Mirrors of the stream (the ASGI entry forwards every publish into
        # socket.io rooms so websocket clients get pushes instead of polls).
        # Called outside the lock: a listener that blocks must not stall
        # publishers, and a listener that publishes must not deadlock.
        self._listeners = []

    def add_listener(self, fn):
        """Register fn(event, data, room) to observe every publish."""
        self._listeners.append(fn)

    def publish(self, event, data=None, room=None):
        """Append an event for every (or one room's) polling client.

        Never blocks and never raises on a full buffer -- the oldest event is
        evicted instead, because a publisher here is usually a background
        worker mid-firmware-upgrade and must not be stalled or killed by the
        state of some browser's poll loop.
        """
        with self._lock:
            self._seq += 1
            self._events.append({
                "seq": self._seq,
                "event": event,
                "data": {} if data is None else data,
                "room": room,
            })
            seq = self._seq
        for fn in list(self._listeners):
            try:
                fn(event, data, room)
            except Exception:
                # An observer must never be able to break the bus itself.
                pass
        return seq

    def cursor(self):
        """The current sequence number, i.e. where a fresh client starts.

        New clients start here rather than at 0 so opening a page does not
        replay a firmware log from ten minutes ago -- the same "you only see
        what happens after you connect" rule socket.io had.
        """
        with self._lock:
            return self._seq

    def poll(self, since, rooms=()):
        """Return (cursor, events, resync) for everything after `since`.

        resync is True when `since` is older than the buffer still holds, so
        events have been evicted unseen; the caller should reload its state
        rather than trust the partial list.
        """
        joined = set(rooms or ())
        with self._lock:
            cursor = self._seq
            if since is None or since >= cursor:
                # Also covers a client whose cursor is ahead of ours, which
                # means the app restarted and the sequence began again.
                return cursor, [], since is not None and since > cursor
            oldest = self._events[0]["seq"] if self._events else cursor + 1
            resync = since + 1 < oldest
            out = [
                {"seq": e["seq"], "event": e["event"], "data": e["data"]}
                for e in self._events
                if e["seq"] > since and (e["room"] is None or e["room"] in joined)
            ]
        return cursor, out, resync


# The process-wide bus. One granian worker, one App process, so one instance.
bus = EventBus()

publish = bus.publish
poll = bus.poll
cursor = bus.cursor
