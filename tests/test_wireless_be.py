#!/usr/bin/env python3
"""Unit tests for WiFi 7 (BE) capability detection and frequency mapping.

Covers app/pcat_util/command_parser.py:
  - _read_board_phy_capabilities parses /etc/board.json safely
  - _frequency_from_band_htmode maps every (band, htmode) to the right UI value
  - parse_uci_show_wireless attaches capabilities + correct frequency to each radio

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_wireless_be`.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# Real pcat_util.* modules import fine on the dev host (the openwrt wrapper
# falls back to subprocess when python3-ubus isn't installed). We deliberately
# do NOT pre-stub pcat_util.openwrt here -- doing so with an incomplete surface
# breaks the firmware test suite, whose setUp imports pcat_manager_web and
# expects names like uci_set / uci_commit / debug_status to exist.
from pcat_util import command_parser  # noqa: E402


# ---------- Sample board.json fixtures ---------------------------------------

# Modeled after the real /etc/board.json snippet from the photonicat2.
PCIE_WIFI7_PATH = "soc/22000000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0"
# Deliberately NOT in command_parser.builtin_wifi_dev (those use xhci-hcd.N.auto)
# so device_type stays "Unknown" and the test asserts the non-onboard branch.
USB_WIFI6_PATH = "platform/soc/23000000.usb/xhci-hcd.99.auto/usb1/1-1/1-1.7"

BOARD_JSON_WIFI7 = {
    "wlan": {
        "phy0": {
            "path": USB_WIFI6_PATH,
            "info": {"bands": {
                "2G": {"ht": True, "vht": True, "he": True, "max_width": 80, "default_channel": 1},
                "5G": {"ht": True, "vht": True, "he": True, "max_width": 80, "default_channel": 36},
            }},
        },
        "phy1": {
            "path": PCIE_WIFI7_PATH,
            "info": {"bands": {
                "2G": {"ht": True, "he": True, "eht": True, "max_width": 40, "default_channel": 1},
                "5G": {"ht": True, "vht": True, "he": True, "eht": True, "max_width": 160, "default_channel": 36},
                "6G": {"he": True, "eht": True, "max_width": 160, "default_channel": 2},
            }},
        },
    },
}

BOARD_JSON_WIFI6_ONLY = {
    "wlan": {
        "phy1": {
            "path": PCIE_WIFI7_PATH,
            "info": {"bands": {
                "5G": {"ht": True, "vht": True, "he": True, "max_width": 80, "default_channel": 36},
            }},
        },
    },
}


def _write_board_json(tmpdir, payload):
    path = os.path.join(tmpdir, "board.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


# ---------- _frequency_from_band_htmode --------------------------------------


class FrequencyMappingTests(unittest.TestCase):
    """Every (band, htmode) the device might report must map to the UI value
    used by settings.html, otherwise the dropdown shows nothing selected."""

    def test_2_4_ghz_classic(self):
        self.assertEqual(command_parser._frequency_from_band_htmode("2g", "HT20"), "2g")
        self.assertEqual(command_parser._frequency_from_band_htmode("2g", "HT40"), "2g")

    def test_2_4_ghz_be(self):
        self.assertEqual(command_parser._frequency_from_band_htmode("2g", "EHT20"), "2g_be")
        self.assertEqual(command_parser._frequency_from_band_htmode("2g", "EHT40"), "2g_be")

    def test_5_ghz_ac_ax_be(self):
        self.assertEqual(command_parser._frequency_from_band_htmode("5g", "VHT40"), "5g")
        self.assertEqual(command_parser._frequency_from_band_htmode("5g", "HE80"), "5g_ax")
        self.assertEqual(command_parser._frequency_from_band_htmode("5g", "EHT80"), "5g_be")
        self.assertEqual(command_parser._frequency_from_band_htmode("5g", "EHT160"), "5g_be")

    def test_6_ghz_always_be(self):
        # 6 GHz only exists from Wi-Fi 6E / 7 onward; any htmode there means BE.
        self.assertEqual(command_parser._frequency_from_band_htmode("6g", "HE80"), "6g_be")
        self.assertEqual(command_parser._frequency_from_band_htmode("6g", "EHT80"), "6g_be")
        self.assertEqual(command_parser._frequency_from_band_htmode("6g", "EHT160"), "6g_be")
        self.assertEqual(command_parser._frequency_from_band_htmode("6g", "EHT320"), "6g_be")

    def test_unknown_band_returns_empty(self):
        # Frontend treats "" as no-selection — better than guessing.
        self.assertEqual(command_parser._frequency_from_band_htmode("4g", "HE80"), "")
        self.assertEqual(command_parser._frequency_from_band_htmode("", ""), "")


# ---------- _read_board_phy_capabilities -------------------------------------


class BoardJsonCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdirs = []

    def tearDown(self):
        import shutil
        for d in self.tmpdirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mktmp(self):
        d = tempfile.mkdtemp(prefix="board-test-")
        self.tmpdirs.append(d)
        return d

    def test_wifi7_phy_marked_eht(self):
        caps = _read_with(_write_board_json(self._mktmp(), BOARD_JSON_WIFI7))
        self.assertIn(PCIE_WIFI7_PATH, caps)
        pcie = caps[PCIE_WIFI7_PATH]
        self.assertTrue(pcie["is_pcie"], "pcie path must be flagged")
        self.assertTrue(pcie["bands"]["5g"]["eht"])
        self.assertTrue(pcie["bands"]["6g"]["eht"])
        self.assertEqual(pcie["bands"]["5g"]["max_width"], 160)

    def test_usb_phy_not_marked_pcie(self):
        caps = _read_with(_write_board_json(self._mktmp(), BOARD_JSON_WIFI7))
        usb = caps[USB_WIFI6_PATH]
        self.assertFalse(usb["is_pcie"])
        self.assertFalse(usb["bands"]["5g"].get("eht", False),
                         "USB Wi-Fi 6 phy must not advertise EHT")

    def test_band_keys_are_lowercased(self):
        # board.json uses 2G/5G/6G (upper); the rest of the code keys off lower.
        caps = _read_with(_write_board_json(self._mktmp(), BOARD_JSON_WIFI7))
        pcie = caps[PCIE_WIFI7_PATH]
        self.assertIn("2g", pcie["bands"])
        self.assertIn("5g", pcie["bands"])
        self.assertIn("6g", pcie["bands"])
        self.assertNotIn("2G", pcie["bands"])

    def test_missing_board_json_returns_empty(self):
        # Don't crash if /etc/board.json is missing — just disable BE detection.
        with patch.object(command_parser, "_read_board_phy_capabilities",
                          wraps=command_parser._read_board_phy_capabilities):
            with patch("builtins.open", side_effect=FileNotFoundError):
                self.assertEqual(command_parser._read_board_phy_capabilities(), {})

    def test_malformed_board_json_returns_empty(self):
        with patch("builtins.open", side_effect=ValueError("bad json")):
            self.assertEqual(command_parser._read_board_phy_capabilities(), {})

    def test_band_without_eht_defaults_false(self):
        # A Wi-Fi 6 only phy must have eht == False (not missing) so the frontend
        # predicate `caps.bands["5g"].eht` evaluates cleanly.
        caps = _read_with(_write_board_json(self._mktmp(), BOARD_JSON_WIFI6_ONLY))
        pcie = caps[PCIE_WIFI7_PATH]
        self.assertFalse(pcie["bands"]["5g"]["eht"])
        self.assertTrue(pcie["bands"]["5g"]["he"])

    def test_handles_missing_numeric_fields(self):
        bad = {"wlan": {"phy0": {"path": "x", "info": {"bands": {"5G": {"eht": True}}}}}}
        caps = _read_with(_write_board_json(self._mktmp(), bad))
        # Should default missing max_width / default_channel to 0, not crash.
        self.assertEqual(caps["x"]["bands"]["5g"]["max_width"], 0)
        self.assertEqual(caps["x"]["bands"]["5g"]["default_channel"], 0)


def _read_with(path):
    """Helper: call _read_board_phy_capabilities with a custom board.json path."""
    real_open = open

    def fake_open(p, *a, **kw):
        if p == "/etc/board.json":
            return real_open(path, *a, **kw)
        return real_open(p, *a, **kw)

    with patch("builtins.open", side_effect=fake_open):
        return command_parser._read_board_phy_capabilities()


# ---------- parse_uci_show_wireless integration ------------------------------


def _fake_uci_show(uci_text):
    """Build a callable that returns the given UCI dump bytes."""
    def _call(_section):
        return uci_text if isinstance(uci_text, (bytes, bytearray)) else uci_text.encode()
    return _call


# Minimal but realistic UCI dump for two radios — radio0 USB Wi-Fi 6, radio1 PCIe.
# Matches the photonicat2 wireless config layout shown in the original Slack thread.
UCI_TWO_RADIOS = b"""\
wireless.radio0=wifi-device
wireless.radio0.type='mac80211'
wireless.radio0.path='%(usb)s'
wireless.radio0.country='US'
wireless.radio0.band='2g'
wireless.radio0.channel='6'
wireless.radio0.htmode='HT20'
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.mode='ap'
wireless.default_radio0.key='photonicat'
wireless.default_radio0.encryption='psk2'
wireless.default_radio0.ssid='photonicat-2g'
wireless.radio1=wifi-device
wireless.radio1.type='mac80211'
wireless.radio1.path='%(pcie)s'
wireless.radio1.country='US'
wireless.radio1.band='5g'
wireless.radio1.channel='36'
wireless.radio1.htmode='EHT160'
wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
wireless.default_radio1.mode='ap'
wireless.default_radio1.key='photonicat'
wireless.default_radio1.encryption='sae'
wireless.default_radio1.ssid='photonicat-5g-be'
""" % {b"usb": USB_WIFI6_PATH.encode(), b"pcie": PCIE_WIFI7_PATH.encode()}


class ParseUciShowWirelessTests(unittest.TestCase):
    """End-to-end: UCI dump + board.json -> per-radio dict the API returns."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="parse-test-")
        self.board_path = _write_board_json(self.tmp, BOARD_JSON_WIFI7)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, uci_bytes, board_path=None, devices_exist=True):
        """Invoke parse_uci_show_wireless with controlled inputs.

        Patches uci_show, device_exist, is_wifi_interface_enabled, and
        builtins.open (only for /etc/board.json) so the test doesn't depend on
        the host filesystem or /sys.
        """
        if board_path is None:
            board_path = self.board_path
        real_open = open

        def fake_open(p, *a, **kw):
            if p == "/etc/board.json":
                return real_open(board_path, *a, **kw)
            return real_open(p, *a, **kw)

        with patch.object(command_parser, "uci_show", _fake_uci_show(uci_bytes)), \
             patch.object(command_parser, "device_exist", return_value=devices_exist), \
             patch.object(command_parser, "is_wifi_interface_enabled", return_value=True), \
             patch("builtins.open", side_effect=fake_open):
            return command_parser.parse_uci_show_wireless(is_mock_data=False)

    def test_two_radios_returned_with_capabilities(self):
        result = self._run(UCI_TWO_RADIOS)
        self.assertEqual(len(result), 2)
        by_device = {r["device"]: r for r in result}

        # Radio 0: USB, Wi-Fi 6 max, no EHT.
        r0 = by_device["radio0"]
        self.assertEqual(r0["device_type"], "Unknown")  # not in builtin_wifi_dev list
        self.assertEqual(r0["frequency"], "2g")
        self.assertIn("capabilities", r0)
        self.assertFalse(r0["capabilities"]["is_pcie"])
        self.assertFalse(r0["capabilities"]["bands"]["5g"].get("eht", False))

        # Radio 1: PCIe, EHT-capable on 5G/6G.
        r1 = by_device["radio1"]
        self.assertEqual(r1["device_type"], "PCIE")
        self.assertEqual(r1["frequency"], "5g_be",
                         "EHT160 on 5g must map to 5g_be so the dropdown picks the right option")
        self.assertEqual(r1["encryption"], "sae")
        self.assertTrue(r1["capabilities"]["is_pcie"])
        self.assertTrue(r1["capabilities"]["bands"]["5g"]["eht"])
        self.assertTrue(r1["capabilities"]["bands"]["6g"]["eht"])

    def test_radio_not_present_is_omitted(self):
        # If /sys says the device doesn't exist, it must not be returned.
        result = self._run(UCI_TWO_RADIOS, devices_exist=False)
        self.assertEqual(result, [], "no radio should be returned when device_exist is False")

    def test_capabilities_default_empty_when_path_unknown(self):
        # UCI references a path that board.json doesn't know about — capabilities
        # should fall back to the safe empty shape so the frontend doesn't NPE.
        bogus_uci = UCI_TWO_RADIOS.replace(PCIE_WIFI7_PATH.encode(), b"some/unknown/path")
        result = self._run(bogus_uci)
        by_device = {r["device"]: r for r in result}
        caps = by_device["radio1"]["capabilities"]
        self.assertEqual(caps, {"is_pcie": False, "bands": {}},
                         "unknown phy path must yield empty caps, not raise")

    def test_legacy_he80_still_maps_to_5g_ax(self):
        # Make sure the BE support didn't regress existing Wi-Fi 6 behaviour.
        he80_uci = UCI_TWO_RADIOS.replace(b"htmode='EHT160'", b"htmode='HE80'")
        result = self._run(he80_uci)
        r1 = next(r for r in result if r["device"] == "radio1")
        self.assertEqual(r1["frequency"], "5g_ax")


# ---------- POST validation contract (freq_defaults table) -------------------


class PostHandlerFrequencyDefaultsTests(unittest.TestCase):
    """Verifies the wireless POST handler in save_wireless_action() stays in
    sync with what the frontend can send: the accepted frequency/encryption
    allow-lists and the per-frequency band/htmode/channel defaults.

    Rather than booting the full Flask app, we parse the handler's source with
    ``ast`` and read the literal values straight out of the function body. That
    keeps the test fast and import-light while still asserting real values
    (not just that some substring exists)."""

    EXPECTED_FREQS = {"2g", "5g", "5g_ax", "5g_be", "6g_be"}
    EXPECTED_ENCRYPTIONS = {"none", "psk", "psk2", "sae"}

    # band + htmode the handler must apply for each frequency. Mirrors the
    # freq_defaults table in save_wireless_action(); kept in lock-step so a
    # change to one without the other turns the test red.
    EXPECTED_DEFAULTS = {
        "2g":    {"band": "2g", "htmode": "HT20"},
        "5g":    {"band": "5g", "htmode": "VHT40"},
        "5g_ax": {"band": "5g", "htmode": "HE80"},
        "5g_be": {"band": "5g", "htmode": "EHT80"},
        "6g_be": {"band": "6g", "htmode": "EHT320"},
    }

    @classmethod
    def setUpClass(cls):
        import ast

        with open(os.path.join(REPO_ROOT, "app", "pcat_manager_web.py")) as f:
            tree = ast.parse(f.read())

        fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "save_wireless_action"),
            None,
        )
        assert fn is not None, "save_wireless_action() not found in pcat_manager_web.py"
        cls._fn = fn

        # Find the `freq_defaults = { ... }` dict literal assigned in the body.
        freq_defaults = None
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "freq_defaults" in targets and isinstance(node.value, ast.Dict):
                    freq_defaults = ast.literal_eval(node.value)
                    break
        assert freq_defaults is not None, "freq_defaults table not found"
        cls._freq_defaults = freq_defaults

        # Collect every string-tuple compared against `not in (...)` so we can
        # recover the frequency and encryption allow-lists without hard-coding
        # which line they live on.
        cls._membership_tuples = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare) and any(
                isinstance(op, ast.NotIn) for op in node.ops
            ):
                for comp in node.comparators:
                    if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                        try:
                            members = set(ast.literal_eval(comp))
                        except (ValueError, SyntaxError):
                            continue
                        if all(isinstance(m, str) for m in members):
                            cls._membership_tuples.append(members)

    def test_accepted_frequencies_and_encryptions(self):
        # The handler validates frequency and encryption against explicit
        # allow-lists; both expected sets must appear verbatim as a `not in`
        # tuple so the frontend and backend agree on accepted values.
        self.assertIn(
            self.EXPECTED_FREQS, self._membership_tuples,
            "frequency allow-list in save_wireless_action() drifted from the frontend contract",
        )
        self.assertIn(
            self.EXPECTED_ENCRYPTIONS, self._membership_tuples,
            "encryption allow-list in save_wireless_action() drifted from the frontend contract",
        )

    def test_freq_defaults_apply_correct_band_and_htmode(self):
        # Every frequency the frontend can send must have a defaults entry, and
        # each must apply the agreed band + htmode (BE modes use EHT htmodes).
        self.assertEqual(
            set(self._freq_defaults), self.EXPECTED_FREQS,
            "freq_defaults keys must match the accepted-frequency allow-list",
        )
        for freq, expected in self.EXPECTED_DEFAULTS.items():
            entry = self._freq_defaults[freq]
            self.assertEqual(entry["band"], expected["band"],
                             f"{freq} should use band {expected['band']}")
            self.assertEqual(entry["htmode"], expected["htmode"],
                             f"{freq} should use htmode {expected['htmode']}")

        # BE (Wi-Fi 7) modes must use EHT htmodes; non-BE must not.
        self.assertTrue(self._freq_defaults["5g_be"]["htmode"].startswith("EHT"))
        self.assertTrue(self._freq_defaults["6g_be"]["htmode"].startswith("EHT"))
        self.assertFalse(self._freq_defaults["5g_ax"]["htmode"].startswith("EHT"))


if __name__ == "__main__":
    unittest.main()
