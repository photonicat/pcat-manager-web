#!/usr/bin/env python3
"""Unit tests for the Device Control WiFi toggle path (forum r7907, bug 4).

The reported symptom was "the switch flips and nothing happens". Four defects
stacked into it, and each gets pinned here:

* luci_reload() ran a binary this LuCI-less firmware does not ship; the
  FileNotFoundError aborted enable_wifi_interface() after the uci write but
  before `wifi reload`, so the radio state never applied.
* config_network_toggle() drove radio0/radio1 by index while radio numbering
  is not boot-stable; 'Onboard WiFi' could drive the PCIe card.
* The POST handler never invalidated the 15s network-interfaces cache, so the
  page's confirming re-fetch served the pre-toggle snapshot.
* The save payload force-posted both radios true whenever the WiFi upstream
  slot was enabled, silently discarding the other radio's toggle.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_device_control_toggle`.
"""

import ast
import os
import re
import unittest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

import app as pc_app

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ReloadHelpersSurviveMissingBinariesTest(unittest.TestCase):
    """A missing binary must count as a failed reload, not an exception:
    callers run these inside one try block and must reach the next step."""

    def test_luci_reload_missing_binary_returns_nonzero(self):
        with patch.object(pc_app.subprocess, "run",
                          side_effect=FileNotFoundError("luci-reload")):
            self.assertEqual(pc_app.luci_reload(), 127)

    def test_wifi_reload_missing_binary_returns_nonzero(self):
        with patch.object(pc_app.subprocess, "run",
                          side_effect=FileNotFoundError("/sbin/wifi")):
            self.assertEqual(pc_app.wifi_reload(), 127)


class EnableWifiInterfaceTest(unittest.TestCase):
    """enable_wifi_interface must reach `wifi reload` on a firmware without
    luci-reload — the exact break behind 'the toggle does nothing'."""

    def setUp(self):
        self.uci_writes = []
        patches = [
            patch.object(pc_app, "is_wifi_interface_enabled",
                         return_value=True),               # currently on
            patch.object(pc_app, "uci_set",
                         lambda k, v: self.uci_writes.append((k, v)) or 0),
            patch.object(pc_app, "uci_commit", lambda cfg: 0),
            patch.object(pc_app.wifi_radio, "record_intent",
                         lambda dev, flag: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_reload_runs_even_when_luci_reload_is_missing(self):
        ran = []

        def fake_run(cmd, **kw):
            if cmd[0] == "luci-reload":
                raise FileNotFoundError("luci-reload")
            ran.append(list(cmd))
            return MagicMock(returncode=0)

        with patch.object(pc_app.subprocess, "run", side_effect=fake_run):
            ok = pc_app.enable_wifi_interface("radio0", False, restart=True)

        self.assertTrue(ok, "the toggle must not report failure over a "
                            "missing optional binary")
        self.assertIn(["/sbin/wifi", "reload"], ran,
                      "uci was written but the radio state never applied")
        self.assertIn(("wireless.radio0.disabled", "1"), self.uci_writes)


class RadioClassResolutionTest(unittest.TestCase):
    """The toggles are labelled Onboard/PCIe; the uci writes must land on the
    radio that IS that card, resolved by device path, not by index."""

    def setUp(self):
        # A unit whose radios enumerated swapped: radio0 is the PCIe card.
        self.swapped = {"onboard": "radio1", "pcie": "radio0"}
        self.enabled = []
        patches = [
            patch.object(pc_app, "_radio_for_class", self.swapped.get),
            patch.object(pc_app, "enable_wifi_interface",
                         lambda dev, flag, restart=True:
                             self.enabled.append((dev, flag)) or True),
            patch.object(pc_app, "enable_lan_interface",
                         lambda flag, restart=False: False),
            patch.object(pc_app, "ensure_brlan_up",
                         lambda restart=False: False),
            patch.object(pc_app, "network_restart", lambda: 0),
            patch.object(pc_app, "socket_client", MagicMock()),
            patch.object(pc_app.wifi_wan, "active_sta_radio",
                         lambda: None),
            # Raising here skips the whole (try-wrapped) sanity report block.
            patch.object(pc_app, "get_real_operstate",
                         side_effect=RuntimeError("not under test")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_onboard_toggle_drives_the_onboard_radio(self):
        pc_app.config_network_toggle("priority", True, True, True,
                                     onboard_wifi_flag=False,
                                     pcie_wifi_flag=True)
        self.assertIn(("radio1", False), self.enabled,
                      "'Onboard WiFi off' must reach the onboard card "
                      "(radio1 on this unit), not whatever radio0 is")
        self.assertIn(("radio0", True), self.enabled)

    def test_the_relaying_radio_is_left_alone(self):
        with patch.object(pc_app.wifi_wan, "active_sta_radio",
                          lambda: "radio1"):
            pc_app.config_network_toggle("priority", True, True, True,
                                         onboard_wifi_flag=False,
                                         pcie_wifi_flag=False)
        self.assertEqual(self.enabled, [("radio0", False)],
                         "the radio carrying the upstream STA must not be "
                         "switched off by the AP toggles")


class SaveHandlerInvalidatesCacheTest(unittest.TestCase):
    """The POST must drop the 15s interfaces cache: the page re-fetches 5s
    later and otherwise reads the pre-toggle snapshot, snapping the switch
    back even though the change applied. Asserted on the source (importing
    the flask module boots half the app)."""

    def test_post_handler_zeroes_the_cache_timestamp(self):
        src = read(os.path.join(APP_DIR, "pcat_manager_web.py"))
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "save_network_interfaces_api")
        zeroed = any(
            isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Subscript)
            and getattr(n.targets[0].value, "id", "") == "network_interfaces_cache"
            and getattr(n.value, "value", None) == 0
            for n in ast.walk(fn))
        self.assertTrue(zeroed,
                        "save_network_interfaces_api no longer invalidates "
                        "network_interfaces_cache after applying the toggles")


class DeviceControlTemplateContractTest(unittest.TestCase):
    """Structure of the save path in device_control.html."""

    def setUp(self):
        self.src = read(os.path.join(APP_DIR, "templates",
                                     "device_control.html"))

    def test_only_the_upstream_radio_is_forced_on(self):
        # The old payload forced BOTH radios true whenever the wifi slot was
        # enabled (wifi_up ? true : ...), discarding the other toggle.
        self.assertIn("onboard_wifi: this.wifi_upstream_uses('onboard')",
                      self.src)
        self.assertIn("pcie_wifi: this.wifi_upstream_uses('pcie')", self.src)
        self.assertNotRegex(
            self.src, r"onboard_wifi:\s*wifi_up\s*\?",
            "the wifi-slot force-true payload is back")

    def test_failed_save_resyncs_the_switches(self):
        # The .catch path must re-fetch state: switches left showing the
        # failed request look exactly like a change that applied.
        body = self.src[self.src.index("request_set_network_mode() {"):]
        body = body[:body.index("fetch_network_mode() {")]
        m = re.search(r"\.catch\(\(error\) => \{(.*?)\}\);", body, re.S)
        self.assertIsNotNone(m, "save chain lost its catch handler")
        self.assertIn("fetch_network_mode()", m.group(1))
        self.assertIn("fetch_wan_priority()", m.group(1))


if __name__ == "__main__":
    unittest.main()
