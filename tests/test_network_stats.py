#!/usr/bin/env python3
"""Unit tests for app/network_stats.py -- the per-device byte counters behind
the /stats data-usage page.

WHY: these numbers are what users check against their SIM data quota, and the
counters live on the /etc eMMC overlay -- so both the arithmetic (counter
resets, batched flushes, day/week/month windows) and the write discipline
(RAM-batched deltas, retention cleanup, reset+VACUUM) must hold or users see
wrong quota numbers or worn flash. Everything runs against a temp sqlite file;
/sys, /proc and /tmp readers are patched, so no host interface counters or
bootup marker files leak into the results.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_network_stats`.
"""

import logging
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import mock_open, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# tests/run_tests.py wraps the suite in a patch.dict mocking `pcat_config`,
# which makes the real pcat_util.pcat_logger crash at import. Import the real
# pcat_util PACKAGE, then graft a lightweight pcat_logger stub on top --
# network_stats/database only call logger.debug/info/warning.
import pcat_util  # noqa: E402  -- real package, empty __init__.py
if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_network_stats")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

import network_stats  # noqa: E402

BOOTUP_MARKER = "/tmp/pcat-manager-web-bootup"


class HelperFunctionTests(unittest.TestCase):
    """Module-level /sys readers, with the filesystem fully faked."""

    def test_get_bridge_members_lists_brif(self):
        with patch.object(network_stats.os, "listdir",
                          return_value=["eth1", "phy0-ap0"]) as ls:
            self.assertEqual(network_stats.get_bridge_members("br-lan"),
                             ["eth1", "phy0-ap0"])
        ls.assert_called_once_with("/sys/class/net/br-lan/brif")

    def test_get_bridge_members_missing_bridge_is_empty(self):
        """A down/absent bridge must yield [] (triggers the egress fallback),
        not an exception in the sampling thread."""
        for exc in (FileNotFoundError, NotADirectoryError, PermissionError):
            with patch.object(network_stats.os, "listdir", side_effect=exc):
                self.assertEqual(network_stats.get_bridge_members("br-lan"), [])

    def test_get_sta_devices_filters_sta_netdevs(self):
        """Only phy*-sta* devices count as upstream WiFi; APs and wired
        interfaces must not be misclassified as the WAN link."""
        with patch.object(network_stats.os, "listdir",
                          return_value=["eth0", "phy0-ap0", "phy1-sta0", "lo",
                                        "br-lan", "phy0-sta0"]):
            self.assertEqual(sorted(network_stats.get_sta_devices()),
                             ["phy0-sta0", "phy1-sta0"])

    def test_get_sta_devices_unreadable_sysfs_is_empty(self):
        with patch.object(network_stats.os, "listdir",
                          side_effect=FileNotFoundError):
            self.assertEqual(network_stats.get_sta_devices(), [])

    def test_get_device_x_bytes_reads_counter(self):
        with patch("builtins.open", mock_open(read_data="12345\n")) as m:
            self.assertEqual(network_stats.get_device_x_bytes("usb0", "rx"), 12345)
        m.assert_called_once_with("/sys/class/net/usb0/statistics/rx_bytes")

    def test_get_device_x_bytes_missing_device_is_zero(self):
        """A vanished netdev (modem reset, WiFi down) must read as 0, not
        crash the periodic sampler."""
        with patch("builtins.open", side_effect=FileNotFoundError):
            self.assertEqual(network_stats.get_device_x_bytes("usb0", "rx"), 0)

    def test_get_device_x_bytes_garbage_content_is_zero(self):
        with patch("builtins.open", mock_open(read_data="not-a-number\n")):
            self.assertEqual(network_stats.get_device_x_bytes("usb0", "tx"), 0)


class NetworkStatsTestBase(unittest.TestCase):
    """Builds a NetworkStats against a temp DB, with the /tmp bootup marker
    faked so the host filesystem is never touched."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="netstats-test-")
        self.db = os.path.join(self.tmpdir.name, "stats.db")
        self.stats = self.make_stats(bootup_marker_exists=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def make_stats(self, bootup_marker_exists=True):
        real_exists = os.path.exists

        def fake_exists(path):
            if path == BOOTUP_MARKER:
                return bootup_marker_exists
            return real_exists(path)

        with patch.object(network_stats.os.path, "exists",
                          side_effect=fake_exists):
            if bootup_marker_exists:
                return network_stats.NetworkStats(self.db)
            with patch("builtins.open", mock_open()) as m:
                stats = network_stats.NetworkStats(self.db)
            self._marker_open = m
            return stats

    def insert_row(self, device, rx, tx, created_at):
        con = sqlite3.connect(self.db)
        con.execute(
            "insert into network_stats(rx_bytes,tx_bytes,device,remark,created_at)"
            "values(?,?,?,?,?)", (rx, tx, device, "", created_at))
        con.commit()
        con.close()

    def db_rows(self):
        con = sqlite3.connect(self.db)
        rows = list(con.execute(
            "select device, rx_bytes, tx_bytes, created_at from network_stats"))
        con.close()
        return rows

    def now_str(self, delta=timedelta()):
        return (datetime.now() + delta).strftime("%Y-%m-%d %H:%M:%S")


class InitTests(NetworkStatsTestBase):

    def test_creates_schema_and_index(self):
        con = sqlite3.connect(self.db)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        indexes = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")]
        con.close()
        self.assertIn("network_stats", tables)
        self.assertIn("idx_network_stats_device_created", indexes,
                      "SUM queries need this index or the stats page crawls")

    def test_index_backfilled_on_existing_db(self):
        """DBs created before the index existed must get it on the next boot;
        init_db alone won't reapply schema to an existing file."""
        con = sqlite3.connect(self.db)
        con.execute("DROP INDEX idx_network_stats_device_created")
        con.commit()
        con.close()
        self.make_stats(bootup_marker_exists=True)  # simulated next boot
        con = sqlite3.connect(self.db)
        indexes = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")]
        con.close()
        self.assertIn("idx_network_stats_device_created", indexes)

    def test_bootup_flag_false_when_marker_present(self):
        self.assertFalse(self.stats.device_bootup)

    def test_bootup_flag_true_and_marker_created_when_absent(self):
        """First run after power-on must both report bootup and drop the
        marker so later restarts of just the web app don't re-trigger
        boot-time behavior."""
        stats = self.make_stats(bootup_marker_exists=False)
        self.assertTrue(stats.device_bootup)
        self._marker_open.assert_called_once_with(BOOTUP_MARKER, "w")


class LogNetDeviceBytesTests(NetworkStatsTestBase):

    def _sample(self, device, rx, tx):
        with patch.object(network_stats, "get_device_x_bytes",
                          side_effect=lambda d, direction:
                          rx if direction == "rx" else tx):
            self.stats.log_net_device_bytes(device)

    def test_first_sample_only_sets_baseline(self):
        """The first reading after start is a baseline, not usage -- counting
        it would bill the user for all traffic since the kernel booted."""
        self._sample("usb0", 1000, 500)
        self.assertEqual(self.stats._pending, {})
        self.assertEqual(self.stats.last_rx_bytes["usb0"], 1000)
        self.assertEqual(self.stats.last_tx_bytes["usb0"], 500)

    def test_second_sample_accumulates_delta(self):
        self._sample("usb0", 1000, 500)
        self._sample("usb0", 1600, 700)
        self.assertEqual(self.stats._pending["usb0"], [600, 200])

    def test_deltas_accumulate_across_samples_until_flush(self):
        self._sample("usb0", 1000, 500)
        self._sample("usb0", 1100, 600)
        self._sample("usb0", 1300, 900)
        self.assertEqual(self.stats._pending["usb0"], [300, 400])

    def test_counter_reset_counts_from_zero(self):
        """FM350 (eth2) resets its counters on every reconnect; the traffic
        since the reset must still be billed, and the baseline must advance so
        it is not billed twice."""
        self._sample("eth2", 5000, 4000)
        self._sample("eth2", 300, 200)  # reset happened
        self.assertEqual(self.stats._pending["eth2"], [300, 200])
        self._sample("eth2", 300, 200)  # no traffic since
        self.assertEqual(self.stats._pending["eth2"], [300, 200],
                         "reset traffic must not be re-counted")

    def test_zero_delta_creates_no_pending_entry(self):
        """Idle interfaces must not generate rows -- that's the flash-wear
        point of the batching."""
        self._sample("eth0", 1000, 500)
        self._sample("eth0", 1000, 500)
        self.assertEqual(self.stats._pending, {})


class FlushPendingTests(NetworkStatsTestBase):

    def test_flush_writes_one_row_per_device_and_clears_pending(self):
        self.stats._pending = {"usb0": [600, 200], "eth0": [10, 20]}
        self.stats.flush_pending()
        self.assertEqual(self.stats._pending, {})
        rows = {r[0]: (r[1], r[2]) for r in self.db_rows()}
        self.assertEqual(rows, {"usb0": (600, 200), "eth0": (10, 20)})

    def test_flush_with_nothing_pending_writes_nothing(self):
        before = time.time()
        self.stats.flush_pending()
        self.assertEqual(self.db_rows(), [])
        self.assertGreaterEqual(self.stats._last_flush, before,
                                "flush window must restart even on a no-op")

    def test_failed_flush_requeues_deltas_for_next_attempt(self):
        """A transient write failure (locked DB, full flash) must not lose the
        accumulated usage -- it merges back and lands on the next flush."""
        self.stats._pending = {"usb0": [600, 200]}
        with patch.object(network_stats.sqlite3, "connect",
                          side_effect=sqlite3.OperationalError("disk I/O error")):
            self.stats.flush_pending()  # must not raise
        self.assertEqual(self.stats._pending, {"usb0": [600, 200]})
        # More traffic arrives, then a healthy flush merges old + new.
        self.stats._pending["usb0"][0] += 50
        self.stats._pending["usb0"][1] += 5
        self.stats.flush_pending()
        rows = self.db_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][1], rows[0][2]), (650, 205))


class LogStatsTests(NetworkStatsTestBase):

    def _run_log_stats(self, counters, sta_devices=()):
        def fake_bytes(device, direction):
            rx, tx = counters.get(device, (0, 0))
            return rx if direction == "rx" else tx
        with patch.object(network_stats, "get_device_x_bytes",
                          side_effect=fake_bytes), \
             patch.object(network_stats, "get_sta_devices",
                          return_value=list(sta_devices)):
            self.stats.log_stats()

    def test_samples_all_egress_devices_including_wifi_sta(self):
        """Every possible egress -- both modems, wired WAN and the WiFi STA --
        must be sampled each tick; a counter not sampled now is unrecoverable
        history."""
        first = {"usb0": (10, 1), "wwan0": (20, 2), "eth2": (30, 3),
                 "eth0": (40, 4), "phy1-sta0": (50, 5)}
        second = {d: (rx + 100, tx + 7) for d, (rx, tx) in first.items()}
        self._run_log_stats(first, sta_devices=["phy1-sta0"])
        self._run_log_stats(second, sta_devices=["phy1-sta0"])
        self.assertEqual(self.stats._pending, {
            "usb0": [100, 7], "wwan0": [100, 7], "eth2": [100, 7],
            "eth0": [100, 7], "phy1-sta0": [100, 7]})

    def test_flush_only_after_interval_elapses(self):
        """Writes must stay batched inside the flush window (flash wear), then
        land automatically once the window elapses."""
        first = {"usb0": (10, 1)}
        second = {"usb0": (110, 8)}
        self._run_log_stats(first)
        self._run_log_stats(second)
        self.assertEqual(self.db_rows(), [],
                         "no flash write inside the flush window")
        self.stats._last_flush = time.time() - self.stats.FLUSH_INTERVAL - 1
        self._run_log_stats(second)  # unchanged counters; just crosses window
        rows = self.db_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1], rows[0][2]), ("usb0", 100, 7))


class SpeedTests(NetworkStatsTestBase):
    """log_rx_speed / log_tx_speed feed the live gauge on the web UI."""

    def _rx_speed(self, values, members, stas=(), calls=None):
        def fake_bytes(device, direction):
            if calls is not None:
                calls.append((device, direction))
            return values.get((device, direction), 0)
        with patch.object(network_stats, "get_device_x_bytes",
                          side_effect=fake_bytes), \
             patch.object(network_stats, "get_bridge_members",
                          return_value=list(members)), \
             patch.object(network_stats, "get_sta_devices",
                          return_value=list(stas)):
            return self.stats.log_rx_speed()

    def _tx_speed(self, values, members, stas=(), calls=None):
        def fake_bytes(device, direction):
            if calls is not None:
                calls.append((device, direction))
            return values.get((device, direction), 0)
        with patch.object(network_stats, "get_device_x_bytes",
                          side_effect=fake_bytes), \
             patch.object(network_stats, "get_bridge_members",
                          return_value=list(members)), \
             patch.object(network_stats, "get_sta_devices",
                          return_value=list(stas)):
            return self.stats.log_tx_speed()

    def test_first_sample_reports_zero(self):
        """The gauge must not spike to 'all bytes since boot' on its first
        paint."""
        values = {("eth1", "tx"): 1000}
        self.assertEqual(self._rx_speed(values, members=["eth1"]), 0)

    def test_download_sums_lan_member_tx_deltas(self):
        """Client download = bytes the router sends to clients, summed over
        all bridge members (wired + WiFi clients)."""
        calls = []
        v1 = {("eth1", "tx"): 1000, ("phy0-ap0", "tx"): 2000}
        self._rx_speed(v1, members=["eth1", "phy0-ap0"], calls=calls)
        self.assertTrue(all(direction == "tx" for _, direction in calls),
                        "download must read LAN member tx, not rx")
        v2 = {("eth1", "tx"): 1500, ("phy0-ap0", "tx"): 2300}
        self.assertEqual(self._rx_speed(v2, members=["eth1", "phy0-ap0"]), 800)

    def test_upload_sums_lan_member_rx_deltas(self):
        v1 = {("eth1", "rx"): 100}
        self._tx_speed(v1, members=["eth1"])
        v2 = {("eth1", "rx"): 460}
        self.assertEqual(self._tx_speed(v2, members=["eth1"]), 360)

    def test_member_counter_reset_does_not_zero_whole_speed(self):
        """One member resetting (WiFi client roams off) must not clamp the
        aggregate to 0 -- other members' traffic still counts, and the reset
        member counts from 0."""
        v1 = {("eth1", "tx"): 10000, ("phy0-ap0", "tx"): 5000}
        self._rx_speed(v1, members=["eth1", "phy0-ap0"])
        v2 = {("eth1", "tx"): 10400, ("phy0-ap0", "tx"): 250}  # ap0 reset
        self.assertEqual(self._rx_speed(v2, members=["eth1", "phy0-ap0"]),
                         400 + 250)

    def test_vanished_member_baseline_is_dropped(self):
        """An interface that disappears and later reappears with a low counter
        must be re-baselined, not billed a huge negative-then-reset jump."""
        v1 = {("eth1", "tx"): 1000, ("phy0-ap0", "tx"): 90000}
        self._rx_speed(v1, members=["eth1", "phy0-ap0"])
        v2 = {("eth1", "tx"): 1100}
        self.assertEqual(self._rx_speed(v2, members=["eth1"]), 100)
        self.assertNotIn("phy0-ap0", self.stats.last_log_rx_speed)
        # Reappears with a fresh counter: contributes nothing this sample
        # (fresh baseline), instead of a bogus spike.
        v3 = {("eth1", "tx"): 1150, ("phy0-ap0", "tx"): 40}
        self.assertEqual(self._rx_speed(v3, members=["eth1", "phy0-ap0"]), 50)

    def test_no_bridge_members_falls_back_to_egress_with_flipped_direction(self):
        """With the bridge down, download speed comes from egress rx (and must
        include the WiFi STA, or WiFi-as-WAN would show 0)."""
        calls = []
        v1 = {("usb0", "rx"): 1000, ("phy1-sta0", "rx"): 2000}
        self._rx_speed(v1, members=[], stas=["phy1-sta0"], calls=calls)
        sampled = {d for d, _ in calls}
        self.assertEqual(sampled,
                         set(self.stats.SPEED_DEVICES) | {"phy1-sta0"})
        self.assertTrue(all(direction == "rx" for _, direction in calls),
                        "egress fallback for download must read rx")
        v2 = {("usb0", "rx"): 1400, ("phy1-sta0", "rx"): 2100}
        self.assertEqual(self._rx_speed(v2, members=[], stas=["phy1-sta0"]),
                         500)


class UsageWindowTests(NetworkStatsTestBase):
    """today/this-week/month sums -- the numbers users compare to their SIM
    quota."""

    MOBILE_DEVICES = ("usb0", "wwan0", "eth2")

    def test_today_sums_all_mobile_devices_rx_plus_tx(self):
        for dev in self.MOBILE_DEVICES:
            self.insert_row(dev, 100, 10, self.now_str())
        self.insert_row("eth0", 7000, 700, self.now_str())      # wired: excluded
        self.insert_row("phy1-sta0", 999, 99, self.now_str())   # wifi: excluded
        self.assertEqual(self.stats.today_bytes("mobile"), 330)

    def test_today_wired_counts_only_eth0(self):
        self.insert_row("eth0", 7000, 700, self.now_str())
        self.insert_row("usb0", 100, 10, self.now_str())
        self.assertEqual(self.stats.today_bytes("wired"), 7700)

    def test_today_excludes_yesterday(self):
        self.insert_row("usb0", 500, 50, self.now_str(-timedelta(days=1)))
        self.insert_row("usb0", 100, 10, self.now_str())
        self.assertEqual(self.stats.today_bytes("mobile"), 110)

    def test_empty_table_reports_zero_not_none(self):
        """SUM over no rows yields NULL; the UI must get 0, not crash on
        None arithmetic."""
        self.assertEqual(self.stats.today_bytes("mobile"), 0)
        self.assertEqual(self.stats.this_week_bytes("wired"), 0)
        self.assertEqual(self.stats.month_bytes(date.today(), "mobile"), 0)

    def test_unflushed_deltas_are_visible_immediately(self):
        """Opening the stats page must show usage from the current (not yet
        flushed) window -- users would otherwise see numbers up to 10 minutes
        stale after heavy traffic."""
        self.stats._pending = {"usb0": [600, 200]}
        self.assertEqual(self.stats.today_bytes("mobile"), 800)
        self.assertEqual(self.stats._pending, {}, "sum must flush first")

    def test_week_window_is_monday_through_sunday(self):
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        self.insert_row("usb0", 100, 10,
                        monday.strftime("%Y-%m-%d 00:00:00"))       # in
        self.insert_row("usb0", 40, 4,
                        (monday + timedelta(days=6)).strftime("%Y-%m-%d 23:59:59"))  # in
        self.insert_row("usb0", 9000, 900,
                        (monday - timedelta(days=1)).strftime("%Y-%m-%d 23:59:59"))  # out
        self.assertEqual(self.stats.this_week_bytes("mobile"), 154)

    def test_week_wired_counts_only_eth0(self):
        self.insert_row("eth0", 100, 10, self.now_str())
        self.insert_row("usb0", 5000, 500, self.now_str())
        self.assertEqual(self.stats.this_week_bytes("wired"), 110)

    def test_month_window_covers_first_to_last_day_inclusive(self):
        """The end-of-month arithmetic (28th + 4 days trick) must include the
        31st and exclude the neighboring months, or January usage bleeds into
        February's bill."""
        self.insert_row("usb0", 100, 10, "2026-01-01 00:00:00")   # in
        self.insert_row("usb0", 40, 4, "2026-01-31 23:59:59")     # in
        self.insert_row("usb0", 7000, 700, "2025-12-31 23:59:59")  # out
        self.insert_row("usb0", 9000, 900, "2026-02-01 00:00:00")  # out
        self.assertEqual(self.stats.month_bytes(date(2026, 1, 15), "mobile"), 154)

    def test_month_wired_counts_only_eth0(self):
        self.insert_row("eth0", 100, 10, "2026-01-10 12:00:00")
        self.insert_row("usb0", 5000, 500, "2026-01-10 12:00:00")
        self.assertEqual(self.stats.month_bytes(date(2026, 1, 15), "wired"), 110)

    def test_unknown_network_type_returns_zero(self):
        """An unrecognized category must not crash the stats request handler:
        before the fix, anything other than 'mobile'/'wired' left sql_command
        unassigned and raised UnboundLocalError -- which would have bitten the
        moment a 'wifi' category was added to the UI. All three windows share
        the guard."""
        from datetime import date
        self.assertEqual(self.stats.today_bytes("wifi"), 0)
        self.assertEqual(self.stats.this_week_bytes("wifi"), 0)
        self.assertEqual(self.stats.month_bytes(date.today(), "wifi"), 0)


class RetentionAndResetTests(NetworkStatsTestBase):

    def test_cleanup_old_drops_only_rows_past_retention(self):
        """Without retention the table grows forever and the SUM queries slow
        the stats page; but recent history must survive the daily cleanup."""
        old = (datetime.now()
               - timedelta(days=self.stats.RETENTION_DAYS + 5)).strftime(
                   "%Y-%m-%d 12:00:00")
        self.insert_row("usb0", 9000, 900, old)
        self.insert_row("usb0", 100, 10, self.now_str())
        self.stats.cleanup_old()
        rows = self.db_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][1], rows[0][2]), (100, 10))

    def test_clear_resets_rows_and_unflushed_deltas(self):
        """The user-facing 'reset statistics' must clear both the DB and the
        RAM-pending deltas -- otherwise the next flush resurrects pre-reset
        usage. The VACUUM after the delete must also succeed (the delete is
        committed first, so this does not hit the VACUUM-inside-transaction
        failure that battery_history's cleanup once had)."""
        self.insert_row("usb0", 9000, 900, self.now_str())
        self.stats._pending = {"usb0": [600, 200]}
        self.stats.clear_net_device_bytes()
        self.assertEqual(self.db_rows(), [])
        self.assertEqual(self.stats._pending, {})
        self.assertEqual(self.stats.today_bytes("mobile"), 0,
                         "pending deltas must not resurrect after reset")


if __name__ == "__main__":
    unittest.main()
