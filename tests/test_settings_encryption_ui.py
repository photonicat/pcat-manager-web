#!/usr/bin/env python3
"""Unit tests for the WiFi settings encryption dropdown (forum r7907, bugs 1+2).

The reported symptoms: WPA3-SAE vanished from the dropdown the moment any
non-BE frequency was clicked, and most radios never saw a WPA3 option at all.
The logic lives in Alpine expressions inside settings.html, so the real
template code is extracted and executed under node (skipped cleanly when node
is not installed); the watcher wiring is asserted structurally.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_settings_encryption_ui`.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
SETTINGS_HTML = os.path.join(APP_DIR, "templates", "settings.html")

NODE = shutil.which("node")

# Capability payloads exactly as /api/v2/wireless.json reports them on a
# photonicat2 (onboard aic8800 = HE on 2g/5g, PCIe QCN9274 = EHT incl. 6g).
SCENARIOS = """
const onboard = {frequency: "2g", capabilities: {bands: {
  "2g": {he: true, ht: true}, "5g": {he: true, vht: true, ht: true}}}};
const pcie = {frequency: "5g_ax", capabilities: {bands: {
  "2g": {eht: true, he: true, ht: true},
  "5g": {eht: true, he: true, vht: true, ht: true},
  "6g": {eht: true, he: true}}}};
const wifi5 = {frequency: "5g", capabilities: {bands: {
  "5g": {vht: true, ht: true}}}};

function opts(info) { return comp.encryption_options_for(info).map(o => o[0]); }
function reconciled(info, enc) {
  const i = Object.assign({}, info, {encryption: enc});
  comp.reconcile_encryption(i);
  return i.encryption;
}

console.log(JSON.stringify({
  onboard_2g: opts(onboard),
  onboard_5g_ax: opts(Object.assign({}, onboard, {frequency: "5g_ax"})),
  pcie_5g_ax: opts(pcie),
  pcie_2g: opts(Object.assign({}, pcie, {frequency: "2g"})),
  pcie_5g_be: opts(Object.assign({}, pcie, {frequency: "5g_be"})),
  pcie_6g_be: opts(Object.assign({}, pcie, {frequency: "6g_be"})),
  wifi5_only: opts(wifi5),
  sae_survives_freq_change: reconciled(
      Object.assign({}, pcie, {frequency: "5g_ax"}), "sae"),
  six_ghz_forces_sae: reconciled(
      Object.assign({}, pcie, {frequency: "6g_be"}), "psk2"),
  no_sae_radio_falls_to_psk2_not_open: reconciled(
      Object.assign({}, wifi5, {frequency: "5g"}), "sae"),
}));
"""


def extract_component_js():
    """The dropdown logic, verbatim, out of the Alpine component."""
    with open(SETTINGS_HTML, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("encrp_options_wifi6:")
    end = src.index("wifi_info_0_available:")
    return "const comp = {\n" + src[start:end] + "};\n"


@unittest.skipUnless(NODE, "node is not installed")
class EncryptionDropdownBehaviourTest(unittest.TestCase):
    """Executes the actual template code — not a re-implementation."""

    @classmethod
    def setUpClass(cls):
        script = extract_component_js() + SCENARIOS
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script)
            cls._js = f.name
        out = subprocess.run([NODE, cls._js], capture_output=True, text=True,
                             timeout=30)
        assert out.returncode == 0, out.stderr
        cls.result = json.loads(out.stdout)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls._js)

    def test_sae_is_offered_on_every_band_of_a_capable_radio(self):
        # Bug 1: SAE was gated on the *selected frequency* (5g_be/6g_be only)
        # and vanished when any other band was clicked.
        for key in ("onboard_2g", "onboard_5g_ax",
                    "pcie_2g", "pcie_5g_ax", "pcie_5g_be"):
            self.assertIn("sae", self.result[key],
                          "%s lost the WPA3 option" % key)

    def test_wpa2_and_open_remain_available_outside_6ghz(self):
        # Bug 2's inverse guard: adding WPA3 must not remove WPA2.
        for key in ("onboard_2g", "pcie_5g_ax", "pcie_5g_be"):
            self.assertEqual(self.result[key][:2], ["none", "psk2"], key)

    def test_6ghz_offers_only_sae(self):
        self.assertEqual(self.result["pcie_6g_be"], ["sae"])

    def test_wifi5_only_hardware_keeps_the_old_list(self):
        # No capability, no offer: SAE on a radio that cannot do it is a
        # save that fails or an AP clients cannot join.
        self.assertEqual(self.result["wifi5_only"], ["none", "psk2"])

    def test_selected_sae_survives_a_frequency_change(self):
        # The literal forum repro: WPA3 card, click a non-BE frequency.
        self.assertEqual(self.result["sae_survives_freq_change"], "sae")

    def test_6ghz_selection_forces_sae(self):
        self.assertEqual(self.result["six_ghz_forces_sae"], "sae")

    def test_reconcile_never_falls_back_to_an_open_network(self):
        self.assertEqual(self.result["no_sae_radio_falls_to_psk2_not_open"],
                         "psk2")


class WatcherWiringTest(unittest.TestCase):
    """Both radios need the frequency watcher; only radio 1 had one, so the
    first card's 6 GHz selection could post a stale non-SAE value."""

    def setUp(self):
        with open(SETTINGS_HTML, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_both_radios_reconcile_on_frequency_change(self):
        for radio in ("wifiinfo_0", "wifiinfo_1"):
            pattern = (r'\$watch\("%s\.frequency".*?'
                       r'reconcile_encryption\(this\.%s\)') % (radio, radio)
            self.assertRegex(self.src, re.compile(pattern, re.S),
                             "%s frequency watcher missing or no longer "
                             "reconciles the encryption" % radio)

    def test_dropdown_is_rendered_from_encryption_options_for(self):
        for radio in ("wifiinfo_0", "wifiinfo_1"):
            self.assertIn("encryption_options_for(%s)" % radio, self.src)


if __name__ == "__main__":
    unittest.main()
