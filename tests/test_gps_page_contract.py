#!/usr/bin/env python3
"""Structural tests for the /gps page's capability gate and refresh loop.

Reloading /gps could leave a permanent "modem has no usable GNSS" card with
the receiver apparently off. gps_capable was evaluated server-side in the
route and baked into the template, and _gps_capable_modem() reads the modem
model -- empty for the first seconds after boot and after a module reset. A
reload landing in that window rendered the not-capable branch for good,
because nothing ever re-evaluated it. Reloading after a reboot lands in that
window almost by definition.

The page is Jinja + Alpine, so these assert on template structure rather than
behaviour: the capability decision must come from the status poll, not from a
server-side {% if %}, and the poll must stay wired up.

    python3 -m unittest tests.test_gps_page_contract -v
"""

import os
import re
import unittest

TEMPLATES = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "templates"))
GPS_HTML = os.path.join(TEMPLATES, "gps.html")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class GpsCapabilityGateTest(unittest.TestCase):
    def setUp(self):
        self.src = read(GPS_HTML)
        # Comments explain the fix and legitimately name the old flag.
        self.code = re.sub(r"<!--.*?-->", "", self.src, flags=re.S)

    def test_capability_card_is_driven_by_the_status_poll(self):
        self.assertIn(
            "st.capable === false", self.code,
            "the not-capable card must be decided from the poll so the page "
            "corrects itself once the modem identifies")

    def test_no_server_side_capability_branch_remains(self):
        # The whole bug: a render-time decision that never re-evaluated.
        self.assertNotIn(
            "{% if not gps_capable %}", self.code,
            "capability must not be baked in at render time")
        self.assertNotIn(
            "{% if gps_capable %}", self.code,
            "map init must not be gated on the render-time flag either")

    def test_status_poll_stays_on_a_timer(self):
        # "the data auto ajax refresh" -- the fields, map and AT log all hang
        # off this one interval.
        self.assertRegex(
            self.code, r"setInterval\(\s*\(\)\s*=>\s*this\.poll\(\)\s*,\s*\d+\s*\)",
            "the GPS page must keep polling on an interval")

    def test_poll_interval_is_responsive(self):
        m = re.search(r"setInterval\(\s*\(\)\s*=>\s*this\.poll\(\)\s*,\s*(\d+)\s*\)",
                      self.code)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 5000,
                             "a slow poll makes the page look frozen")

    def test_poll_reads_powered_from_the_api(self):
        self.assertRegex(self.code, r"this\.powered\s*=\s*!!\s*d\.powered")

    def test_map_init_is_guarded(self):
        # initMap() is now called unconditionally; a Leaflet throw there would
        # take the Alpine component down and stop the poll with it.
        self.assertRegex(
            self.code,
            r'getElementById\("gps-map"\)|typeof L === "undefined"',
            "initMap() must no-op instead of throwing when the container or "
            "Leaflet is missing")

    def test_template_div_nesting_is_balanced(self):
        stripped = re.sub(r"{%.*?%}", "", self.code, flags=re.S)
        opens = len(re.findall(r"<div\b", stripped))
        closes = len(re.findall(r"</div>", stripped))
        self.assertEqual(opens, closes,
                         "unbalanced <div> nesting in gps.html")

    def test_jinja_template_still_compiles(self):
        try:
            from jinja2 import Environment, FileSystemLoader
        except ImportError:
            self.skipTest("jinja2 not installed")
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        env.parse(self.src)  # raises TemplateSyntaxError on malformed markup


class GpsCapabilityBackendTest(unittest.TestCase):
    """The API must keep exporting `capable`, which the page now depends on."""

    def test_status_api_reports_capable_in_both_branches(self):
        src = read(os.path.join(os.path.dirname(TEMPLATES),
                                "pcat_manager_web.py"))
        m = re.search(r"def gps_status_api\(\):(.*?)(?=\n@flask_app\.route)",
                      src, flags=re.S)
        self.assertIsNotNone(m, "gps_status_api not found")
        body = m.group(1)
        self.assertIn('"capable": False', body,
                      "the not-capable early return must say so explicitly")
        self.assertIn('status["capable"] = True', body,
                      "the capable path must set the flag the page reads")


if __name__ == "__main__":
    unittest.main(verbosity=2)
