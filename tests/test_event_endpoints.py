#!/usr/bin/env python3
"""HTTP-layer tests for the event-bus endpoints in app/pcat_manager_web.py.

These are the two routes that replaced socket.io (issue #113):

    GET  /api/v1/events.json        server -> client, short poll
    POST /api/v1/events/emit.json   client -> server commands

tests/test_event_bus.py covers the buffer itself; what is asserted here is the
part that only exists once the buffer is behind Flask -- the auth gate, the
CSRF guard that the old /socket.io transport was exempt from, room isolation
between two terminals, and that a caller-only reply never reaches the
broadcast stream.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_event_endpoints`.
"""

import logging
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# Host-only imports pcat_manager_web pulls in at module scope. None of them are
# reached by these tests, but the module will not import without them present.
for _absent in ("pam", "serial", "serial.tools", "serial.tools.list_ports"):
    if _absent not in sys.modules:
        _mod = types.ModuleType(_absent)
        _mod.__getattr__ = lambda name: types.SimpleNamespace()
        sys.modules[_absent] = _mod

import pcat_util  # noqa: E402  -- real package, so siblings still resolve
if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_event_endpoints")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

import pcat_manager_web as web  # noqa: E402

CSRF = "test-csrf-token"
HEADERS = {"X-CSRF-Token": CSRF}


class EventEndpointTestCase(unittest.TestCase):
    def setUp(self):
        web.flask_app.config["TESTING"] = True
        self._skip_auth = web.pcat_config.skip_auth
        web.pcat_config.skip_auth = False
        self.client = web.flask_app.test_client()

    def tearDown(self):
        web.pcat_config.skip_auth = self._skip_auth

    def login(self):
        with self.client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["_csrf_token"] = CSRF

    def anonymous_with_csrf(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF


class AuthTests(EventEndpointTestCase):
    def test_anonymous_poll_is_refused(self):
        self.assertEqual(self.client.get("/api/v1/events.json").status_code, 403)

    def test_anonymous_emit_is_refused(self):
        self.anonymous_with_csrf()
        resp = self.client.post("/api/v1/events/emit.json",
                                json={"event": "terminal_kill", "data": {}},
                                headers=HEADERS)
        self.assertEqual(resp.status_code, 403)

    def test_emit_without_a_csrf_token_is_refused(self):
        """The old /socket.io transport was CSRF-exempt; this one is not."""
        self.login()
        resp = self.client.post("/api/v1/events/emit.json",
                                json={"event": "terminal_kill", "data": {}})
        self.assertEqual(resp.status_code, 400)

    def test_the_startup_audit_does_not_flag_these_routes(self):
        flagged = [endpoint for endpoint, _methods in web.audit_route_auth()]
        self.assertNotIn("events_poll_action", flagged)
        self.assertNotIn("events_emit_action", flagged)


class PollTests(EventEndpointTestCase):
    def test_a_fresh_client_gets_a_cursor_and_no_backlog(self):
        web.publish("log_response", {"data": "from before this client existed"})
        self.login()
        body = self.client.get("/api/v1/events.json").get_json()
        self.assertEqual(body["events"], [])
        self.assertFalse(body["resync"])
        self.assertIsInstance(body["cursor"], int)

    def test_published_events_are_delivered_after_the_cursor(self):
        self.login()
        start = self.client.get("/api/v1/events.json").get_json()["cursor"]
        web.publish("nas_job_status", {"id": "job1", "state": "running"})

        body = self.client.get("/api/v1/events.json?since=%d" % start).get_json()
        self.assertEqual([e["event"] for e in body["events"]], ["nas_job_status"])
        self.assertEqual(body["events"][0]["data"]["state"], "running")

    def test_a_malformed_since_is_treated_as_a_fresh_client(self):
        self.login()
        body = self.client.get("/api/v1/events.json?since=banana").get_json()
        self.assertEqual(body["events"], [])


class RoomTests(EventEndpointTestCase):
    def test_terminal_output_is_withheld_unless_the_room_was_joined(self):
        self.login()
        start = self.client.get("/api/v1/events.json").get_json()["cursor"]
        web.publish("terminal_output", {"data": "secret"}, room="terminal:a")

        body = self.client.get("/api/v1/events.json?since=%d" % start).get_json()
        self.assertEqual(body["events"], [])

        body = self.client.get(
            "/api/v1/events.json?since=%d&rooms=terminal:a" % start).get_json()
        self.assertEqual([e["data"]["data"] for e in body["events"]], ["secret"])

    def test_another_terminals_room_does_not_match(self):
        self.login()
        start = self.client.get("/api/v1/events.json").get_json()["cursor"]
        web.publish("terminal_output", {"data": "secret"}, room="terminal:a")

        body = self.client.get(
            "/api/v1/events.json?since=%d&rooms=terminal:b" % start).get_json()
        self.assertEqual(body["events"], [])


class EmitTests(EventEndpointTestCase):
    def test_a_command_dispatches_and_replies_to_its_caller_only(self):
        self.login()
        resp = self.client.post("/api/v1/events/emit.json",
                                json={"event": "client_connected", "data": {}},
                                headers=HEADERS)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["replies"][0]["event"], "connected_response")

        # ...and that reply is not in the buffer every other client polls.
        stream = self.client.get(
            "/api/v1/events.json?since=%d" % body["join_from"]).get_json()
        self.assertTrue(all(e["event"] != "connected_response"
                            for e in stream["events"]))

    def test_an_unknown_command_is_rejected(self):
        self.login()
        resp = self.client.post("/api/v1/events/emit.json",
                                json={"event": "no_such_event"}, headers=HEADERS)
        self.assertEqual(resp.status_code, 404)

    def test_join_from_precedes_the_dispatch(self):
        """The client rewinds to this to catch output a handler produced for a
        room it had not joined yet -- the terminal's first shell prompt."""
        self.login()
        before = self.client.get("/api/v1/events.json").get_json()["cursor"]
        resp = self.client.post("/api/v1/events/emit.json",
                                json={"event": "client_connected", "data": {}},
                                headers=HEADERS)
        self.assertLessEqual(resp.get_json()["join_from"], before + 1)

    def test_dispatch_scratch_does_not_leak_between_requests(self):
        """Handlers run on a shared pool of granian threads; a reply left
        behind would be collected by whoever lands on that thread next."""
        self.login()
        for _ in range(3):
            body = self.client.post("/api/v1/events/emit.json",
                                    json={"event": "client_connected", "data": {}},
                                    headers=HEADERS).get_json()
            self.assertEqual(len(body["replies"]), 1)
            self.assertEqual(body["join"], [])


if __name__ == "__main__":
    unittest.main()
