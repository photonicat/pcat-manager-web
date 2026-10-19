#!/usr/bin/env python3
"""Unit tests for the polling event bus (app/pcat_util/event_bus.py).

This is the replacement for flask_socketio. It exists because socket.io's
long-poll permanently held one of granian's four --blocking-threads slots per
connected client, which froze the whole UI (issue #113), so the property that
matters most here is the one asserted in PollNeverBlocksTests: a poll returns
immediately whether or not there is anything to deliver.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_event_bus`.
"""

import os
import sys
import threading
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

from pcat_util.event_bus import EventBus  # noqa: E402


class CursorTests(unittest.TestCase):
    def test_new_client_starts_at_the_end_and_sees_no_history(self):
        """Opening a page must not replay an old firmware log at the user."""
        bus = EventBus()
        bus.publish("log_response", {"data": "old line"})
        start = bus.cursor()

        cursor, events, resync = bus.poll(start)
        self.assertEqual(events, [])
        self.assertFalse(resync)
        self.assertEqual(cursor, start)

    def test_events_arrive_in_order_after_the_cursor(self):
        bus = EventBus()
        start = bus.cursor()
        bus.publish("a", {"n": 1})
        bus.publish("b", {"n": 2})

        cursor, events, _ = bus.poll(start)
        self.assertEqual([e["event"] for e in events], ["a", "b"])
        self.assertEqual([e["data"]["n"] for e in events], [1, 2])
        self.assertEqual(cursor, start + 2)

    def test_polling_twice_does_not_redeliver(self):
        bus = EventBus()
        start = bus.cursor()
        bus.publish("a")
        cursor, events, _ = bus.poll(start)
        self.assertEqual(len(events), 1)

        cursor2, events2, _ = bus.poll(cursor)
        self.assertEqual(events2, [])
        self.assertEqual(cursor2, cursor)

    def test_publish_returns_the_sequence_number(self):
        bus = EventBus()
        first = bus.publish("a")
        self.assertEqual(bus.publish("b"), first + 1)


class RoomTests(unittest.TestCase):
    def test_room_events_are_withheld_from_clients_that_did_not_join(self):
        """Two open terminals must not read each other's shells."""
        bus = EventBus()
        start = bus.cursor()
        bus.publish("terminal_output", {"data": "secret"}, room="terminal:a")

        _, events, _ = bus.poll(start, rooms=[])
        self.assertEqual(events, [])
        _, events, _ = bus.poll(start, rooms=["terminal:b"])
        self.assertEqual(events, [])
        _, events, _ = bus.poll(start, rooms=["terminal:a"])
        self.assertEqual([e["data"]["data"] for e in events], ["secret"])

    def test_broadcasts_reach_a_client_that_is_in_a_room(self):
        bus = EventBus()
        start = bus.cursor()
        bus.publish("nas_disks_changed", {"appeared": []})
        bus.publish("terminal_output", {"data": "x"}, room="terminal:a")

        _, events, _ = bus.poll(start, rooms=["terminal:a"])
        self.assertEqual([e["event"] for e in events],
                         ["nas_disks_changed", "terminal_output"])

    def test_room_is_not_leaked_to_the_client(self):
        bus = EventBus()
        start = bus.cursor()
        bus.publish("terminal_output", {"data": "x"}, room="terminal:a")
        _, events, _ = bus.poll(start, rooms=["terminal:a"])
        self.assertNotIn("room", events[0])


class OverflowTests(unittest.TestCase):
    def test_a_client_that_falls_behind_is_told_to_resync(self):
        """Silently truncating the upgrade log would read as a stalled upgrade."""
        bus = EventBus(capacity=4)
        start = bus.cursor()
        for i in range(10):
            bus.publish("log_response", {"n": i})

        _, events, resync = bus.poll(start)
        self.assertTrue(resync)
        # Only what the buffer still holds, and nothing invented.
        self.assertEqual([e["data"]["n"] for e in events], [6, 7, 8, 9])

    def test_keeping_up_never_reports_resync(self):
        bus = EventBus(capacity=4)
        cursor = bus.cursor()
        for i in range(10):
            bus.publish("log_response", {"n": i})
            cursor, events, resync = bus.poll(cursor)
            self.assertFalse(resync)
            self.assertEqual([e["data"]["n"] for e in events], [i])

    def test_a_restarted_app_is_reported_as_resync(self):
        """The client's cursor outlives the process; a restart rewinds seq to 0."""
        bus = EventBus()
        _, events, resync = bus.poll(500)
        self.assertEqual(events, [])
        self.assertTrue(resync)


class PollNeverBlocksTests(unittest.TestCase):
    """The regression guard for issue #113.

    socket.io held each poll open for a full ping_interval, so N clients pinned
    N of granian's 4 request threads and everything else queued. Any future
    change that makes poll() wait for an event reintroduces that freeze.
    """

    def test_poll_returns_immediately_when_there_is_nothing_to_send(self):
        bus = EventBus()
        cursor = bus.cursor()
        started = time.monotonic()
        for _ in range(50):
            bus.poll(cursor)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5,
                        "poll() blocked; it must never hold a request thread")

    def test_publish_does_not_block_on_a_full_buffer(self):
        bus = EventBus(capacity=8)
        started = time.monotonic()
        for i in range(1000):
            bus.publish("log_response", {"n": i})
        self.assertLess(time.monotonic() - started, 1.0)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_publishers_and_pollers_do_not_corrupt_the_buffer(self):
        bus = EventBus(capacity=5000)
        errors = []

        def publisher(tag):
            try:
                for i in range(200):
                    bus.publish("e", {"tag": tag, "n": i})
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def poller():
            try:
                cursor = 0
                for _ in range(200):
                    cursor, _events, _ = bus.poll(cursor)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=publisher, args=(t,)) for t in range(4)]
        threads += [threading.Thread(target=poller) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        # 4 publishers x 200 events, sequence numbers dense and unique.
        self.assertEqual(bus.cursor(), 800)
        _, events, _ = bus.poll(0)
        self.assertEqual(len(events), 800)
        self.assertEqual(len({e["seq"] for e in events}), 800)


if __name__ == "__main__":
    unittest.main()
