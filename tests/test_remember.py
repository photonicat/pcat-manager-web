#!/usr/bin/env python3
"""Unit tests for SQLite-backed remember-me tokens (app/remember_token.py).

Covers the token store in isolation plus an end-to-end Flask integration that
simulates the original bug: SECRET_KEY rotation across reboot. A persistent
remember-me cookie + sqlite row must keep the user logged in.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_remember`.
"""

import logging
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import timedelta
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# tests/run_tests.py wraps the suite in a patch.dict mocking `pcat_config`,
# which makes the real pcat_util.pcat_logger crash at import (RotatingFileHandler
# receives a MagicMock filename). We import the real pcat_util PACKAGE so that
# sibling submodules (openwrt etc., needed by other tests) still resolve via
# the package's __path__, then graft a lightweight pcat_logger stub on top --
# remember_token only calls logger.warning().
import pcat_util  # noqa: E402  -- real package, empty __init__.py
if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_remember")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

import remember_token  # noqa: E402


class RememberTokenStoreTests(unittest.TestCase):
    """The pure DB layer: issue / validate / revoke / parse_cookie."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="remember-test-")
        self._orig_path = remember_token.REMEMBER_DB_PATH
        remember_token.REMEMBER_DB_PATH = os.path.join(self.tmp, "remember.db")
        self.assertTrue(remember_token.init_remember_db())

    def tearDown(self):
        remember_token.REMEMBER_DB_PATH = self._orig_path
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _count_rows(self):
        import sqlite3
        con = sqlite3.connect(remember_token.REMEMBER_DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM remember_tokens").fetchone()[0]
        con.close()
        return n

    def test_init_is_idempotent(self):
        # Calling twice must not error or wipe existing rows.
        sel, val, _ = remember_token.issue_token("root", 1)
        self.assertTrue(remember_token.init_remember_db())
        self.assertEqual(remember_token.validate_token(sel, val), "root")

    def test_issue_and_validate_roundtrip(self):
        sel, val, exp = remember_token.issue_token("root", 30)
        self.assertEqual(len(sel), 32, "selector should be 16 bytes hex (32 chars)")
        self.assertEqual(len(val), 64, "validator should be 32 bytes hex (64 chars)")
        self.assertGreater(exp, int(time.time()))
        self.assertEqual(remember_token.validate_token(sel, val), "root")

    def test_validator_is_not_stored_in_plaintext(self):
        """Defense in depth: an attacker reading the DB cannot impersonate users."""
        sel, val, _ = remember_token.issue_token("root", 1)
        import sqlite3
        con = sqlite3.connect(remember_token.REMEMBER_DB_PATH)
        stored = con.execute(
            "SELECT validator_hash FROM remember_tokens WHERE selector = ?",
            (sel,),
        ).fetchone()[0]
        con.close()
        self.assertNotEqual(stored, val, "raw validator must not be stored")
        self.assertEqual(len(stored), 64, "sha256 hex digest expected")

    def test_wrong_validator_rejected(self):
        sel, val, _ = remember_token.issue_token("root", 1)
        # Flip last hex char while keeping length & charset valid.
        tampered = val[:-1] + ("0" if val[-1] != "0" else "1")
        self.assertIsNone(remember_token.validate_token(sel, tampered))
        # Real one still works.
        self.assertEqual(remember_token.validate_token(sel, val), "root")

    def test_unknown_selector_rejected(self):
        # Token never issued.
        self.assertIsNone(remember_token.validate_token("00" * 16, "ff" * 32))

    def test_empty_inputs_rejected(self):
        self.assertIsNone(remember_token.validate_token("", "abc"))
        self.assertIsNone(remember_token.validate_token("abc", ""))
        self.assertIsNone(remember_token.validate_token(None, None))

    def test_expired_token_rejected_and_cleaned_up(self):
        sel, val, _ = remember_token.issue_token("root", 1)
        # Backdate the row so validate sees it as expired.
        import sqlite3
        con = sqlite3.connect(remember_token.REMEMBER_DB_PATH)
        con.execute(
            "UPDATE remember_tokens SET expires_at = ? WHERE selector = ?",
            (int(time.time()) - 60, sel),
        )
        con.commit()
        con.close()
        self.assertIsNone(remember_token.validate_token(sel, val))
        self.assertEqual(self._count_rows(), 0, "expired row should be pruned on validate")

    def test_revoke_token(self):
        sel, val, _ = remember_token.issue_token("root", 1)
        self.assertEqual(self._count_rows(), 1)
        remember_token.revoke_token(sel)
        self.assertEqual(self._count_rows(), 0)
        self.assertIsNone(remember_token.validate_token(sel, val))

    def test_revoke_token_noop_for_unknown(self):
        # Should not raise or affect other rows.
        sel, _, _ = remember_token.issue_token("root", 1)
        remember_token.revoke_token("deadbeef" * 4)
        self.assertEqual(self._count_rows(), 1, "unrelated token must remain")
        remember_token.revoke_token("")
        remember_token.revoke_token(None)
        self.assertEqual(self._count_rows(), 1)

    def test_revoke_all_for_user(self):
        for _ in range(3):
            remember_token.issue_token("alice", 1)
        remember_token.issue_token("bob", 1)
        self.assertEqual(self._count_rows(), 4)
        remember_token.revoke_all_for_user("alice")
        self.assertEqual(self._count_rows(), 1, "only bob's row should remain")

    def test_multiple_devices_per_user_supported(self):
        # Issuing two tokens for one user should yield distinct selectors and
        # both should validate independently (simulates two browsers/devices).
        s1, v1, _ = remember_token.issue_token("root", 1)
        s2, v2, _ = remember_token.issue_token("root", 1)
        self.assertNotEqual(s1, s2)
        self.assertNotEqual(v1, v2)
        self.assertEqual(remember_token.validate_token(s1, v1), "root")
        self.assertEqual(remember_token.validate_token(s2, v2), "root")
        # Cross-pairing must fail.
        self.assertIsNone(remember_token.validate_token(s1, v2))
        self.assertIsNone(remember_token.validate_token(s2, v1))

    def test_opportunistic_prune_on_issue(self):
        # Insert an already-expired row, then issue a new token; the prune in
        # issue_token() should drop the stale row.
        sel, val, _ = remember_token.issue_token("root", 1)
        import sqlite3
        con = sqlite3.connect(remember_token.REMEMBER_DB_PATH)
        con.execute(
            "UPDATE remember_tokens SET expires_at = ? WHERE selector = ?",
            (int(time.time()) - 60, sel),
        )
        con.commit()
        con.close()
        remember_token.issue_token("root", 1)
        # New row inserted, expired one pruned.
        self.assertEqual(self._count_rows(), 1)

    def test_parse_cookie_happy_and_sad(self):
        self.assertEqual(
            remember_token.parse_cookie("abc:def"),
            ("abc", "def"),
        )
        # validator may contain colons or other chars after the first ':'
        s, v = remember_token.parse_cookie("sel:val:with:colons")
        self.assertEqual(s, "sel")
        self.assertEqual(v, "val:with:colons")
        # malformed inputs all yield (None, None)
        for bad in ("", None, "no-colon-here", ":only-validator", "only-selector:"):
            self.assertEqual(remember_token.parse_cookie(bad), (None, None), bad)


class FlaskIntegrationTests(unittest.TestCase):
    """Reproduces the production wiring (login/before_request/logout) on a
    minimal Flask app so we can exercise the cookie flow without importing
    pcat_manager_web (which pulls hardware-only modules)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="remember-flask-")
        self._orig_path = remember_token.REMEMBER_DB_PATH
        remember_token.REMEMBER_DB_PATH = os.path.join(self.tmp, "remember.db")
        remember_token.init_remember_db()

    def tearDown(self):
        remember_token.REMEMBER_DB_PATH = self._orig_path
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _build_app(secret_key):
        from flask import Flask, request, session, redirect, make_response

        app = Flask(__name__)
        app.config["SECRET_KEY"] = secret_key
        app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

        REMEMBER_COOKIE_NAME = "pcat_remember"
        REMEMBER_TTL_DAYS = 180

        @app.before_request
        def restore():
            if "username" not in session:
                raw = request.cookies.get(REMEMBER_COOKIE_NAME)
                sel, val = remember_token.parse_cookie(raw) if raw else (None, None)
                if sel and val:
                    user = remember_token.validate_token(sel, val)
                    if user:
                        session["username"] = user
                        session["remember_login"] = True
                        session.permanent = True

        @app.route("/login", methods=["POST"])
        def login():
            if request.form.get("password") != "hunter2":
                return ("bad", 401)
            session["username"] = "root"
            session.permanent = True
            resp = make_response(redirect("/"))
            if request.form.get("remember_login"):
                sel, val, _ = remember_token.issue_token("root", REMEMBER_TTL_DAYS)
                resp.set_cookie(
                    REMEMBER_COOKIE_NAME, f"{sel}:{val}",
                    max_age=REMEMBER_TTL_DAYS * 86400,
                    httponly=True, samesite="Lax", secure=False, path="/",
                )
            return resp

        @app.route("/logout", methods=["POST"])
        def logout():
            session.pop("username", None)
            resp = make_response(redirect("/login"))
            raw = request.cookies.get(REMEMBER_COOKIE_NAME)
            sel, _ = remember_token.parse_cookie(raw) if raw else (None, None)
            if sel:
                remember_token.revoke_token(sel)
            resp.delete_cookie(REMEMBER_COOKIE_NAME, path="/")
            return resp

        @app.route("/")
        def home():
            if "username" not in session:
                return ("anon", 401)
            return (f"hello {session['username']}", 200)

        return app

    @staticmethod
    def _extract_remember_cookie(resp):
        """Return the raw 'selector:validator' value from a Set-Cookie header,
        or None if not present."""
        for h in resp.headers.getlist("Set-Cookie"):
            if h.startswith("pcat_remember="):
                # First '=' separates name from value; value runs until first ';'
                value = h.split(";", 1)[0].split("=", 1)[1]
                return value, h
        return None, None

    # --- Flow ---

    def test_login_without_remember_does_not_set_cookie(self):
        app = self._build_app("k1")
        c = app.test_client()
        r = c.post("/login", data={"password": "hunter2"})
        self.assertEqual(r.status_code, 302)
        val, _ = self._extract_remember_cookie(r)
        self.assertIsNone(val, "no remember cookie should be set when checkbox is off")

    def test_login_with_remember_sets_secure_cookie_and_db_row(self):
        app = self._build_app("k1")
        c = app.test_client()
        r = c.post("/login", data={"password": "hunter2", "remember_login": "on"})
        self.assertEqual(r.status_code, 302)
        val, header = self._extract_remember_cookie(r)
        self.assertIsNotNone(val)
        # Cookie hygiene we rely on.
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Lax", header)
        self.assertIn("Max-Age=", header)
        # selector:validator format
        sel, validator = val.split(":", 1)
        self.assertEqual(len(sel), 32)
        self.assertEqual(len(validator), 64)
        # DB row exists for this selector.
        self.assertEqual(remember_token.validate_token(sel, validator), "root")

    def test_reboot_simulation_session_restored_from_cookie(self):
        """The original bug: SECRET_KEY rotates and Flask session is wiped, but
        the user's long-lived cookie + sqlite row must keep them logged in."""
        app1 = self._build_app("first-boot-key")
        c1 = app1.test_client()
        r = c1.post("/login", data={"password": "hunter2", "remember_login": "on"})
        cookie_val, _ = self._extract_remember_cookie(r)
        self.assertIsNotNone(cookie_val)

        # "Reboot": new app with a different SECRET_KEY, new test client (no
        # Flask session cookie carried over). Only the persistent cookie remains.
        app2 = self._build_app("post-reboot-key")
        c2 = app2.test_client()
        c2.set_cookie("pcat_remember", cookie_val, domain="localhost", path="/")
        r = c2.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"hello root", r.data)

    def test_logout_revokes_db_row_and_clears_cookie(self):
        app = self._build_app("k1")
        c = app.test_client()
        c.post("/login", data={"password": "hunter2", "remember_login": "on"})
        r = c.post("/logout")
        self.assertEqual(r.status_code, 302)
        cleared = any(
            "pcat_remember=" in h and ("Max-Age=0" in h or "1970" in h)
            for h in r.headers.getlist("Set-Cookie")
        )
        self.assertTrue(cleared, "logout must clear the remember cookie")

    def test_replay_of_revoked_cookie_is_rejected(self):
        app = self._build_app("k1")
        c = app.test_client()
        r = c.post("/login", data={"password": "hunter2", "remember_login": "on"})
        cookie_val, _ = self._extract_remember_cookie(r)
        c.post("/logout")  # revokes DB row

        # New "boot" trying to replay the stolen/old cookie.
        app2 = self._build_app("k2")
        c2 = app2.test_client()
        c2.set_cookie("pcat_remember", cookie_val, domain="localhost", path="/")
        r = c2.get("/")
        self.assertEqual(r.status_code, 401, "revoked cookie must not authenticate")

    def test_garbage_cookie_does_not_create_session(self):
        app = self._build_app("k1")
        c = app.test_client()
        for bad in ("not-a-pair", ":", "abc:", ":xyz", "abc:nonexistentvalidator"):
            c.set_cookie("pcat_remember", bad, domain="localhost", path="/")
            r = c.get("/")
            self.assertEqual(r.status_code, 401, f"bad cookie {bad!r} should not auth")

    def test_failed_login_does_not_create_token(self):
        app = self._build_app("k1")
        c = app.test_client()
        r = c.post("/login", data={"password": "wrong", "remember_login": "on"})
        self.assertEqual(r.status_code, 401)
        val, _ = self._extract_remember_cookie(r)
        self.assertIsNone(val)
        # No DB rows should exist.
        import sqlite3
        con = sqlite3.connect(remember_token.REMEMBER_DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM remember_tokens").fetchone()[0]
        con.close()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
