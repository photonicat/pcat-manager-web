#!/usr/bin/env python3
"""Unit tests for pcat_timer_manager -- the scheduler that drives the power
LED, the beeper and the smart fan from a once-a-minute loop.

This module was at 0% coverage, yet it is the only thing standing between the
user's "LED off at night" schedule and hardware that never changes state: a
regression in the time-range math silently leaves the LED burning all night
(or the beeper dead all day), a regression in the change-gating re-sends a
PMU/STM32 UART transaction every minute and wastes battery, and a regression
in the old-format migration wipes settings on upgrade. Every test isolates
the manager onto a temp settings file and fake socket/config stubs, so
nothing touches /etc, no thread is really started, and no wall clock is read.

Runs anywhere: no device, no PMU, datetime.now is frozen.

    python3 -m unittest tests.test_pcat_timer_manager -v
"""

import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime as real_datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

_APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, _APP_DIR)


def _load_timer_manager():
    """Load the real module with its dependencies stubbed at import time.

    tests/run_tests.py wraps the whole suite in patch.dict(sys.modules,
    {'pcat_config': MagicMock(), 'pcat_logger': MagicMock(), ...}); a plain
    `import pcat_timer_manager` would bind those MagicMocks (and the real
    pcat_util.pcat_logger crashes at import while pcat_config is mocked --
    see the note in test_remember.py). So exec the source from file under a
    private name, with our own stub pcat_util.pcat_logger and pcat_config in
    sys.modules just for the duration of the load. The loaded module keeps
    references to the stubs; sys.modules is restored afterwards, so this
    works identically standalone and under the full-suite runner.
    """
    stub_logger_mod = types.ModuleType("pcat_util.pcat_logger")
    logger = logging.getLogger("test_pcat_timer_manager")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    stub_logger_mod.logger = logger

    stub_pcat_util = types.ModuleType("pcat_util")
    stub_pcat_util.pcat_logger = stub_logger_mod

    stub_config = MagicMock(name="pcat_config_stub")

    with patch.dict(sys.modules, {
        "pcat_util": stub_pcat_util,
        "pcat_util.pcat_logger": stub_logger_mod,
        "pcat_config": stub_config,
    }):
        spec = importlib.util.spec_from_file_location(
            "pcat_timer_manager_under_test",
            os.path.join(_APP_DIR, "pcat_timer_manager.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


tm = _load_timer_manager()


class FakeSocketClient(object):
    """Records pmu_io_set / set_power_on_mode calls instead of talking UART."""

    def __init__(self, connected=True, temperature=45,
                 status_led_v2_enabled=None, beeper_enabled=None,
                 stm32_firmware_version=None,
                 schedule_supported=True, raw_supported=True):
        self.sock_connected = connected
        self.board_temperature = temperature
        self.status_led_v2_enabled = status_led_v2_enabled
        self.beeper_enabled = beeper_enabled
        self.stm32_firmware_version = stm32_firmware_version
        self.pmu_calls = []
        self.power_on_modes = []
        self.schedule_calls = []
        self.raw_calls = []
        self.time_sync_calls = 0
        self.schedule_gets = 0
        self.io_schedule = None
        # schedule_supported=False models the daemon this feature actually
        # shipped against: pmu-io-schedule-* silently dropped, read-back
        # times out (None). raw_supported=False models a daemon so old it
        # lacks even pmu-raw-command.
        self.schedule_supported = schedule_supported
        self.raw_supported = raw_supported

    def pmu_io_set(self, **kwargs):
        self.pmu_calls.append(kwargs)
        # Mirror real socket client: a set updates the cached HW state so
        # subsequent apply ticks see the new value.
        if "status_led_v2_enabled" in kwargs:
            self.status_led_v2_enabled = 1 if kwargs["status_led_v2_enabled"] else 0
        if "beeper_enabled" in kwargs:
            self.beeper_enabled = 1 if kwargs["beeper_enabled"] else 0

    def pmu_date_time_sync_utc(self):
        self.time_sync_calls += 1

    def pmu_io_schedule_set(self, led, beeper):
        self.schedule_calls.append((led, beeper))
        if not self.schedule_supported:
            return None
        self.io_schedule = {"supported": 1, "valid": 1,
                            "led": dict(led), "beeper": dict(beeper),
                            "current_led": 1, "current_beeper": 1,
                            "time_valid": 1, "flags": 3}
        return self.io_schedule

    def pmu_io_schedule_get(self, wait=False, timeout=1.5):
        self.schedule_gets += 1
        if not self.schedule_supported:
            return None
        return self.io_schedule

    def pmu_raw_command(self, mcu_command, payload_hex="", need_ack=True,
                        wait=False, timeout=1.5):
        self.raw_calls.append((int(mcu_command), payload_hex))
        return self.raw_supported

    def set_power_on_mode(self, mode):
        self.power_on_modes.append(mode)


class FakeStopEvent(object):
    """Stand-in for the loop's threading.Event: records wait() durations and
    flips running off after a fixed number of ticks so run() terminates
    without any real sleeping."""

    def __init__(self, mgr, ticks):
        self.mgr = mgr
        self.ticks = ticks
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) >= self.ticks:
            self.mgr.running = False

    def set(self):
        pass


class FrozenDateTime(object):
    """Controllable stand-in for the module's datetime.now()."""

    fixed = real_datetime(2026, 1, 1, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        # datetime.now(tz) is used by the raw time-sync payload builder; the
        # frozen clock has no real offset, so stamp the requested tz onto it.
        return cls.fixed if tz is None else cls.fixed.replace(tzinfo=tz)


class TimerTestBase(unittest.TestCase):
    """Redirect the settings file to a temp dir and give every test a fresh
    pcat_config stub with sane smart-fan defaults."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.settings_path = os.path.join(self.tmpdir.name, "timer.json")
        self._orig_config = tm.pcat_config
        tm.pcat_config = MagicMock(name="pcat_config_per_test")
        tm.pcat_config.get_fan_mode.return_value = "smart_quiet"
        tm.pcat_config.get_fan_speed.return_value = 9
        tm.pcat_config.set_fan_speed_smart.return_value = 3
        tm.pcat_config.is_device_charging.return_value = True
        tm.pcat_config.get_power_mode.return_value = "manual"

    def tearDown(self):
        tm.pcat_config = self._orig_config
        self.tmpdir.cleanup()

    def make_manager(self):
        # The constructor calls load_settings() against its default
        # /etc/pcat_timer_settings.json before we can redirect it; force the
        # existence check False so construction never depends on host /etc.
        with patch.object(tm.os.path, "exists", return_value=False):
            mgr = tm.PcatTimerManager()
        mgr.settings_file = self.settings_path
        return mgr

    def write_settings(self, data):
        with open(self.settings_path, "w") as f:
            json.dump(data, f)

    def saved(self):
        """What actually reached disk."""
        with open(self.settings_path) as f:
            return json.load(f)


class DefaultsTest(TimerTestBase):

    def test_factory_defaults_are_always_on(self):
        """A device with no saved schedule must behave like stock hardware:
        LED lit and beeper audible, not dark/silent."""
        mgr = self.make_manager()
        self.assertEqual(mgr.get_power_led_mode(), "on")
        self.assertEqual(mgr.get_beeper_mode(), "on")
        self.assertEqual(mgr.get_power_led_start_time(), "08:00")
        self.assertEqual(mgr.get_power_led_end_time(), "22:00")
        self.assertEqual(mgr.get_beeper_start_time(), "08:00")
        self.assertEqual(mgr.get_beeper_end_time(), "22:00")

    def test_getters_survive_empty_device_settings(self):
        """A hand-edited file with empty sections must not crash the web UI
        that renders these values -- getters fall back to defaults."""
        mgr = self.make_manager()
        mgr.settings = {"power_led": {}, "beeper": {}}
        self.assertEqual(mgr.get_power_led_mode(), "on")
        self.assertEqual(mgr.get_power_led_start_time(), "08:00")
        self.assertEqual(mgr.get_power_led_end_time(), "22:00")
        self.assertEqual(mgr.get_beeper_mode(), "on")
        self.assertEqual(mgr.get_beeper_start_time(), "08:00")
        self.assertEqual(mgr.get_beeper_end_time(), "22:00")


class LoadSettingsTest(TimerTestBase):

    def test_saved_schedule_survives_restart(self):
        """The user's night schedule must come back after a reboot."""
        mgr = self.make_manager()
        self.write_settings({
            "power_led": {"mode": "timed", "start_time": "07:30",
                          "end_time": "23:00"},
            "beeper": {"mode": "off", "start_time": "08:00",
                       "end_time": "22:00"},
        })
        mgr.load_settings()
        self.assertEqual(mgr.get_power_led_mode(), "timed")
        self.assertEqual(mgr.get_power_led_start_time(), "07:30")
        self.assertEqual(mgr.get_power_led_end_time(), "23:00")
        self.assertEqual(mgr.get_beeper_mode(), "off")

    def test_missing_file_keeps_defaults(self):
        mgr = self.make_manager()
        mgr.load_settings()  # self.settings_path does not exist
        self.assertEqual(mgr.get_power_led_mode(), "on")

    def test_corrupt_file_keeps_defaults_and_does_not_crash(self):
        """This device gets power-cut; a half-written JSON must not brick the
        timer thread at startup -- it falls back to always-on."""
        mgr = self.make_manager()
        with open(self.settings_path, "w") as f:
            f.write('{"power_led": TRUNCATED')
        mgr.load_settings()
        self.assertEqual(mgr.get_power_led_mode(), "on")
        self.assertEqual(mgr.get_beeper_mode(), "on")


class MigrationTest(TimerTestBase):
    """Upgrading firmware from the old enabled/all_day format must map onto
    the 3-state modes, or every user's schedule silently resets."""

    def load_old(self, power_led):
        mgr = self.make_manager()
        self.write_settings({
            "power_led": power_led,
            "beeper": {"mode": "on", "start_time": "08:00",
                       "end_time": "22:00"},
        })
        mgr.load_settings()
        return mgr

    def test_disabled_migrates_to_off(self):
        mgr = self.load_old({"enabled": False, "all_day": True,
                             "start_time": "08:00", "end_time": "22:00"})
        self.assertEqual(mgr.get_power_led_mode(), "off")

    def test_enabled_all_day_migrates_to_on(self):
        mgr = self.load_old({"enabled": True, "all_day": True,
                             "start_time": "08:00", "end_time": "22:00"})
        self.assertEqual(mgr.get_power_led_mode(), "on")

    def test_enabled_scheduled_migrates_to_timed_and_keeps_times(self):
        mgr = self.load_old({"enabled": True, "all_day": False,
                             "start_time": "09:15", "end_time": "21:45"})
        self.assertEqual(mgr.get_power_led_mode(), "timed")
        self.assertEqual(mgr.get_power_led_start_time(), "09:15")
        self.assertEqual(mgr.get_power_led_end_time(), "21:45")

    def test_old_keys_are_removed(self):
        """Leftover enabled/all_day keys would resurrect the old format on
        the next save and confuse any future migration."""
        mgr = self.load_old({"enabled": True, "all_day": False,
                             "start_time": "09:00", "end_time": "21:00"})
        self.assertNotIn("enabled", mgr.settings["power_led"])
        self.assertNotIn("all_day", mgr.settings["power_led"])

    def test_new_format_is_left_untouched(self):
        mgr = self.load_old({"mode": "timed", "start_time": "10:00",
                             "end_time": "20:00", "enabled": False})
        # 'mode' present -> no migration; the stale 'enabled' is ignored.
        self.assertEqual(mgr.get_power_led_mode(), "timed")


class SaveSettingsTest(TimerTestBase):

    def test_set_power_led_settings_persists_to_disk(self):
        """Settings kept only in memory would vanish on the next reboot."""
        mgr = self.make_manager()
        mgr.set_power_led_settings("timed", "06:00", "23:30")
        self.assertEqual(self.saved()["power_led"],
                         {"mode": "timed", "start_time": "06:00",
                          "end_time": "23:30"})

    def test_set_beeper_settings_persists_to_disk(self):
        mgr = self.make_manager()
        mgr.set_beeper_settings("off", "08:00", "22:00")
        self.assertEqual(self.saved()["beeper"]["mode"], "off")

    def test_unwritable_path_logs_but_does_not_raise(self):
        """A read-only overlay must not turn a settings save into a crash of
        the web request that triggered it."""
        mgr = self.make_manager()
        mgr.settings_file = os.path.join(self.tmpdir.name, "no_such_dir",
                                         "timer.json")
        mgr.save_settings()  # must not raise
        self.assertEqual(mgr.get_power_led_mode(), "on")  # state intact


class TimeRangeTest(TimerTestBase):
    """is_time_in_range with datetime.now frozen -- the math that decides
    whether the LED is lit right now."""

    def setUp(self):
        super().setUp()
        self.mgr = self.make_manager()
        self._patcher = patch.object(tm, "datetime", FrozenDateTime)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        FrozenDateTime.fixed = real_datetime(2026, 1, 1, 12, 0, 0)
        super().tearDown()

    def at(self, hour, minute):
        FrozenDateTime.fixed = real_datetime(2026, 1, 1, hour, minute, 0)

    def test_same_day_range_inside(self):
        self.at(12, 0)
        self.assertTrue(self.mgr.is_time_in_range("08:00", "22:00"))

    def test_same_day_range_outside(self):
        self.at(23, 30)
        self.assertFalse(self.mgr.is_time_in_range("08:00", "22:00"))
        self.at(7, 59)
        self.assertFalse(self.mgr.is_time_in_range("08:00", "22:00"))

    def test_same_day_boundaries_are_inclusive(self):
        """08:00-22:00 must include both endpoints, or the LED flickers off
        for the first minute of its own schedule."""
        self.at(8, 0)
        self.assertTrue(self.mgr.is_time_in_range("08:00", "22:00"))
        self.at(22, 0)
        self.assertTrue(self.mgr.is_time_in_range("08:00", "22:00"))

    def test_overnight_range_spans_midnight(self):
        """22:00-08:00 is the typical 'quiet hours inverted' schedule; it
        must be active late evening AND early morning."""
        self.at(23, 0)
        self.assertTrue(self.mgr.is_time_in_range("22:00", "08:00"))
        self.at(3, 0)
        self.assertTrue(self.mgr.is_time_in_range("22:00", "08:00"))
        self.at(12, 0)
        self.assertFalse(self.mgr.is_time_in_range("22:00", "08:00"))

    def test_garbage_time_string_reads_as_out_of_range(self):
        """A corrupt stored time must not crash the minute loop; failing
        closed (False -> device off in timed mode) is the defined behavior."""
        self.assertFalse(self.mgr.is_time_in_range("25:99", "22:00"))
        self.assertFalse(self.mgr.is_time_in_range("not a time", "22:00"))


class DesiredStateTest(TimerTestBase):

    def setUp(self):
        super().setUp()
        self.mgr = self.make_manager()
        self._patcher = patch.object(tm, "datetime", FrozenDateTime)
        self._patcher.start()
        FrozenDateTime.fixed = real_datetime(2026, 1, 1, 12, 0, 0)

    def tearDown(self):
        self._patcher.stop()
        super().tearDown()

    def test_off_mode_is_false(self):
        self.mgr.settings["power_led"]["mode"] = "off"
        self.assertFalse(self.mgr._desired_state("power_led"))

    def test_on_mode_is_true(self):
        self.mgr.settings["power_led"]["mode"] = "on"
        self.assertTrue(self.mgr._desired_state("power_led"))

    def test_unknown_mode_falls_back_to_on(self):
        """A future/corrupt mode value must not dark the LED -- matching the
        module's documented previous behavior."""
        self.mgr.settings["power_led"]["mode"] = "disco"
        self.assertTrue(self.mgr._desired_state("power_led"))

    def test_timed_mode_follows_the_clock(self):
        self.mgr.settings["beeper"] = {"mode": "timed", "start_time": "08:00",
                                       "end_time": "22:00"}
        self.assertTrue(self.mgr._desired_state("beeper"))  # noon
        FrozenDateTime.fixed = real_datetime(2026, 1, 1, 23, 0, 0)
        self.assertFalse(self.mgr._desired_state("beeper"))


class ApplyPowerLedTest(TimerTestBase):

    def test_set_socket_client_wires_the_pmu_path(self):
        """main uses set_socket_client() (not attribute pokes) to hand over
        the PMU bridge; if it stops storing the client, every apply becomes
        a silent no-op and no schedule ever reaches the hardware."""
        mgr = self.make_manager()
        client = FakeSocketClient()
        mgr.set_socket_client(client)
        mgr.apply_power_led_setting()
        self.assertEqual(client.pmu_calls, [{"status_led_v2_enabled": True}])

    def test_no_socket_client_is_a_noop(self):
        """The manager starts before the pcat-manager socket is up; applying
        then must not crash the thread."""
        mgr = self.make_manager()
        mgr.apply_power_led_setting()  # must not raise
        self.assertIsNone(mgr._applied_led_state)

    def test_first_apply_sends_on_state(self):
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_power_led_setting()
        self.assertEqual(mgr.socket_client.pmu_calls,
                         [{"status_led_v2_enabled": True}])
        self.assertIs(mgr._applied_led_state, True)

    def test_unchanged_state_is_not_resent(self):
        """Every pmu_io_set is a socket command plus an STM32 UART round-trip
        with a 1s wait; re-sending an unchanged state every minute wastes
        battery 24/7."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_power_led_setting()
        mgr.apply_power_led_setting()
        mgr.apply_power_led_setting()
        self.assertEqual(len(mgr.socket_client.pmu_calls), 1)

    def test_mode_change_sends_the_new_state(self):
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_power_led_setting()
        mgr.settings["power_led"]["mode"] = "off"
        mgr.apply_power_led_setting()
        self.assertEqual(mgr.socket_client.pmu_calls[-1],
                         {"status_led_v2_enabled": False})
        self.assertIs(mgr._applied_led_state, False)

    def test_disconnected_socket_defers_and_retries_next_tick(self):
        """If the send is skipped while the socket is down, the tracker must
        stay unset so the state is actually pushed once the socket returns --
        otherwise the LED stays wrong until the next mode change."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient(connected=False)
        mgr.apply_power_led_setting()
        self.assertEqual(mgr.socket_client.pmu_calls, [])
        self.assertIsNone(mgr._applied_led_state)
        mgr.socket_client.sock_connected = True
        mgr.apply_power_led_setting()
        self.assertEqual(mgr.socket_client.pmu_calls,
                         [{"status_led_v2_enabled": True}])

    def test_hardware_mismatch_is_resent_even_if_tracker_says_applied(self):
        """Boot races (or a dropped UART frame) can leave the PMU LED off after
        we already marked the schedule as applied. The minute loop must notice
        the hardware disagreement and re-push, or the LED stays dark until the
        next mode change / reboot."""
        mgr = self.make_manager()
        # Tracker claims ON was applied, but the PMU still reports off.
        client = FakeSocketClient(status_led_v2_enabled=0)
        mgr.socket_client = client
        mgr._applied_led_state = True
        mgr.apply_power_led_setting()
        self.assertEqual(client.pmu_calls, [{"status_led_v2_enabled": True}])

    def test_hardware_already_matching_skips_resend(self):
        """Once the PMU reports the desired state, do not burn another
        socket->UART round-trip every minute."""
        mgr = self.make_manager()
        client = FakeSocketClient(status_led_v2_enabled=1)
        mgr.socket_client = client
        mgr._applied_led_state = True
        mgr.apply_power_led_setting()
        self.assertEqual(client.pmu_calls, [])
        self.assertIs(mgr._applied_led_state, True)

    def test_int_hw_reading_zero_is_treated_as_off(self):
        """pmu-io-get returns 0/1 integers, not Python bools."""
        mgr = self.make_manager()
        mgr.settings["power_led"]["mode"] = "off"
        client = FakeSocketClient(status_led_v2_enabled=0)
        mgr.socket_client = client
        mgr._applied_led_state = False
        mgr.apply_power_led_setting()
        self.assertEqual(client.pmu_calls, [])


class ApplyBeeperTest(TimerTestBase):

    def test_no_socket_client_is_a_noop(self):
        mgr = self.make_manager()
        mgr.apply_beeper_setting()  # must not raise
        self.assertIsNone(mgr._applied_beeper_state)

    def test_sends_beeper_kwarg_not_led_kwarg(self):
        """Crossing the wires here would beep when the user asked to dim the
        LED."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_beeper_setting()
        self.assertEqual(mgr.socket_client.pmu_calls,
                         [{"beeper_enabled": True}])

    def test_unchanged_state_is_not_resent(self):
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_beeper_setting()
        mgr.apply_beeper_setting()
        self.assertEqual(len(mgr.socket_client.pmu_calls), 1)

    def test_off_mode_silences_the_beeper(self):
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.settings["beeper"]["mode"] = "off"
        mgr.apply_beeper_setting()
        self.assertEqual(mgr.socket_client.pmu_calls,
                         [{"beeper_enabled": False}])

    def test_disconnected_socket_defers(self):
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient(connected=False)
        mgr.apply_beeper_setting()
        self.assertEqual(mgr.socket_client.pmu_calls, [])
        self.assertIsNone(mgr._applied_beeper_state)


class SmartFanTest(TimerTestBase):

    def test_manual_mode_applies_saved_speed_directly(self):
        """In manual mode the user's slider value must reach the PWM as-is;
        running the smart curve instead would override their choice."""
        tm.pcat_config.get_fan_mode.return_value = "manual"
        tm.pcat_config.get_fan_speed.return_value = 7
        mgr = self.make_manager()
        mgr.apply_smart_fan_control()
        tm.pcat_config.set_fan_speed_direct.assert_called_once_with(7)
        tm.pcat_config.set_fan_speed_smart.assert_not_called()

    def test_smart_mode_feeds_temperature_and_cap_to_the_curve(self):
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient(temperature=55)
        tm.pcat_config.get_fan_mode.return_value = "smart_cool"
        tm.pcat_config.get_fan_speed.return_value = 6
        mgr.apply_smart_fan_control()
        tm.pcat_config.set_fan_speed_smart.assert_called_once_with(
            55, 6, "smart_cool")

    def test_no_socket_client_passes_none_temperature(self):
        """Before the PMU socket is up there is no board temperature; the
        curve must be told None (fan off) rather than a stale number."""
        mgr = self.make_manager()
        mgr.apply_smart_fan_control()
        args = tm.pcat_config.set_fan_speed_smart.call_args[0]
        self.assertIsNone(args[0])

    def test_none_board_temperature_passes_none(self):
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient(temperature=None)
        tm.pcat_config.get_fan_speed.return_value = None
        tm.pcat_config.set_fan_speed_smart.return_value = None
        tm.pcat_config.is_device_charging.return_value = None
        mgr.apply_smart_fan_control()  # N/A display paths must not crash
        self.assertIsNone(
            tm.pcat_config.set_fan_speed_smart.call_args[0][0])

    def test_logs_only_when_computed_speed_changes(self):
        """The change-gate exists because an unconditional log wrote ~1440
        lines/day; the charging probe (an I2C/sysfs read) must also only run
        on change."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient(temperature=50)
        tm.pcat_config.set_fan_speed_smart.return_value = 3
        mgr.apply_smart_fan_control()
        mgr.apply_smart_fan_control()
        self.assertEqual(tm.pcat_config.is_device_charging.call_count, 1)
        self.assertEqual(mgr._applied_fan_speed, 3)
        tm.pcat_config.set_fan_speed_smart.return_value = 5
        mgr.apply_smart_fan_control()
        self.assertEqual(tm.pcat_config.is_device_charging.call_count, 2)
        self.assertEqual(mgr._applied_fan_speed, 5)

    def test_config_error_is_swallowed(self):
        """A hiccup reading the fan config must not kill the once-a-minute
        loop that also drives the LED and beeper."""
        tm.pcat_config.get_fan_mode.side_effect = RuntimeError("uci broke")
        mgr = self.make_manager()
        mgr.apply_smart_fan_control()  # must not raise


class StartupSettingsTest(TimerTestBase):

    def test_without_socket_nothing_is_applied(self):
        mgr = self.make_manager()
        mgr.apply_startup_settings()  # must not raise
        tm.pcat_config.get_power_mode.assert_not_called()

    def test_applies_led_beeper_and_fan_immediately(self):
        """Waiting for the first 10s loop tick would leave the hardware in
        its boot state; startup must push the saved config right away."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_startup_settings()
        kwargs = [list(c.keys())[0] for c in mgr.socket_client.pmu_calls]
        self.assertIn("status_led_v2_enabled", kwargs)
        self.assertIn("beeper_enabled", kwargs)
        tm.pcat_config.set_fan_speed_smart.assert_called_once()

    def test_direct_power_mode_is_restored_to_the_daemon(self):
        """pcat-manager forgets power-on-mode across its own restarts; a user
        who chose direct power must not find their router refusing to boot
        headless after a daemon restart."""
        tm.pcat_config.get_power_mode.return_value = "direct_power"
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_startup_settings()
        self.assertEqual(mgr.socket_client.power_on_modes, [2])

    def test_other_power_modes_do_not_touch_power_on_mode(self):
        tm.pcat_config.get_power_mode.return_value = "manual"
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_startup_settings()
        self.assertEqual(mgr.socket_client.power_on_modes, [])


class ForceApplyTest(TimerTestBase):

    def test_force_resends_even_when_state_is_unchanged(self):
        """force_apply exists for recovery/debugging: after an MCU reset the
        tracker believes the LED is already right, but the hardware forgot.
        Force must drop the gate and re-send everything."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.apply_power_led_setting()
        mgr.apply_beeper_setting()
        self.assertEqual(len(mgr.socket_client.pmu_calls), 2)
        mgr.force_apply_settings()
        self.assertEqual(len(mgr.socket_client.pmu_calls), 4)
        # fan tracker was reset then re-populated by the forced run
        self.assertEqual(mgr._applied_fan_speed, 3)


class ThreadLifecycleTest(TimerTestBase):

    def test_start_marks_running_and_applies_startup_settings(self):
        """start() must push the saved state before the loop's first tick."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        with patch.object(tm.threading.Thread, "start") as thread_start:
            mgr.start()
        thread_start.assert_called_once()
        self.assertTrue(mgr.running)
        self.assertEqual(len(mgr.socket_client.pmu_calls), 2)

    def test_stop_flips_running_and_wakes_the_loop(self):
        """stop() must set the event, or shutdown blocks up to 60s on the
        loop's wait."""
        mgr = self.make_manager()
        with patch.object(tm.threading.Thread, "start"):
            mgr.start()
        mgr.stop()
        self.assertFalse(mgr.running)
        self.assertTrue(mgr._stop_event.is_set())

    def test_loop_first_tick_is_10s_then_60s(self):
        """The early 10s tick exists so a just-booted device applies its
        schedule quickly; steady state must back off to once a minute."""
        mgr = self.make_manager()
        mgr.socket_client = FakeSocketClient()
        mgr.running = True
        event = FakeStopEvent(mgr, ticks=3)
        mgr._stop_event = event
        mgr.run()
        self.assertEqual(event.waits, [10, 60, 60])
        # and the loop actually drove the hardware
        self.assertEqual(len(mgr.socket_client.pmu_calls), 2)

    def test_loop_survives_exceptions_and_retries_after_10s(self):
        """One bad tick (socket hiccup, corrupt config) must not kill the
        thread that controls the fan -- a dead loop means a fan stuck at its
        last speed forever."""
        mgr = self.make_manager()
        mgr.running = True
        event = FakeStopEvent(mgr, ticks=2)
        mgr._stop_event = event
        with patch.object(mgr, "apply_power_led_setting",
                          side_effect=RuntimeError("boom")):
            mgr.run()  # must not raise
        self.assertEqual(event.waits, [10, 10])



class PartialSettingsFileTest(TimerTestBase):
    """Regression for the boot crash on a partial settings file: a file from
    older firmware (or hand-edited) that lacks a whole device section used to
    replace the defaults wholesale, so get_beeper_mode() KeyError'd at boot
    and the per-minute loop errored every tick."""

    def test_missing_device_section_keeps_defaults(self):
        mgr = self.make_manager()
        with open(self.settings_path, "w") as f:
            json.dump({"power_led": {"mode": "off"}}, f)
        mgr.load_settings()
        # The section present in the file wins...
        self.assertEqual(mgr.get_power_led_mode(), "off")
        # ...the absent one keeps its defaults instead of vanishing.
        self.assertEqual(mgr.get_beeper_mode(), "on")
        self.assertEqual(mgr.settings["beeper"]["start_time"], "08:00")

    def test_partial_device_section_keeps_missing_keys(self):
        """A section with only 'mode' must not lose its default time range."""
        mgr = self.make_manager()
        with open(self.settings_path, "w") as f:
            json.dump({"beeper": {"mode": "timed"}}, f)
        mgr.load_settings()
        self.assertEqual(mgr.get_beeper_mode(), "timed")
        self.assertEqual(mgr.settings["beeper"]["end_time"], "22:00")


class McuScheduleGateTest(TimerTestBase):
    """MCU-owned schedule kicks in at RA2E1260808000+; older stays host-path."""

    def test_version_gate(self):
        self.assertFalse(tm.mcu_io_schedule_supported(None))
        self.assertFalse(tm.mcu_io_schedule_supported("RA2E1260807000"))
        self.assertTrue(tm.mcu_io_schedule_supported("RA2E1260808000"))
        self.assertTrue(tm.mcu_io_schedule_supported("RA2E1260812001"))

    def test_local_hhmm_to_utc_converts_offset(self):
        # Fixed +08:00 local so the test is TZ-independent.
        now = real_datetime(2026, 8, 8, 12, 0, 0,
                            tzinfo=timezone(timedelta(hours=8)))
        h, m = tm.local_hhmm_to_utc_hm("09:00", now=now)
        self.assertEqual((h, m), (1, 0))
        h, m = tm.local_hhmm_to_utc_hm("22:00", now=now)
        self.assertEqual((h, m), (14, 0))

    def test_new_fw_pushes_schedule_instead_of_pmu_io_set(self):
        mgr = self.make_manager()
        client = FakeSocketClient(stm32_firmware_version="RA2E1260808000")
        mgr.socket_client = client
        mgr.settings["power_led"] = {"mode": "timed", "start_time": "09:00",
                                     "end_time": "22:00"}
        mgr.settings["beeper"] = {"mode": "off", "start_time": "08:00",
                                  "end_time": "22:00"}
        mgr.apply_power_led_setting()
        self.assertEqual(client.pmu_calls, [])  # no immediate mask path
        self.assertEqual(len(client.schedule_calls), 1)
        self.assertGreaterEqual(client.time_sync_calls, 1)
        led, beeper = client.schedule_calls[0]
        self.assertEqual(led["mode"], "timed")
        self.assertEqual(beeper["mode"], "off")
        self.assertEqual(mgr.schedule_owner(), "mcu")

    def test_old_fw_still_uses_pmu_io_set(self):
        mgr = self.make_manager()
        client = FakeSocketClient(stm32_firmware_version="RA2E1260807000")
        mgr.socket_client = client
        mgr.apply_power_led_setting()
        self.assertEqual(len(client.pmu_calls), 1)
        self.assertEqual(client.schedule_calls, [])
        self.assertEqual(mgr.schedule_owner(), "host")

    def test_mcu_path_skips_identical_repush(self):
        mgr = self.make_manager()
        client = FakeSocketClient(stm32_firmware_version="RA2E1260808000")
        mgr.socket_client = client
        mgr.apply_power_led_setting()
        mgr.apply_power_led_setting()
        mgr.apply_beeper_setting()
        self.assertEqual(len(client.schedule_calls), 1)


class RawPayloadTest(unittest.TestCase):
    """The raw-passthrough fallback hand-encodes the MCU wire shapes; a wrong
    byte here is silently rejected by the MCU's length/range checks."""

    def test_time_sync_payload_is_utc_year_le(self):
        dt = real_datetime(2026, 8, 12, 4, 26, 35, tzinfo=timezone.utc)
        # 2026 = 0x07EA little-endian, then month/day/hour/minute/second.
        self.assertEqual(tm.utc_time_sync_payload_hex(dt), "ea07080c041a23")

    def test_io_schedule_payload_led_then_beeper(self):
        led = {"mode": "timed", "start_hour": 1, "start_minute": 0,
               "end_hour": 14, "end_minute": 0}
        beeper = {"mode": "off", "start_hour": 0, "start_minute": 0,
                  "end_hour": 14, "end_minute": 30}
        self.assertEqual(tm.io_schedule_payload_hex(led, beeper),
                         "0201000e000000000e1e")

    def test_reply_match_requires_exact_schedule(self):
        led = {"mode": "timed", "start_hour": 1, "start_minute": 0,
               "end_hour": 14, "end_minute": 0}
        beeper = {"mode": "off", "start_hour": 0, "start_minute": 0,
                  "end_hour": 0, "end_minute": 0}
        reply = {"valid": 1, "led": dict(led), "beeper": dict(beeper)}
        self.assertTrue(tm.schedule_reply_matches(reply, led, beeper))
        self.assertFalse(tm.schedule_reply_matches(None, led, beeper))
        self.assertFalse(tm.schedule_reply_matches(
            dict(reply, valid=0), led, beeper))
        wrong = dict(reply, led=dict(led, end_hour=13))
        self.assertFalse(tm.schedule_reply_matches(wrong, led, beeper))
        wrong = dict(reply, beeper=dict(beeper, mode="on"))
        self.assertFalse(tm.schedule_reply_matches(wrong, led, beeper))


class DeliveryFallbackTest(TimerTestBase):
    """The daemon this feature first shipped against silently dropped the
    schedule commands, so every UI change was recorded as pushed and nothing
    ever happened (forum report r7897). Delivery is now proven, and degrades
    one rung at a time."""

    FW = "RA2E1260809000"

    def _manager_with(self, client):
        mgr = self.make_manager()
        mgr.socket_client = client
        return mgr

    def test_verified_named_delivery(self):
        client = FakeSocketClient(stm32_firmware_version=self.FW)
        mgr = self._manager_with(client)
        self.assertTrue(mgr.push_mcu_schedule(force=True))
        self.assertEqual(mgr.schedule_delivery(), "named")
        self.assertEqual(client.raw_calls, [],
                         "no raw fallback when the daemon delivers")

    def test_silently_dropped_commands_fall_back_to_raw(self):
        client = FakeSocketClient(stm32_firmware_version=self.FW,
                                  schedule_supported=False)
        mgr = self._manager_with(client)
        mgr.settings["power_led"] = {"mode": "timed", "start_time": "09:00",
                                     "end_time": "22:00"}
        self.assertTrue(mgr.push_mcu_schedule(force=True))
        self.assertEqual(mgr.schedule_delivery(), "raw")
        cmds = [c for c, _p in client.raw_calls]
        self.assertEqual(cmds, [tm.MCU_CMD_SET_TIME,
                                tm.MCU_CMD_IO_SCHEDULE_SET],
                         "time sync must land before the schedule: the "
                         "engine holds timed channels on until it does")
        time_payload, sched_payload = (p for _c, p in client.raw_calls)
        self.assertEqual(len(time_payload), 14)   # 7 bytes hex
        self.assertEqual(len(sched_payload), 20)  # 10 bytes hex
        self.assertEqual(sched_payload[:2], "02", "led mode timed")

    def test_no_passthrough_at_all_ends_in_host_reassert(self):
        client = FakeSocketClient(stm32_firmware_version=self.FW,
                                  schedule_supported=False,
                                  raw_supported=False)
        mgr = self._manager_with(client)
        self.assertFalse(mgr.push_mcu_schedule(force=True))
        self.assertEqual(mgr.schedule_delivery(), "host-reassert")

    def test_host_reassert_resends_every_tick(self):
        # The engine re-applies its default (everything on) at every minute
        # boundary and the io-get cache cannot see it, so change-gating would
        # send once and lose the fight forever.
        client = FakeSocketClient(stm32_firmware_version=self.FW,
                                  schedule_supported=False,
                                  raw_supported=False)
        mgr = self._manager_with(client)
        mgr.settings["power_led"]["mode"] = "off"
        mgr.apply_power_led_setting()
        mgr.apply_power_led_setting()
        mgr.apply_power_led_setting()
        led_sets = [c for c in client.pmu_calls
                    if "status_led_v2_enabled" in c]
        self.assertEqual(len(led_sets), 3)
        self.assertTrue(all(c["status_led_v2_enabled"] is False
                            for c in led_sets))

    def test_failed_delivery_is_not_reprobed_every_minute(self):
        client = FakeSocketClient(stm32_firmware_version=self.FW,
                                  schedule_supported=False,
                                  raw_supported=False)
        mgr = self._manager_with(client)
        mgr.push_mcu_schedule(force=True)
        probes_after_verdict = len(client.schedule_calls)
        mgr.push_mcu_schedule(force=False)
        mgr.push_mcu_schedule(force=False)
        self.assertEqual(len(client.schedule_calls), probes_after_verdict,
                         "the cooldown must hold between unforced pushes")

    def test_user_save_retries_delivery_immediately(self):
        client = FakeSocketClient(stm32_firmware_version=self.FW,
                                  schedule_supported=False,
                                  raw_supported=False)
        mgr = self._manager_with(client)
        mgr.push_mcu_schedule(force=True)
        # The daemon got upgraded underneath us; a forced push (user save)
        # must probe again rather than sit out the cooldown.
        client.schedule_supported = True
        self.assertTrue(mgr.push_mcu_schedule(force=True))
        self.assertEqual(mgr.schedule_delivery(), "named")

    def test_recovered_delivery_reverts_to_named(self):
        client = FakeSocketClient(stm32_firmware_version=self.FW,
                                  schedule_supported=False)
        mgr = self._manager_with(client)
        self.assertTrue(mgr.push_mcu_schedule(force=True))
        self.assertEqual(mgr.schedule_delivery(), "raw")
        client.schedule_supported = True
        self.assertTrue(mgr.push_mcu_schedule(force=True))
        self.assertEqual(mgr.schedule_delivery(), "named")


class McuRtcUpkeepTest(TimerTestBase):
    """The MCU evaluates our windows against its own RTC, so that RTC is part
    of the schedule -- and pushing the windows once does not keep it right.

    Field failure this covers: two days after boot the MCU RTC was hours off,
    so a 09:00-22:00 window lit the LED at 02:00. Every host-side view still
    looked correct (the schedule read-back matched byte for byte, the UI badge
    showed what the MCU reported) and nothing re-synced, because the only sync
    lived inside the change-gated push."""

    FW = "RA2E1260809000"

    def setUp(self):
        super().setUp()
        self._patcher = patch.object(tm, "datetime", FrozenDateTime)
        self._patcher.start()
        self.at(2, 0)  # night: outside the 09:00-22:00 window below
        self.client = FakeSocketClient(stm32_firmware_version=self.FW)
        self.mgr = self.make_manager()
        self.mgr.socket_client = self.client
        self.mgr.settings["power_led"] = {"mode": "timed",
                                          "start_time": "09:00",
                                          "end_time": "22:00"}
        self.mgr.settings["beeper"] = {"mode": "on", "start_time": "08:00",
                                       "end_time": "22:00"}
        self.assertTrue(self.mgr.push_mcu_schedule(force=True))
        self.pushes = len(self.client.schedule_calls)
        self.syncs = self.client.time_sync_calls

    def tearDown(self):
        self._patcher.stop()
        FrozenDateTime.fixed = real_datetime(2026, 1, 1, 12, 0, 0)
        super().tearDown()

    def at(self, hour, minute):
        FrozenDateTime.fixed = real_datetime(2026, 1, 1, hour, minute, 0)

    def engine_reports(self, led_on, beeper_on=True):
        """What the MCU says its own engine is currently driving."""
        self.client.io_schedule["current_led"] = 1 if led_on else 0
        self.client.io_schedule["current_beeper"] = 1 if beeper_on else 0

    def age_verify(self):
        self.mgr._last_schedule_verify -= tm.MCU_VERIFY_SECONDS + 1

    def test_fresh_delivery_is_not_immediately_reverified(self):
        """A push is itself a sync plus a read-back; re-reading it seconds
        later would just spend UART on the timer thread every minute."""
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(self.client.schedule_gets, 0)
        self.assertEqual(self.client.time_sync_calls, self.syncs)

    def test_rtc_is_resynced_on_a_cadence(self):
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(self.client.time_sync_calls, self.syncs,
                         "nothing is due yet")
        self.mgr._last_rtc_sync -= tm.MCU_RTC_RESYNC_SECONDS + 1
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(self.client.time_sync_calls, self.syncs + 1)

    def test_engine_agreeing_with_us_is_left_alone(self):
        self.engine_reports(led_on=False)  # 02:00 with a 09:00-22:00 window
        self.age_verify()
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(self.client.schedule_gets, 1)
        self.assertEqual(len(self.client.schedule_calls), self.pushes,
                         "a healthy MCU must not be re-pushed")

    def test_led_lit_outside_its_window_forces_resync_and_repush(self):
        self.engine_reports(led_on=True)  # the reported field failure
        self.age_verify()
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(len(self.client.schedule_calls), self.pushes + 1)
        self.assertEqual(self.client.time_sync_calls, self.syncs + 1)

    def test_beeper_disagreement_also_heals(self):
        self.engine_reports(led_on=False, beeper_on=False)  # mode is 'on'
        self.age_verify()
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(len(self.client.schedule_calls), self.pushes + 1)

    def test_disagreement_at_a_window_edge_is_tolerated(self):
        """Host and MCU minute boundaries never line up exactly, so one
        minute either side of an edge is expected disagreement, not a bad
        clock -- healing there would re-push twice a day forever."""
        self.at(8, 59)
        self.engine_reports(led_on=True)
        self.age_verify()
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(len(self.client.schedule_calls), self.pushes)

    def test_mcu_that_lost_our_schedule_is_repushed(self):
        """An MCU reset drops back to its own defaults; the read-back stops
        matching and must be corrected without waiting for a user save."""
        self.client.io_schedule["led"] = {"mode": "on", "start_hour": 0,
                                          "start_minute": 0, "end_hour": 0,
                                          "end_minute": 0}
        self.age_verify()
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(len(self.client.schedule_calls), self.pushes + 1)
        self.assertEqual(self.client.time_sync_calls, self.syncs + 1)

    def test_verification_is_rate_limited(self):
        self.engine_reports(led_on=False)
        self.age_verify()
        self.mgr.maintain_mcu_schedule()
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(self.client.schedule_gets, 1)

    def test_blind_raw_delivery_syncs_but_does_not_read_back(self):
        client = FakeSocketClient(stm32_firmware_version=self.FW,
                                  schedule_supported=False)
        mgr = self.make_manager()
        mgr.socket_client = client
        self.assertTrue(mgr.push_mcu_schedule(force=True))
        self.assertEqual(mgr.schedule_delivery(), "raw")
        gets_after_push = client.schedule_gets
        syncs_after_push = client.time_sync_calls
        mgr._last_rtc_sync -= tm.MCU_RTC_RESYNC_SECONDS + 1
        mgr._last_schedule_verify -= tm.MCU_VERIFY_SECONDS + 1
        mgr.maintain_mcu_schedule()
        self.assertEqual(client.time_sync_calls, syncs_after_push + 1,
                         "the RTC still needs keeping even when blind")
        self.assertEqual(client.schedule_gets, gets_after_push,
                         "nothing to verify against on the blind rung")

    def test_host_scheduled_devices_are_not_touched(self):
        """Old firmware evaluates nothing on its own -- no RTC to keep."""
        client = FakeSocketClient(stm32_firmware_version="RA2E1260807000")
        mgr = self.make_manager()
        mgr.socket_client = client
        mgr.maintain_mcu_schedule()
        self.assertEqual(client.time_sync_calls, 0)
        self.assertEqual(client.schedule_gets, 0)

    def test_disconnected_socket_is_a_noop(self):
        self.client.sock_connected = False
        self.mgr._last_rtc_sync -= tm.MCU_RTC_RESYNC_SECONDS + 1
        self.mgr.maintain_mcu_schedule()
        self.assertEqual(self.client.time_sync_calls, self.syncs)

    def test_minute_loop_runs_the_upkeep(self):
        """The upkeep only helps if the loop actually calls it."""
        self.mgr._stop_event = FakeStopEvent(self.mgr, ticks=2)
        self.mgr.running = True
        self.mgr._last_rtc_sync -= tm.MCU_RTC_RESYNC_SECONDS + 1
        self.mgr.run()
        self.assertEqual(self.client.time_sync_calls, self.syncs + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
