#!/usr/bin/env python3
"""Unit tests for the no-WiFi-client auto-shutdown guard (forum r7907, bug 5).

The counter side (live AP ifnames, unknown-vs-zero) is pinned in
test_command_parser_extra. This file pins the consumer: an *unknown* client
count must never arm or advance the poweroff countdown — mapping unknown to 0
is what powered devices off under connected clients whenever a ubus probe
hiccuped or the probed hostapd name missed the live AP.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_idle_shutdown_guard`.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

import app as pc_app
import pcat_config


def make_app(client_count, timeout_minutes=10):
    """A bare App with just the idle-shutdown state, no thread machinery."""
    a = object.__new__(pc_app.App)
    a.idle_status = "active"
    a.idle_start_time = None
    a.idle_remaining_seconds = 0
    a.last_wifi_client_count = 0
    a.get_wifi_client_count = lambda: client_count
    return a


class IdleShutdownGuardTests(unittest.TestCase):

    def setUp(self):
        self.settings = {"idle_shutdown_enabled": True,
                         "idle_shutdown_timeout": 10,
                         "idle_shutdown_battery_only": False}
        patches = [
            patch.object(pcat_config, "get_user_config_key",
                         lambda key, fallback=None:
                             self.settings.get(key, fallback)),
            patch.object(pc_app, "power_event_log", MagicMock()),
            patch.object(pc_app.subprocess, "run", MagicMock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.poweroff = pc_app.subprocess.run

    def test_unknown_count_resets_the_countdown(self):
        app = make_app(client_count=None)
        # Mid-countdown, and past the deadline: the most dangerous moment.
        app.idle_status = "idle"
        app.idle_start_time = time.time() - 3600

        app.check_idle_shutdown()

        self.assertEqual(app.idle_status, "active")
        self.assertIsNone(app.idle_start_time)
        self.poweroff.assert_not_called()

    def test_unknown_count_never_powers_off_even_repeatedly(self):
        app = make_app(client_count=None)
        for _ in range(5):
            app.check_idle_shutdown()
        self.poweroff.assert_not_called()

    def test_connected_clients_reset_the_countdown(self):
        app = make_app(client_count=2)
        app.idle_status = "idle"
        app.idle_start_time = time.time() - 3600

        app.check_idle_shutdown()

        self.assertEqual(app.idle_status, "active")
        self.poweroff.assert_not_called()

    def test_a_true_zero_still_counts_down_and_powers_off(self):
        # The guard must not neuter the feature: a genuinely empty AP past
        # the deadline still shuts the device down.
        app = make_app(client_count=0)
        app.idle_status = "idle"
        app.idle_start_time = time.time() - (10 * 60 + 5)

        app.check_idle_shutdown()

        self.poweroff.assert_called_once()
        self.assertEqual(self.poweroff.call_args[0][0], ["poweroff"])

    def test_zero_starts_the_countdown_without_powering_off(self):
        app = make_app(client_count=0)

        app.check_idle_shutdown()

        self.assertEqual(app.idle_status, "idle")
        self.assertIsNotNone(app.idle_start_time)
        self.poweroff.assert_not_called()

    def test_disabled_feature_is_inert(self):
        self.settings["idle_shutdown_enabled"] = False
        app = make_app(client_count=None)
        app.check_idle_shutdown()
        self.assertEqual(app.idle_status, "disabled")
        self.poweroff.assert_not_called()


class ClientCountFetchTests(unittest.TestCase):
    """get_wifi_client_count passes None through — both from the counter and
    from an exception. Returning 0 there is the old bug in a new coat."""

    def _fetch(self, side_effect=None, value=None):
        app = object.__new__(pc_app.App)
        with patch.object(pc_app.command_parser, "get_wifi_clients_count",
                          side_effect=side_effect,
                          **({} if side_effect else {"return_value": value})):
            return pc_app.App.get_wifi_client_count(app)

    def test_counter_none_is_passed_through(self):
        self.assertIsNone(self._fetch(value=None))

    def test_counter_exception_reads_as_unknown_not_empty(self):
        self.assertIsNone(self._fetch(side_effect=RuntimeError("ubus died")))

    def test_real_counts_pass_through(self):
        self.assertEqual(self._fetch(value=3), 3)


if __name__ == "__main__":
    unittest.main()
