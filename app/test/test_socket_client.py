"""Unit tests for the pcat-manager reply parsing in pc_socket_client.

The daemon's replies are the only source of truth for PMU settings, and the
whole UI reads what this parser leaves behind. The car-mode timeout is the
case that bit users: the web page showed 10 minutes on a device that was
configured for 5 and shutting down at 5, because nothing could tell "the
daemon has not answered yet" from "the daemon says 0".

The module imports device-only peers (pcat_config, pcat_util), which are
stubbed here so the file runs on a dev machine. No socket is opened: the
reply handler is called directly on a constructed client.
"""

import importlib.util
import os
import sys
import types
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_socket_client():
    for name in ("pcat_util", "pcat_util.isp_dict", "pcat_util.pcat_logger",
                 "pcat_config"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["pcat_util.isp_dict"].get_name = lambda *a, **k: ""
    sys.modules["pcat_util"].isp_dict = sys.modules["pcat_util.isp_dict"]
    logger = sys.modules["pcat_util.pcat_logger"]
    logger.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None)
    sys.modules["pcat_util"].pcat_logger = logger
    sys.modules["pcat_config"].get_oem_name = lambda *a, **k: "photonicat"

    spec = importlib.util.spec_from_file_location(
        "pc_socket_client_under_test",
        os.path.join(APP_DIR, "pc_socket_client.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load_socket_client()


class TestCarModeReply(unittest.TestCase):
    """charger-on-auto-start-get: the reply behind /timer's Car/UPS mode."""

    def setUp(self):
        self.client = sc.PcSocketClient()

    def _reply(self, **fields):
        payload = {"command": "charger-on-auto-start-get", "code": 0}
        payload.update(fields)
        # name-mangled private handler
        self.client._PcSocketClient__handle_json_command(payload)
        return self.client.charger_on_auto_start_mode

    def test_timeout_is_unknown_until_the_daemon_answers(self):
        # The regression: this 0 is a placeholder, not a setting. The UI used
        # to render it as a hardcoded 10 minutes.
        mode = self.client.charger_on_auto_start_mode
        self.assertFalse(mode["known"])
        self.assertEqual(mode["timeout"], 0)

    def test_reply_marks_the_value_known(self):
        mode = self._reply(state=1, timeout=300)
        self.assertTrue(mode["known"])
        self.assertEqual(mode["enabled"], "true")
        self.assertEqual(mode["timeout"], 300)     # 5 minutes, as configured

    def test_disabled_state_reads_back_as_false(self):
        mode = self._reply(state=0, timeout=600)
        self.assertEqual(mode["enabled"], "false")
        self.assertTrue(mode["known"])

    def test_a_real_zero_is_distinguishable_from_never_asked(self):
        # Both have timeout 0; only the answered one is `known`, which is the
        # whole point of the flag — the UI may offer its default for this one.
        mode = self._reply(state=0, timeout=0)
        self.assertEqual(mode["timeout"], 0)
        self.assertTrue(mode["known"])

    def test_a_failed_reply_leaves_the_value_unknown(self):
        self.client._PcSocketClient__handle_json_command(
            {"command": "charger-on-auto-start-get", "code": 1, "timeout": 300})
        self.assertFalse(self.client.charger_on_auto_start_mode["known"])

    def test_negative_countdown_is_clamped(self):
        self.assertEqual(self._reply(state=1, timeout=300,
                                     countdown=-1)["countdown"], 0)
        self.assertEqual(self._reply(state=1, timeout=300,
                                     countdown=42)["countdown"], 42)

    def test_dashboard_contract_keys_are_present(self):
        # The dashboard serialises this dict as-is and the web UI reads these
        # names; dropping one silently reintroduces the wrong-timeout bug.
        for key in ("enabled", "timeout", "countdown", "known"):
            self.assertIn(key, self.client.charger_on_auto_start_mode)


if __name__ == "__main__":
    unittest.main()
