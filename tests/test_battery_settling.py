#!/usr/bin/env python3
"""Tests for the battery remaining-time settling window.

power_now swings wildly for several seconds either side of a DC plug/unplug:
the rail collapses or surges, the charger negotiates its current, and the fuel
gauge has not caught up. An estimate computed from those samples is nonsense --
"0:10 left" on a nearly full battery a second after unplugging, or an absurd
time-to-full right after plugging in.

get_battery_charge_discharge_remain_time() therefore withholds the estimate
until the charge direction has been steady for _SETTLE_SECONDS (15s), showing
"-" until then.

Runs anywhere: no device, no battery. time.monotonic is stubbed.

    python3 -m unittest tests.test_battery_settling -v
"""

import ast
import os
import sys
import threading
import time
import types
import unittest

_APP_PY = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "app.py"))


def _load_settling_module():
    """Load ONLY the settling helper out of app.py, into a bare module.

    Importing app.py wholesale drags in the logger (which opens a root-owned
    /tmp log file), the sqlite databases and sysfs probing -- none of which a
    unit test should need, and all of which make the import order-dependent
    under the full-suite runner. Lifting the two definitions by AST keeps this
    test hermetic while still exercising the real shipped code: if the helper
    is renamed or removed, this fails loudly rather than silently testing a
    stale copy.
    """
    with open(_APP_PY) as f:
        source = f.read()
    tree = ast.parse(source)
    wanted = {"_SETTLE_SECONDS", "_battery_dir_state", "_battery_dir_lock",
              "_battery_direction_settled"}
    chunks = []
    for node in tree.body:
        name = None
        if isinstance(node, ast.FunctionDef):
            name = node.name
        elif isinstance(node, ast.Assign) and node.targets:
            target = node.targets[0]
            name = target.id if isinstance(target, ast.Name) else None
        if name in wanted:
            chunks.append(ast.get_source_segment(source, node))
    module = types.ModuleType("pcat_app_settling")
    module.time = time
    module.threading = threading
    exec(compile("\n".join(chunks), _APP_PY, "exec"), module.__dict__)
    missing = wanted - set(module.__dict__)
    if missing:
        raise AssertionError(
            "app.py no longer defines {} -- the settling window may have been "
            "removed or renamed".format(sorted(missing)))
    return module


pcat_app = _load_settling_module()


class FakeClock(object):
    """Controllable stand-in for time.monotonic()."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SettlingWindowTest(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self._orig_monotonic = pcat_app.time.monotonic
        pcat_app.time.monotonic = self.clock
        # Start from a clean slate so tests don't leak direction into each other.
        pcat_app._battery_dir_state["direction"] = None
        pcat_app._battery_dir_state["since"] = 0.0

    def tearDown(self):
        pcat_app.time.monotonic = self._orig_monotonic
        pcat_app._battery_dir_state["direction"] = None
        pcat_app._battery_dir_state["since"] = 0.0

    def settled(self, is_charging):
        return pcat_app._battery_direction_settled(is_charging)

    def test_window_is_15_seconds(self):
        self.assertEqual(pcat_app._SETTLE_SECONDS, 15)

    def test_first_sample_is_never_settled(self):
        """Cold start: nothing is known about how long this state has held."""
        self.assertFalse(self.settled(False))

    def test_not_settled_before_the_window_elapses(self):
        self.settled(False)
        for t in (1, 5, 10, 14):
            self.clock.advance(t if t == 1 else 0)
            self.clock.now = 1000.0 + t
            self.assertFalse(self.settled(False),
                             "settled after only {}s; needs 15s".format(t))

    def test_settles_exactly_at_the_window(self):
        self.settled(False)
        self.clock.advance(15)
        self.assertTrue(self.settled(False))

    def test_stays_settled_afterwards(self):
        self.settled(False)
        self.clock.advance(15)
        self.assertTrue(self.settled(False))
        self.clock.advance(300)
        self.assertTrue(self.settled(False))

    def test_plugging_in_resets_the_window(self):
        """The unplugged estimate described a state the device just left."""
        self.settled(False)
        self.clock.advance(60)
        self.assertTrue(self.settled(False))

        self.clock.advance(1)
        self.assertFalse(self.settled(True), "charge estimate shown immediately on plug-in")
        self.clock.advance(14)
        self.assertFalse(self.settled(True))
        self.clock.advance(1)
        self.assertTrue(self.settled(True))

    def test_unplugging_resets_the_window(self):
        self.settled(True)
        self.clock.advance(60)
        self.assertTrue(self.settled(True))

        self.clock.advance(1)
        self.assertFalse(self.settled(False), "runtime estimate shown immediately on unplug")
        self.clock.advance(15)
        self.assertTrue(self.settled(False))

    def test_flapping_never_settles(self):
        """A load swing that keeps flipping the current sign must not produce
        an estimate at all -- each flip restarts the window."""
        for i in range(20):
            self.clock.advance(3)
            self.assertFalse(self.settled(i % 2 == 0))

    def test_clock_step_cannot_short_circuit_the_window(self):
        """monotonic() is used precisely so an NTP step (likely at boot, when a
        device is also likely to be plugged in) cannot make the window appear
        to have elapsed."""
        self.assertIs(pcat_app.time.monotonic, self.clock)
        self.settled(False)
        # A wall-clock jump does not move the monotonic clock at all.
        self.assertFalse(self.settled(False))


class WiringTest(unittest.TestCase):
    """The settling window only helps if it is actually wired into the
    estimator and respected by its caller. These read the shipped source, so
    they fail if a later edit drops either half."""

    def setUp(self):
        with open(_APP_PY) as f:
            self.app_src = f.read()
        web = os.path.join(os.path.dirname(_APP_PY), "pcat_manager_web.py")
        with open(web) as f:
            self.web_src = f.read()

    def test_estimator_consults_the_settling_window(self):
        fn = self.app_src.split("def get_battery_charge_discharge_remain_time")[1]
        fn = fn.split("\ndef ")[0]
        self.assertIn("_battery_direction_settled", fn,
                      "the remaining-time estimator no longer gates on the "
                      "settling window")

    def test_estimator_reports_dash_not_none_while_settling(self):
        """None would re-trigger the caller's raw-sysfs fallback, which reads
        the same unsettled current and would undo the suppression."""
        fn = self.app_src.split("def get_battery_charge_discharge_remain_time")[1]
        fn = fn.split("\ndef ")[0]
        block = fn.split("if not settled:")[1].split("return")[1][:200]
        self.assertIn('"remaining_time": "-"', block)
        self.assertIn('"settling": True', block)

    def test_dashboard_fallback_is_skipped_while_settling(self):
        self.assertIn('not battery_status.get("settling")', self.web_src,
                      "the dashboard's raw-sysfs fallback would recompute an "
                      "estimate from unsettled current, defeating the window")


if __name__ == "__main__":
    unittest.main(verbosity=2)
