#!/usr/bin/env python3
"""Unit tests for pcat_util/openwrt.py -- the UCI/ubus plumbing every settings
save and dashboard poll goes through.

Why this matters: these wrappers are the single choke point between the web UI
and the router's real configuration state. A wrong argv means uci silently
edits the wrong key; a stale uci-show cache means the dashboard lies about the
config just saved; a swallowed commit failure means settings that evaporate on
reboot (the exact WiFi-upstream loss documented in uci_commit's docstring).
Everything here runs with subprocess and ubus fully mocked -- no test ever
touches the host's uci, ubus, or any real config.

Run: python3 -m unittest tests.test_openwrt_util  (or via tests/run_tests.py)
"""

import contextlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# tests/run_tests.py wraps the suite in patch.dict mocking `pcat_config`,
# which makes the real pcat_util.pcat_logger crash at import. Graft a
# lightweight stub (same pattern as test_remember.py) so uci_commit's lazy
# `from pcat_util import pcat_logger` never pulls in the real one.
import pcat_util  # noqa: E402  -- real package, empty __init__.py
if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_openwrt_util")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

from pcat_util import openwrt  # noqa: E402

_OPENWRT_PY = os.path.join(REPO_ROOT, "app", "pcat_util", "openwrt.py")


def _run_result(returncode=0, stdout="", stderr=""):
    """A stand-in for subprocess.CompletedProcess."""
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@contextlib.contextmanager
def _patched_pcat_logger():
    """Route uci_commit's lazy logger import to a fresh mock, regardless of
    which test module grafted the pcat_util.pcat_logger stub first."""
    mock_logger = MagicMock()
    stub = types.ModuleType("pcat_util.pcat_logger")
    stub.logger = mock_logger
    with patch.dict(sys.modules, {"pcat_util.pcat_logger": stub}):
        # `from pcat_util import pcat_logger` prefers the package attribute.
        with patch.object(pcat_util, "pcat_logger", stub, create=True):
            yield mock_logger


class OpenwrtTestBase(unittest.TestCase):
    """Keep the process-global uci-show cache from leaking between tests:
    other suite members use the same real pcat_util.openwrt module."""

    def setUp(self):
        openwrt.invalidate_uci_cache()

    def tearDown(self):
        openwrt.invalidate_uci_cache()


# ─── ubus: native path ───────────────────────────────────────────────────────

class UbusNativePathTests(OpenwrtTestBase):
    """The native python3-ubus path. If unwrapping or error mapping breaks,
    every status page shows an empty modem/network state on the device."""

    def test_list_result_is_unwrapped_to_first_dict(self):
        """ubus returns [{...}]; callers must get the dict, not the list,
        or every .get() on the result would crash the status endpoints."""
        fake = MagicMock()
        fake.call.return_value = [{"hostname": "pcat2"}]
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", True):
            self.assertEqual(openwrt.ubus_call("system", "board"),
                             {"hostname": "pcat2"})
        fake.call.assert_called_once_with("system", "board", {})

    def test_dict_result_passes_through(self):
        """A plain dict result reaches the caller unchanged."""
        fake = MagicMock()
        fake.call.return_value = {"up": True}
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", True):
            self.assertEqual(openwrt.ubus_call("network.interface.wan", "status",
                                               {"name": "wan"}),
                             {"up": True})
        fake.call.assert_called_once_with("network.interface.wan", "status",
                                          {"name": "wan"})

    def test_empty_list_and_junk_results_become_empty_dict(self):
        """Odd native returns ([], a string) must degrade to {} instead of
        leaking a non-dict into `.get()` call sites."""
        for junk in ([], "garbage", None):
            fake = MagicMock()
            fake.call.return_value = junk
            with patch.object(openwrt, "_ubus_mod", fake), \
                 patch.object(openwrt, "_ubus_connected", True):
                self.assertEqual(openwrt.ubus_call("a", "b"), {})

    def test_not_found_is_a_valid_empty_answer_not_a_failure(self):
        """'Not found' (e.g. hostapd iface not up yet) must return {} without
        tearing down the shared connection or forking a subprocess."""
        fake = MagicMock()
        fake.call.side_effect = Exception("Command failed: Not found")
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", True), \
             patch("subprocess.check_output") as mock_co:
            self.assertEqual(openwrt.ubus_call_strict("hostapd.phy0-ap0",
                                                      "get_clients"), {})
            self.assertTrue(openwrt._ubus_connected)
        mock_co.assert_not_called()

    def test_native_failure_marks_disconnected_and_falls_back(self):
        """A real native failure must drop the connection flag (so the next
        call reconnects) and still answer via the subprocess fallback."""
        fake = MagicMock()
        fake.call.side_effect = Exception("Connection reset")
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", True), \
             patch("subprocess.check_output",
                   return_value='{"ok": 1}') as mock_co:
            self.assertEqual(openwrt.ubus_call("system", "info"), {"ok": 1})
            self.assertFalse(openwrt._ubus_connected)
        mock_co.assert_called_once()

    def test_busy_lock_skips_native_and_uses_subprocess(self):
        """A stalled ubus call in another thread must not freeze this one:
        past the lock timeout the caller forks `ubus call` instead."""
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False
        fake_mod = MagicMock()
        with patch.object(openwrt, "_ubus_mod", fake_mod), \
             patch.object(openwrt, "_ubus_lock", fake_lock), \
             patch("subprocess.check_output",
                   return_value='{"fallback": true}') as mock_co:
            self.assertEqual(openwrt.ubus_call("network", "reload"),
                             {"fallback": True})
        fake_mod.call.assert_not_called()
        fake_lock.release.assert_not_called()
        mock_co.assert_called_once()


class EnsureUbusConnectedTests(OpenwrtTestBase):
    """Reconnect logic: get it wrong and every ubus consumer stays dark after
    a single ubusd restart until the web service is rebooted."""

    def test_noop_when_native_module_absent(self):
        """Without python3-ubus there is nothing to connect; must not raise."""
        with patch.object(openwrt, "_ubus_mod", None), \
             patch.object(openwrt, "_ubus_connected", False):
            openwrt._ensure_ubus_connected()
            self.assertFalse(openwrt._ubus_connected)

    def test_noop_when_already_connected(self):
        """An established connection must not be re-dialed on every call."""
        fake = MagicMock()
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", True):
            openwrt._ensure_ubus_connected()
        fake.connect.assert_not_called()

    def test_successful_connect_sets_flag(self):
        """After ubusd comes back, one call is enough to re-establish."""
        fake = MagicMock()
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", False):
            openwrt._ensure_ubus_connected()
            self.assertTrue(openwrt._ubus_connected)
        fake.connect.assert_called_once_with(openwrt._UBUS_SOCKET)

    def test_already_connected_error_counts_as_connected(self):
        """The bindings raise 'Already connected' after a half-torn state;
        treating that as failure would wedge the native path forever."""
        fake = MagicMock()
        fake.connect.side_effect = Exception("Already connected")
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", False):
            openwrt._ensure_ubus_connected()
            self.assertTrue(openwrt._ubus_connected)

    def test_hard_connect_failure_leaves_disconnected(self):
        """A dead socket keeps the flag False so callers use the fallback."""
        fake = MagicMock()
        fake.connect.side_effect = Exception("No such file or directory")
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", False):
            openwrt._ensure_ubus_connected()
            self.assertFalse(openwrt._ubus_connected)


# ─── ubus: subprocess fallback ───────────────────────────────────────────────

class UbusSubprocessFallbackTests(OpenwrtTestBase):
    """The `ubus call` CLI path (the only path on hosts without python3-ubus).
    Exit-code mapping decides whether a WAN health probe sees 'link down' or
    'could not ask' -- confusing the two demotes a live uplink."""

    def test_command_construction_with_args(self):
        """Args must travel as a single JSON argv element -- no shell, no
        word-splitting, so spaces/quotes in values cannot corrupt the call."""
        args = {"name": "wan wifi", "note": "it's quoted"}
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output", return_value="{}") as mock_co:
            openwrt.ubus_call("network.interface", "status", args)
        cmd = mock_co.call_args[0][0]
        self.assertEqual(cmd, ["ubus", "call", "network.interface", "status",
                               json.dumps(args)])
        self.assertEqual(mock_co.call_args[1]["timeout"], 5)

    def test_command_construction_without_args(self):
        """No-arg calls must not append an empty JSON blob."""
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output", return_value="{}") as mock_co:
            openwrt.ubus_call("system", "board")
        self.assertEqual(mock_co.call_args[0][0],
                         ["ubus", "call", "system", "board"])

    def test_blank_output_is_empty_dict(self):
        """Whitespace-only CLI output must parse to {} instead of crashing
        json.loads (some methods legitimately print nothing)."""
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output", return_value="  \n"):
            self.assertEqual(openwrt.ubus_call("a", "b"), {})

    def test_absent_object_exit_codes_map_to_empty_dict(self):
        """rc 3/4/5 (method/object not found, no data) are real 'absent'
        answers -- strict callers must see {}, not an infrastructure error."""
        for rc in (3, 4, 5):
            with patch.object(openwrt, "_ubus_mod", None), \
                 patch("subprocess.check_output",
                       side_effect=subprocess.CalledProcessError(rc, "ubus")):
                self.assertEqual(openwrt.ubus_call_strict("a", "b"), {},
                                 f"rc={rc} should be a valid empty answer")

    def test_infrastructure_exit_codes_are_failures_for_strict(self):
        """rc 7 (timeout) / 10 (connection failed) mean 'could not ask':
        strict callers get None so a WAN probe never demotes a live uplink,
        while plain ubus_call keeps its back-compatible {}."""
        for rc in (1, 7, 10):
            with patch.object(openwrt, "_ubus_mod", None), \
                 patch("subprocess.check_output",
                       side_effect=subprocess.CalledProcessError(rc, "ubus")):
                self.assertIsNone(openwrt.ubus_call_strict("a", "b"))
                self.assertEqual(openwrt.ubus_call("a", "b"), {})

    def test_timeout_exception_is_an_infrastructure_failure(self):
        """A hung `ubus call` (killed at 5s) is 'could not ask', not 'empty'."""
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output",
                   side_effect=subprocess.TimeoutExpired("ubus", 5)):
            self.assertIsNone(openwrt.ubus_call_strict("a", "b"))
            self.assertEqual(openwrt.ubus_call("a", "b"), {})

    def test_ubus_call_raw_round_trips_json(self):
        """Legacy raw callers get a JSON string, and '' (not '{}'/'null')
        when there is nothing, matching their historical error checks."""
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output", return_value='{"a": 1}'):
            self.assertEqual(json.loads(openwrt.ubus_call_raw("x", "y")),
                             {"a": 1})
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output",
                   side_effect=subprocess.TimeoutExpired("ubus", 5)):
            self.assertEqual(openwrt.ubus_call_raw("x", "y"), "")


# ─── module import behavior ──────────────────────────────────────────────────

class ImportTimeUbusSetupTests(OpenwrtTestBase):
    """The import-time probe decides native-vs-subprocess for the process
    lifetime; a crash here would take down the whole web service at boot."""

    @staticmethod
    def _load_fresh(fake_ubus):
        """Execute openwrt.py from source with a fake `ubus` module visible,
        under a private name so the shared module is untouched."""
        spec = importlib.util.spec_from_file_location(
            "openwrt_fresh_under_test", _OPENWRT_PY)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"ubus": fake_ubus}):
            spec.loader.exec_module(module)
        return module

    def test_native_module_with_working_socket_connects_at_import(self):
        """On the device, import alone must yield a live native connection."""
        fake = MagicMock()
        fake.call.return_value = [{"hostname": "pcat2"}]
        mod = self._load_fresh(fake)
        self.assertTrue(mod.is_native_ubus())
        self.assertTrue(mod._ubus_connected)
        fake.connect.assert_called_once_with(mod._UBUS_SOCKET)

    def test_native_module_with_dead_socket_still_imports(self):
        """ubusd not up yet at service start must not crash the import --
        the module keeps the native binding and reconnects lazily."""
        fake = MagicMock()
        fake.connect.side_effect = Exception("connect refused")
        mod = self._load_fresh(fake)
        self.assertTrue(mod.is_native_ubus())
        self.assertFalse(mod._ubus_connected)

    def test_host_without_python3_ubus_reports_subprocess_mode(self):
        """Dev machines have no python3-ubus: the shared module must have
        landed in subprocess mode, never touching a native binding."""
        self.assertFalse(openwrt.is_native_ubus())
        self.assertFalse(openwrt.is_native_uci())


# ─── uci: reads ──────────────────────────────────────────────────────────────

class UciGetTests(OpenwrtTestBase):
    """uci_get feeds every single-key config read."""

    def test_success_returns_stripped_value_with_exact_argv(self):
        """The key must be one argv element (no shell) and the trailing
        newline uci prints must be stripped before comparison logic sees it."""
        with patch("subprocess.run",
                   return_value=_run_result(0, "MySSID\n")) as mock_run:
            self.assertEqual(openwrt.uci_get("wireless.radio0.ssid"), "MySSID")
        self.assertEqual(mock_run.call_args[0][0],
                         ["uci", "get", "wireless.radio0.ssid"])
        self.assertEqual(mock_run.call_args[1]["timeout"], 5)

    def test_missing_key_returns_empty_string(self):
        """A key that does not exist (rc!=0) reads as '', which callers
        treat as 'unset' rather than crashing."""
        with patch("subprocess.run", return_value=_run_result(1, "", "not found")):
            self.assertEqual(openwrt.uci_get("wireless.nope"), "")

    def test_subprocess_failure_returns_empty_string(self):
        """A hung/failed uci binary degrades to '' instead of an exception."""
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("uci", 5)):
            self.assertEqual(openwrt.uci_get("system.@system[0].hostname"), "")


class UciShowTests(OpenwrtTestBase):
    """uci_show is the bulk reader behind the cached snapshot."""

    def test_returns_raw_bytes_with_exact_argv(self):
        """Output stays bytes (legacy callers decode themselves)."""
        with patch("subprocess.check_output",
                   return_value=b"network.lan=interface\n") as mock_co:
            self.assertEqual(openwrt.uci_show("network"),
                             b"network.lan=interface\n")
        self.assertEqual(mock_co.call_args[0][0], ["uci", "show", "network"])

    def test_failure_returns_empty_bytes(self):
        """Missing config or dead uci binary reads as b'' not an exception."""
        with patch("subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, "uci")):
            self.assertEqual(openwrt.uci_show("nonexistent"), b"")


class UciShowDictTests(OpenwrtTestBase):
    """Parsing of the whole-config snapshot the dashboard reads 4s-ly."""

    SAMPLE = (b"wireless.radio0=wifi-device\n"
              b"wireless.radio0.channel='36'\n"
              b"wireless.default_radio0.ssid='My Home WiFi'\n"
              b"wireless.default_radio0.key='pa'ss'\n"
              b"malformed line without equals\n")

    def test_parses_values_and_strips_outer_quotes(self):
        """Quoted values lose exactly one outer quote pair -- inner quotes
        (a password containing ') must survive verbatim or the UI would
        display and re-save a corrupted key."""
        with patch.object(openwrt, "uci_show", return_value=self.SAMPLE):
            data = openwrt.uci_show_dict("wireless", max_age=0)
        self.assertEqual(data["wireless.radio0"], "wifi-device")
        self.assertEqual(data["wireless.radio0.channel"], "36")
        self.assertEqual(data["wireless.default_radio0.ssid"], "My Home WiFi")
        self.assertEqual(data["wireless.default_radio0.key"], "pa'ss")
        self.assertNotIn("malformed line without equals", data)
        self.assertEqual(len(data), 4)

    def test_error_output_yields_empty_dict(self):
        """If uci itself fails the caller sees {} (missing config), not a
        crash mid-dashboard-render."""
        with patch.object(openwrt, "uci_show", return_value=b""):
            self.assertEqual(openwrt.uci_show_dict("ghost", max_age=0), {})


class UciShowCacheTests(OpenwrtTestBase):
    """The TTL cache that collapses ~8 forks per dashboard poll into one.
    Staleness bugs here mean the UI shows pre-save values after a save."""

    def _show_mock(self):
        return MagicMock(return_value=b"network.lan.ipaddr='192.168.1.1'\n")

    def test_second_read_within_ttl_hits_cache(self):
        """Repeated reads inside the TTL must not fork uci again."""
        show = self._show_mock()
        with patch.object(openwrt, "uci_show", show), \
             patch("time.time", return_value=1000.0):
            first = openwrt.uci_show_dict("network")
            second = openwrt.uci_show_dict("network")
        self.assertEqual(first, second)
        self.assertEqual(show.call_count, 1)

    def test_read_after_ttl_expiry_refetches(self):
        """Out-of-band edits (ssh, Luci) become visible after at most TTL."""
        show = self._show_mock()
        # uci_show_dict samples the clock once per call.
        clock = MagicMock(side_effect=[1000.0,
                                       1000.0 + openwrt.UCI_SHOW_CACHE_TTL + 1])
        with patch.object(openwrt, "uci_show", show), \
             patch("time.time", clock):
            openwrt.uci_show_dict("network")
            openwrt.uci_show_dict("network")
        self.assertEqual(show.call_count, 2)

    def test_max_age_zero_bypasses_cache(self):
        """Callers that need fresh truth (post-save verification) can force
        a refetch even inside the TTL window."""
        show = self._show_mock()
        with patch.object(openwrt, "uci_show", show), \
             patch("time.time", return_value=1000.0):
            openwrt.uci_show_dict("network")
            openwrt.uci_show_dict("network", max_age=0)
        self.assertEqual(show.call_count, 2)

    def test_configs_are_cached_independently(self):
        """Caching 'network' must not serve those keys for 'wireless'."""
        with patch.object(openwrt, "uci_show",
                          side_effect=[b"network.lan=interface\n",
                                       b"wireless.radio0=wifi-device\n"]), \
             patch("time.time", return_value=1000.0):
            self.assertIn("network.lan", openwrt.uci_show_dict("network"))
            self.assertIn("wireless.radio0", openwrt.uci_show_dict("wireless"))

    def test_invalidate_uci_cache_forces_refetch(self):
        """Explicit invalidation drops every cached config at once."""
        show = self._show_mock()
        with patch.object(openwrt, "uci_show", show), \
             patch("time.time", return_value=1000.0):
            openwrt.uci_show_dict("network")
            openwrt.invalidate_uci_cache()
            openwrt.uci_show_dict("network")
        self.assertEqual(show.call_count, 2)


# ─── uci: writes ─────────────────────────────────────────────────────────────

class UciWriteTests(OpenwrtTestBase):
    """uci set / add_list / delete: exact argv, honest return codes, and
    cache invalidation so a save is immediately visible to the next poll."""

    def test_uci_set_passes_value_as_single_argv_element(self):
        """Spaces and quotes in an SSID/password must reach uci verbatim as
        one argument -- there is no shell to mangle or inject through."""
        kv = "wireless.default_radio0.ssid=My Home's \"WiFi\" 2.4G"
        with patch("subprocess.run", return_value=_run_result(0)) as mock_run:
            self.assertTrue(openwrt.uci_set(kv))
        self.assertEqual(mock_run.call_args[0][0], ["uci", "set", kv])

    def test_uci_set_reports_failure(self):
        """A failed set returns False so callers do not report success for a
        change that never landed."""
        with patch("subprocess.run", return_value=_run_result(1)):
            self.assertFalse(openwrt.uci_set("bad..key=1"))
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("uci", 5)):
            self.assertFalse(openwrt.uci_set("wireless.radio0.disabled=0"))

    def test_uci_add_list_argv_and_results(self):
        """add_list appends (not overwrites) firewall-zone style lists."""
        kv = "firewall.@zone[1].network=wan_wifi"
        with patch("subprocess.run", return_value=_run_result(0)) as mock_run:
            self.assertTrue(openwrt.uci_add_list(kv))
        self.assertEqual(mock_run.call_args[0][0], ["uci", "add_list", kv])
        with patch("subprocess.run", return_value=_run_result(2)):
            self.assertFalse(openwrt.uci_add_list(kv))
        with patch("subprocess.run", side_effect=OSError("no uci binary")):
            self.assertFalse(openwrt.uci_add_list(kv))

    def test_uci_delete_argv_and_results(self):
        """Deleting an absent key reports False without raising, so callers
        can treat 'already gone' deliberately."""
        with patch("subprocess.run", return_value=_run_result(0)) as mock_run:
            self.assertTrue(openwrt.uci_delete("wireless.wifinet_sta.key"))
        self.assertEqual(mock_run.call_args[0][0],
                         ["uci", "delete", "wireless.wifinet_sta.key"])
        with patch("subprocess.run", return_value=_run_result(1)):
            self.assertFalse(openwrt.uci_delete("wireless.gone"))
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("uci", 5)):
            self.assertFalse(openwrt.uci_delete("wireless.gone"))

    def test_writes_invalidate_the_show_cache(self):
        """After any write the very next dashboard poll must refetch --
        otherwise the UI shows the pre-save value for up to 10 seconds."""
        for write in (openwrt.uci_set, openwrt.uci_add_list,
                      openwrt.uci_delete):
            openwrt.invalidate_uci_cache()  # fresh start per sub-case
            show = MagicMock(return_value=b"network.lan=interface\n")
            with patch.object(openwrt, "uci_show", show), \
                 patch("time.time", return_value=1000.0):
                openwrt.uci_show_dict("network")
                with patch("subprocess.run", return_value=_run_result(0)):
                    write("network.lan.x=1")
                openwrt.uci_show_dict("network")
            self.assertEqual(show.call_count, 2,
                             f"{write.__name__} did not invalidate the cache")


class UciCommitTests(OpenwrtTestBase):
    """uci_commit persistence: a quiet failure leaves changes staged in /tmp
    and they evaporate on reboot (this exact mode lost a device's WiFi STA)."""

    def test_success_returns_true_with_exact_argv_and_generous_timeout(self):
        """Commit uses the 15s timeout so a wifi-reload CPU spike does not
        kill the one subprocess whose loss costs config across reboots."""
        with patch("subprocess.run", return_value=_run_result(0)) as mock_run:
            self.assertTrue(openwrt.uci_commit("wireless"))
        self.assertEqual(mock_run.call_args[0][0],
                         ["uci", "commit", "wireless"])
        self.assertEqual(mock_run.call_args[1]["timeout"], 15)

    def test_success_invalidates_cache(self):
        """Post-commit reads must reflect the committed state immediately."""
        show = MagicMock(return_value=b"wireless.radio0=wifi-device\n")
        with patch.object(openwrt, "uci_show", show), \
             patch("time.time", return_value=1000.0):
            openwrt.uci_show_dict("wireless")
            with patch("subprocess.run", return_value=_run_result(0)):
                openwrt.uci_commit("wireless")
            openwrt.uci_show_dict("wireless")
        self.assertEqual(show.call_count, 2)

    def test_nonzero_exit_is_logged_and_returns_false(self):
        """A commit rejected by uci must be LOGGED with rc and stderr --
        silence here is exactly how config evaporates on reboot."""
        with _patched_pcat_logger() as mock_logger, \
             patch("subprocess.run",
                   return_value=_run_result(1, b"", b"uci: I/O error\n")):
            self.assertFalse(openwrt.uci_commit("wireless"))
        mock_logger.error.assert_called_once()
        msg = mock_logger.error.call_args[0][0]
        self.assertIn("wireless", msg)
        self.assertIn("rc=1", msg)
        self.assertIn("I/O error", msg)

    def test_timeout_is_logged_and_returns_false(self):
        """A commit killed by the timeout is a failure the log must show."""
        with _patched_pcat_logger() as mock_logger, \
             patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("uci", 15)):
            self.assertFalse(openwrt.uci_commit("network"))
        mock_logger.error.assert_called_once()
        self.assertIn("network", mock_logger.error.call_args[0][0])

    def test_broken_logger_still_returns_false(self):
        """Even if logging itself blows up, the caller must still learn the
        commit failed (False), never see a secondary exception."""
        with _patched_pcat_logger() as mock_logger, \
             patch("subprocess.run", side_effect=OSError("boom")):
            mock_logger.error.side_effect = RuntimeError("logger dead")
            self.assertFalse(openwrt.uci_commit("network"))


# ─── diagnostics ─────────────────────────────────────────────────────────────

class DebugStatusTests(OpenwrtTestBase):
    """The /dev diagnostics helper -- it must summarize honestly (and never
    crash) on both dev hosts and devices."""

    def test_subprocess_mode_reports_ok_probes(self):
        """On a healthy box the report carries hostnames from both probes."""
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output",
                   return_value='{"hostname": "pcat2"}'), \
             patch("subprocess.run",
                   return_value=_run_result(0, "pcat2\n")):
            info = openwrt.debug_status()
        self.assertFalse(info["ubus_native"])
        self.assertEqual(info["uci_mode"], "subprocess")
        self.assertEqual(info["subprocess_ubus_test"], "OK")
        self.assertEqual(info["subprocess_hostname"], "pcat2")
        self.assertEqual(info["subprocess_uci_test"], "OK")
        self.assertEqual(info["subprocess_uci_hostname"], "pcat2")

    def test_subprocess_mode_reports_failures_without_raising(self):
        """Dead ubus CLI and failing uci show up as FAIL strings, and a
        nonzero uci rc is reported distinctly from an exception."""
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output",
                   side_effect=subprocess.TimeoutExpired("ubus", 5)), \
             patch("subprocess.run", return_value=_run_result(1)):
            info = openwrt.debug_status()
        self.assertIn("FAIL", info["subprocess_ubus_test"])
        self.assertEqual(info["subprocess_uci_test"], "FAIL: rc=1")
        with patch.object(openwrt, "_ubus_mod", None), \
             patch("subprocess.check_output",
                   side_effect=OSError("no ubus binary")), \
             patch("subprocess.run", side_effect=OSError("no uci binary")):
            info = openwrt.debug_status()
        self.assertIn("FAIL", info["subprocess_uci_test"])

    def test_native_probe_ok_and_fail_paths(self):
        """With native ubus present the report includes a live-call check."""
        fake = MagicMock()
        fake.call.return_value = [{"hostname": "pcat2"}]
        with patch.object(openwrt, "_ubus_mod", fake), \
             patch.object(openwrt, "_ubus_connected", True), \
             patch("subprocess.check_output", side_effect=OSError()), \
             patch("subprocess.run", side_effect=OSError()):
            info = openwrt.debug_status()
        self.assertTrue(info["ubus_native"])
        self.assertEqual(info["ubus_test"], "OK")
        self.assertEqual(info["ubus_hostname"], "pcat2")

        fake_bad = MagicMock()
        fake_bad.call.side_effect = Exception("socket gone")
        with patch.object(openwrt, "_ubus_mod", fake_bad), \
             patch.object(openwrt, "_ubus_connected", True), \
             patch("subprocess.check_output", side_effect=OSError()), \
             patch("subprocess.run", side_effect=OSError()):
            info = openwrt.debug_status()
        self.assertIn("FAIL", info["ubus_test"])



class CacheFailurePoisonTest(OpenwrtTestBase):
    """Regression: a transient uci_show failure (b"") used to be cached as an
    empty config for the full TTL, blanking the dashboard/wireless page for up
    to 10s after uci had already recovered. Failures must not enter the cache."""

    def test_failed_read_is_not_cached(self):
        show = MagicMock(side_effect=[b"", b"network.lan.ipaddr='10.0.0.1'\n"])
        with patch.object(openwrt, "uci_show", show), \
             patch("time.time", return_value=1000.0):
            first = openwrt.uci_show_dict("network")
            second = openwrt.uci_show_dict("network")
        self.assertEqual(first, {})
        self.assertEqual(second, {"network.lan.ipaddr": "10.0.0.1"})
        self.assertEqual(show.call_count, 2, "failure was served from cache")

    def test_successful_read_still_caches(self):
        show = MagicMock(return_value=b"network.lan.ipaddr='10.0.0.1'\n")
        with patch.object(openwrt, "uci_show", show), \
             patch("time.time", return_value=1000.0):
            openwrt.uci_show_dict("network")
            openwrt.uci_show_dict("network")
        self.assertEqual(show.call_count, 1)


if __name__ == "__main__":
    unittest.main()
