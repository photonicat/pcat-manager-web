#!/usr/bin/env python3
"""Unit tests for app/pcat_util/user_auth.py -- the login gate.

This is the only thing standing between the LAN and the router's root-level
web UI, so the branches matter: --skip-auth must only ever be honored when the
flag is truly set, None credentials must never reach PAM (python-pam would
raise on None and turn a bad login into a 500), any exception PAM does raise
must be logged and become a plain rejection (never a 500 on /login), and the
PAM verdict must pass through unmodified. pam.authenticate is always patched
-- these tests must never touch the host's real /etc/shadow or PAM stack.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_user_auth`.
"""

import importlib
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# user_auth does `import pcat_config` at module level. Under tests/run_tests.py
# that name is already a MagicMock in sys.modules (see the patch.dict there);
# standalone it would pull the real pcat_config, which reads /etc paths at
# import time. Graft a minimal stub only when the name is absent so both entry
# points work -- same pattern as test_remember.py.
if "pcat_config" not in sys.modules:
    _stub = types.ModuleType("pcat_config")
    _stub.skip_auth = False
    sys.modules["pcat_config"] = _stub

# python-pam is a device-only dependency (OpenWrt package python3-pam) and is
# usually absent on dev machines, which used to make this whole module fail at
# import and silently drop every login-gate test from `tests/run_tests.py`.
# The tests patch `user_auth.pam.authenticate` anyway, so a stub with that one
# attribute is all they need.
try:
    import pam  # noqa: F401
except ImportError:
    _pam_stub = types.ModuleType("pam")
    _pam_stub.authenticate = lambda *args, **kwargs: False
    sys.modules["pam"] = _pam_stub

from pcat_util import user_auth  # noqa: E402


class UnixUserAuthTests(unittest.TestCase):
    def setUp(self):
        # Under run_tests.py pcat_config is a MagicMock whose .skip_auth is a
        # truthy MagicMock -- left alone, every test would short-circuit to
        # True and assert nothing. Pin it False; individual tests re-patch.
        p = patch.object(user_auth.pcat_config, "skip_auth", False, create=True)
        p.start()
        self.addCleanup(p.stop)

    def test_skip_auth_bypasses_pam_entirely(self):
        # Dev-mode contract: with --skip-auth, login must succeed without ever
        # consulting PAM (the dev host's accounts are irrelevant).
        with patch.object(user_auth.pcat_config, "skip_auth", True, create=True), \
             patch.object(user_auth.pam, "authenticate") as auth:
            self.assertTrue(user_auth.unix_user_auth("anyone", "anything"))
        auth.assert_not_called()

    def test_none_credentials_rejected_before_pam(self):
        # A login POST with a missing field yields None; that must be a clean
        # False, not a python-pam TypeError bubbling up as a 500 on the login
        # page.
        with patch.object(user_auth.pam, "authenticate") as auth:
            self.assertFalse(user_auth.unix_user_auth(None, "pw"))
            self.assertFalse(user_auth.unix_user_auth("root", None))
            self.assertFalse(user_auth.unix_user_auth(None, None))
        auth.assert_not_called()

    def test_valid_credentials_delegate_to_pam(self):
        with patch.object(user_auth.pam, "authenticate", return_value=True) as auth:
            self.assertTrue(user_auth.unix_user_auth("root", "hunter2"))
        auth.assert_called_once_with("root", "hunter2")

    def test_pam_rejection_passes_through(self):
        # The PAM verdict must not be massaged: a False here is the only thing
        # that keeps a wrong password out.
        with patch.object(user_auth.pam, "authenticate", return_value=False):
            self.assertFalse(user_auth.unix_user_auth("root", "wrong"))

    def test_empty_string_credentials_still_go_to_pam(self):
        # "" is not None: the None-guard must not swallow it, PAM decides
        # (an account could legitimately have policy on empty passwords).
        with patch.object(user_auth.pam, "authenticate", return_value=False) as auth:
            self.assertFalse(user_auth.unix_user_auth("root", ""))
        auth.assert_called_once_with("root", "")

    def test_pam_exception_is_a_clean_rejection(self):
        # python-pam raises for some inputs (ValueError on a NUL byte, decode
        # errors on the reason string, ctypes errors from libpam) instead of
        # returning False. The login view has no try/except of its own, so an
        # escaped exception renders the 500 page where the user typed a wrong
        # password. It must come back as False, and be logged once.
        for exc in (ValueError("none of username, password, or service may contain NUL"),
                    AttributeError("'NoneType' object has no attribute 'decode'"),
                    RuntimeError("pam blew up")):
            with self.subTest(exc=type(exc).__name__), \
                 patch.object(user_auth.pam, "authenticate", side_effect=exc), \
                 patch.object(user_auth, "_log_pam_error") as log:
                self.assertFalse(user_auth.unix_user_auth("root", "wrong"))
                log.assert_called_once()
                self.assertIs(log.call_args[0][0], exc)

    def test_pam_exception_is_logged_with_traceback_but_without_password(self):
        # The log line is the only diagnostic for a genuinely broken PAM stack
        # (the user just sees "invalid password"), so it must carry the
        # exception type and a traceback -- and never the submitted password.
        test_log = logging.getLogger("test_user_auth.pam")
        with patch.object(user_auth, "_auth_logger", return_value=test_log), \
             patch.object(user_auth.pam, "authenticate", side_effect=ValueError("boom")), \
             self.assertLogs(test_log, level="ERROR") as captured:
            self.assertFalse(user_auth.unix_user_auth("root", "s3cret-typo"))
        self.assertEqual(len(captured.records), 1)
        self.assertIn("ValueError", captured.output[0])
        self.assertIn("boom", captured.output[0])
        self.assertIsNotNone(captured.records[0].exc_info)
        self.assertNotIn("s3cret-typo", captured.output[0])

    def test_auth_logger_falls_back_when_project_logger_unavailable(self):
        # Under the unit-test stubs pcat_util.pcat_logger cannot be imported
        # (it opens the log file at import). The helper must still return a
        # usable logger rather than raise from inside the except block --
        # otherwise the fallback itself would become the 500.
        with patch.dict(sys.modules, {"pcat_util.pcat_logger": None}), \
             patch.object(user_auth, "pam"):
            # Also hide a grafted package attribute (test_remember installs one).
            import pcat_util as _pkg
            had = hasattr(_pkg, "pcat_logger")
            saved = getattr(_pkg, "pcat_logger", None)
            if had:
                delattr(_pkg, "pcat_logger")
            try:
                log = user_auth._auth_logger()
            finally:
                if had:
                    _pkg.pcat_logger = saved
        self.assertIsInstance(log, logging.Logger)
        self.assertEqual(log.name, "pcat_logger")


class PasslibImportFallbackTests(unittest.TestCase):
    def test_missing_passlib_does_not_break_import(self):
        # Some firmware images ship without passlib; the module-level
        # try/except must absorb the ImportError so the login page still loads
        # (PAM auth does not need passlib at all).
        try:
            with patch.dict(sys.modules, {"passlib.hosts": None}):
                importlib.reload(user_auth)
            self.assertTrue(callable(user_auth.unix_user_auth))
        finally:
            # Restore a cleanly-imported module for any later test in the
            # suite that touches user_auth.
            importlib.reload(user_auth)


if __name__ == "__main__":
    unittest.main()
