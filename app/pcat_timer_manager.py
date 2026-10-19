# -*- coding: utf-8 -*-
"""
Timer manager for handling scheduled power LED and beeper control
"""

import threading
import time
import json
import os
from datetime import datetime, time as datetime_time, timezone, timedelta
from pcat_util import pcat_logger
import pcat_config

# MCU firmware that owns LED/beeper daily schedules itself (UTC RTC).
# Older firmware keeps the Python minute-loop path below.
MCU_IO_SCHEDULE_MIN_FW = "RA2E1260808000"


def _mcu_version_sort_key(version):
    """Sortable key for RA2E1YYMMDDNNN (14 chars) → int of trailing 9 digits."""
    if not version or len(version) < 14:
        return None
    digits = version[5:14]
    if not digits.isdigit():
        return None
    return int(digits)


def mcu_io_schedule_supported(version):
    """True when MCU fw is RA2E1260808000 or newer."""
    ver_key = _mcu_version_sort_key(version)
    min_key = _mcu_version_sort_key(MCU_IO_SCHEDULE_MIN_FW)
    if ver_key is None or min_key is None:
        return False
    return ver_key >= min_key


def local_hhmm_to_utc_hm(hhmm, now=None):
    """Convert a local 'HH:MM' wall time to (utc_hour, utc_minute).

    Uses the host's current UTC offset (incl. DST). Good enough for daily
    windows; on TZ change the host re-pushes the schedule.
    """
    if now is None:
        now = datetime.now().astimezone()
    try:
        hour, minute = [int(x) for x in hhmm.split(":", 1)]
    except (ValueError, AttributeError):
        hour, minute = 8, 0
    local_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.hour, utc_dt.minute


def mode_to_u8(mode):
    if mode == "off":
        return 0
    if mode == "timed":
        return 2
    return 1


# UART command ids for the raw-passthrough fallback (MCU
# uart_protocol_analysis.h; wire shapes in the daemon docs, ctrl_cmd.md §5).
MCU_CMD_SET_TIME = 9
MCU_CMD_IO_SCHEDULE_SET = 175

# How long to sit on a "the daemon cannot deliver" verdict before probing the
# named commands again. Delivery probing costs several seconds of read-back
# waits on the timer thread, so it must not run every minute forever.
DELIVERY_RETRY_SECONDS = 600

# The MCU evaluates the windows against its own RTC, so that RTC is part of
# the schedule: once it drifts or resets, the window silently moves and the
# LED burns all night while every host-side view still looks right (the
# schedule read-back matches, the UI badge shows what the MCU reports, and
# nothing re-syncs because we only sync when the schedule itself changes).
# Observed on a device 2 days after boot: MCU RTC hours off, LED lit at 02:00
# inside an 09:00-22:00 window. So re-sync on a cadence, and cross-check the
# engine's own output against ours in between.
MCU_RTC_RESYNC_SECONDS = 3600
MCU_VERIFY_SECONDS = 600
# Host/MCU minute boundaries never line up exactly; near a window edge a
# disagreement is expected, not evidence of a bad clock.
WINDOW_EDGE_SLACK_MINUTES = 2


def utc_time_sync_payload_hex(now=None):
    """7-byte CPU_SET_TIME_REQ payload: year LE, month 1-12, day, h, m, s.

    UTC on purpose -- the schedule windows are pushed as UTC, and the MCU
    engine holds every "timed" channel force-on until a time sync lands.
    """
    dt = now if now is not None else datetime.now(timezone.utc)
    return bytes([dt.year & 0xFF, (dt.year >> 8) & 0xFF, dt.month, dt.day,
                  dt.hour, dt.minute, dt.second]).hex()


def io_schedule_payload_hex(led, beeper):
    """10-byte CPU_SET_IO_SCHEDULE_REQ payload: LED channel then beeper."""
    def channel(ch):
        return [mode_to_u8(ch.get("mode", "on")),
                int(ch.get("start_hour", 0)), int(ch.get("start_minute", 0)),
                int(ch.get("end_hour", 0)), int(ch.get("end_minute", 0))]
    return bytes(channel(led) + channel(beeper)).hex()


def schedule_reply_matches(reply, led, beeper):
    """Does a pmu-io-schedule-get reply carry exactly the schedule we pushed?

    This is the delivery proof: the daemon reply echoes the MCU's own stored
    schedule (the set-ack path re-reads it), so a match means the engine is
    really running our windows -- not that our command vanished into an old
    daemon that drops unknown commands without a word.
    """
    if not isinstance(reply, dict) or reply.get("valid") != 1:
        return False
    for got, want in ((reply.get("led"), led), (reply.get("beeper"), beeper)):
        if not isinstance(got, dict):
            return False
        if mode_to_u8(got.get("mode")) != mode_to_u8(want.get("mode", "on")):
            return False
        for key in ("start_hour", "start_minute", "end_hour", "end_minute"):
            try:
                if int(got.get(key, -1)) != int(want.get(key, 0)):
                    return False
            except (TypeError, ValueError):
                return False
    return True


class PcatTimerManager(threading.Thread):
    """Manages timed settings for power LED and beeper"""
    
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.running = False
        self.settings_file = '/etc/pcat_timer_settings.json'
        
        # Default settings - using 3-state system
        self.settings = {
            'power_led': {
                'mode': 'on',  # off, on, timed
                'start_time': '08:00',
                'end_time': '22:00'
            },
            'beeper': {
                'mode': 'on',  # off, on, timed
                'start_time': '08:00',
                'end_time': '22:00'
            }
        }
        
        self.socket_client = None
        # Last state actually pushed to the PMU / fan. The loop re-evaluates
        # every minute but only *sends* on change: each pmu_io_set is a socket
        # command plus an STM32 UART transaction (and a 1s read-back wait), so
        # resending an unchanged state 24/7 wastes battery.
        self._applied_led_state = None
        self._applied_beeper_state = None
        self._applied_fan_speed = None
        self._stop_event = threading.Event()
        # Serialise apply paths: start() calls apply_startup_settings() on the
        # parent thread while run() immediately applies on the timer thread.
        # Without a lock both can queue concurrent pmu-io-set/get and leave
        # the optimistic tracker claiming success while the PMU stayed off.
        self._apply_lock = threading.Lock()
        # None until first probe; True when MCU owns the schedule.
        self._mcu_schedule_owner = None
        self._last_mcu_schedule_push = None  # signature of last push
        # How the schedule actually reaches the MCU: None until proven,
        # "named" (daemon commands, verified by read-back), "raw" (daemon
        # passthrough, blind), "host-reassert" (daemon can deliver nothing;
        # the host loop re-asserts against the MCU engine every minute).
        self._mcu_delivery = None
        self._delivery_failed_until = 0.0
        # Monotonic stamps for the MCU RTC upkeep (see MCU_RTC_RESYNC_SECONDS).
        self._last_rtc_sync = 0.0
        self._last_schedule_verify = 0.0
        self.load_settings()

    def set_socket_client(self, socket_client):
        """Set the socket client for PMU control"""
        self.socket_client = socket_client
    
    def load_settings(self):
        """Load settings from file"""
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    loaded_settings = json.load(f)
                self._migrate_old_format(loaded_settings)
                # Merge over the defaults rather than replacing them: a file
                # written by older firmware (or by hand) may lack a whole
                # device section, and a wholesale swap made get_beeper_mode()
                # / _desired_state() KeyError at boot -- crashing start() and
                # erroring the per-minute loop on every tick.
                for device, device_settings in loaded_settings.items():
                    if (isinstance(device_settings, dict)
                            and isinstance(self.settings.get(device), dict)):
                        self.settings[device].update(device_settings)
                    else:
                        self.settings[device] = device_settings
                pcat_logger.logger.info(f"Loaded timer settings from {self.settings_file}")
        except Exception as e:
            pcat_logger.logger.warning(f"Failed to load timer settings: {e}")
    
    def _migrate_old_format(self, settings):
        """Migrate old enabled/all_day format to new mode format"""
        for device in ['power_led', 'beeper']:
            if device in settings:
                device_settings = settings[device]
                if 'mode' not in device_settings:
                    # Migrate from old format
                    enabled = device_settings.get('enabled', True)
                    all_day = device_settings.get('all_day', True)
                    
                    if not enabled:
                        device_settings['mode'] = 'off'
                    elif all_day:
                        device_settings['mode'] = 'on'
                    else:
                        device_settings['mode'] = 'timed'
                    
                    # Remove old keys
                    device_settings.pop('enabled', None)
                    device_settings.pop('all_day', None)
                    pcat_logger.logger.info(f"Migrated {device} settings from old format to mode: {device_settings['mode']}")
    
    def save_settings(self):
        """Save settings to file"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
            pcat_logger.logger.info(f"Saved timer settings to {self.settings_file}")
        except Exception as e:
            pcat_logger.logger.error(f"Failed to save timer settings: {e}")
    
    def set_power_led_settings(self, mode, start_time, end_time):
        """Set power LED settings with 3-state mode: off, on, timed"""
        self.settings['power_led'] = {
            'mode': mode,
            'start_time': start_time,
            'end_time': end_time
        }
        self.save_settings()
        # Invalidate the last MCU push so the next apply re-sends even if only
        # the local wall-clock window changed.
        self._last_mcu_schedule_push = None
        pcat_logger.logger.info(f"Power LED settings updated: mode={mode}, start={start_time}, end={end_time}")
    
    def set_beeper_settings(self, mode, start_time, end_time):
        """Set beeper settings with 3-state mode: off, on, timed"""
        self.settings['beeper'] = {
            'mode': mode,
            'start_time': start_time,
            'end_time': end_time
        }
        self.save_settings()
        self._last_mcu_schedule_push = None
        pcat_logger.logger.info(f"Beeper settings updated: mode={mode}, start={start_time}, end={end_time}")
    
    def get_power_led_mode(self):
        return self.settings['power_led'].get('mode', 'on')
    
    def get_power_led_start_time(self):
        return self.settings['power_led'].get('start_time', '08:00')
    
    def get_power_led_end_time(self):
        return self.settings['power_led'].get('end_time', '22:00')
    
    def get_beeper_mode(self):
        return self.settings['beeper'].get('mode', 'on')
    
    def get_beeper_start_time(self):
        return self.settings['beeper'].get('start_time', '08:00')
    
    def get_beeper_end_time(self):
        return self.settings['beeper'].get('end_time', '22:00')
    
    def is_time_in_range(self, start_time_str, end_time_str):
        """Check if current time is within the specified range"""
        try:
            current_time = datetime.now().time()
            start_time = datetime_time.fromisoformat(start_time_str)
            end_time = datetime_time.fromisoformat(end_time_str)
            
            pcat_logger.logger.debug(f"Time range check: {start_time_str}-{end_time_str}, current: {current_time.strftime('%H:%M:%S')}")
            
            if start_time <= end_time:
                # Same day range (e.g., 09:00 to 17:00)
                result = start_time <= current_time <= end_time
                pcat_logger.logger.debug(f"Same day range: {start_time} <= {current_time} <= {end_time} = {result}")
                return result
            else:
                # Overnight range (e.g., 22:00 to 08:00)
                result = current_time >= start_time or current_time <= end_time
                pcat_logger.logger.debug(f"Overnight range: {current_time} >= {start_time} OR {current_time} <= {end_time} = {result}")
                return result
        except Exception as e:
            pcat_logger.logger.error(f"Error checking time range: {e}")
            return False
    
    def _desired_state(self, device):
        """The ON/OFF state a device should be in right now, from its mode."""
        device_settings = self.settings[device]
        mode = device_settings.get('mode', 'on')
        if mode == 'off':
            return False
        if mode == 'timed':
            return self.is_time_in_range(device_settings['start_time'],
                                         device_settings['end_time'])
        return True  # 'on' (and unknown modes, matching previous behavior)

    def _needs_apply(self, desired, applied_tracker, actual_hw):
        """True when we must push `desired` to the PMU.

        Prefer the last hardware read-back when available: the change-gating
        tracker alone is optimistic (set before the async pmu-io-get returns),
        so a boot race or dropped UART frame can leave the LED/beeper stuck
        wrong forever if we only trust the tracker. When the PMU reports a
        state that disagrees with the schedule, re-send even if we think we
        already applied it.
        """
        if actual_hw is not None:
            return bool(actual_hw) != bool(desired)
        return desired != applied_tracker

    def _probe_mcu_schedule_owner(self):
        """Decide once (per process) whether the MCU owns LED/beeper schedules.

        Uses the STM32 firmware version string. If version is not yet known,
        leave owner unset so the next tick can retry; fall back to host path
        only after a successful version read that is too old.
        """
        if self._mcu_schedule_owner is not None:
            return self._mcu_schedule_owner
        if not self.socket_client:
            return False
        ver = getattr(self.socket_client, "stm32_firmware_version", None)
        if not ver:
            return False  # not ready; keep probing
        self._mcu_schedule_owner = mcu_io_schedule_supported(ver)
        pcat_logger.logger.info(
            "LED/beeper schedule owner: %s (fw=%s)",
            "mcu" if self._mcu_schedule_owner else "host",
            ver,
        )
        return self._mcu_schedule_owner

    def schedule_owner(self):
        """'mcu' or 'host' (best-effort; 'host' until version is known)."""
        return "mcu" if self._probe_mcu_schedule_owner() else "host"

    def _channel_utc_dict(self, device):
        """Build the UTC schedule dict for one channel from local settings."""
        s = self.settings[device]
        mode = s.get("mode", "on")
        sh, sm = local_hhmm_to_utc_hm(s.get("start_time", "08:00"))
        eh, em = local_hhmm_to_utc_hm(s.get("end_time", "22:00"))
        return {
            "mode": mode,
            "start_hour": sh,
            "start_minute": sm,
            "end_hour": eh,
            "end_minute": em,
        }

    def schedule_delivery(self):
        """How the schedule reaches the MCU, for the dev/status page:
        None | 'named' | 'raw' | 'host-reassert'."""
        return self._mcu_delivery

    def _set_delivery(self, mode):
        if mode == self._mcu_delivery:
            return
        self._mcu_delivery = mode
        if mode == "named":
            pcat_logger.logger.info(
                "MCU IO schedule delivery: daemon commands (verified)")
        elif mode == "raw":
            pcat_logger.logger.warning(
                "MCU IO schedule delivery: raw passthrough -- the running "
                "pcat-manager predates pmu-io-schedule-*, so delivery is "
                "blind; update pcat-manager for verified delivery")
        elif mode == "host-reassert":
            pcat_logger.logger.error(
                "MCU IO schedule delivery failed: the daemon has neither the "
                "schedule commands nor a raw passthrough. Falling back to "
                "re-asserting LED/beeper state from the host every minute -- "
                "expect up to a minute of lag around each window edge")

    def _deliver_schedule(self, led, beeper):
        """Get the schedule into the MCU engine and prove it, three rungs down.

        Named daemon commands first, verified by reading the MCU's stored
        schedule back -- the failure this guards against is precisely a
        daemon that drops unknown commands without a word (forum report
        r7897: every UI change "pushed" and nothing ever happened). Then the
        daemon's raw passthrough, which old daemons do ship: blind at the MCU
        level but the daemon's queueing reply at least proves the frame went
        out. Only when even that is missing does the host loop take over.
        """
        sc = self.socket_client

        sc.pmu_date_time_sync_utc()
        # A delivery is itself a sync + a read-back, so it restarts both
        # upkeep clocks; without this the loop would verify again seconds
        # after every push.
        self._last_rtc_sync = time.monotonic()
        self._last_schedule_verify = self._last_rtc_sync
        reply = sc.pmu_io_schedule_set(led, beeper)
        if schedule_reply_matches(reply, led, beeper):
            self._set_delivery("named")
            return True
        if reply is not None:
            # The daemon answered, so the named commands exist; the snapshot
            # is refreshed off the set-ack and can be one poll stale. One
            # more look before concluding anything.
            time.sleep(0.7)
            reply = sc.pmu_io_schedule_get(wait=True, timeout=1.5)
            if schedule_reply_matches(reply, led, beeper):
                self._set_delivery("named")
                return True
            # Known commands but no verified state: MCU still settling or
            # engine genuinely absent. Not delivered; the minute loop
            # retries, and unlike the rungs below this needs no cooldown.
            return False

        raw_ok = sc.pmu_raw_command(
            MCU_CMD_SET_TIME, utc_time_sync_payload_hex(), wait=True)
        if sc.pmu_raw_command(
                MCU_CMD_IO_SCHEDULE_SET, io_schedule_payload_hex(led, beeper),
                wait=True) and raw_ok:
            self._set_delivery("raw")
            return True

        self._set_delivery("host-reassert")
        self._delivery_failed_until = time.monotonic() + DELIVERY_RETRY_SECONDS
        return False

    def push_mcu_schedule(self, force=False):
        """Push both channels to the MCU. No-op when owner is host/unknown.

        True means the schedule verifiably reached the MCU (or blindly via
        the raw rung); False sends the caller to the host fallback path.
        """
        if not self.socket_client or not self.socket_client.sock_connected:
            return False
        if not self._probe_mcu_schedule_owner():
            return False
        # A concluded "cannot deliver" is sat on rather than re-probed every
        # minute (each probe blocks the timer thread for seconds of read-back
        # waits). A user save (force=True) retries immediately.
        if (not force and self._mcu_delivery == "host-reassert"
                and time.monotonic() < self._delivery_failed_until):
            return False

        led = self._channel_utc_dict("power_led")
        beeper = self._channel_utc_dict("beeper")
        sig = (led["mode"], led["start_hour"], led["start_minute"],
               led["end_hour"], led["end_minute"],
               beeper["mode"], beeper["start_hour"], beeper["start_minute"],
               beeper["end_hour"], beeper["end_minute"])
        if not force and sig == self._last_mcu_schedule_push:
            return True

        pcat_logger.logger.info(
            "Pushing MCU IO schedule: led=%s beeper=%s (UTC)", led, beeper)
        try:
            delivered = self._deliver_schedule(led, beeper)
        except Exception as e:
            pcat_logger.logger.warning("push_mcu_schedule failed: %s", e)
            return False
        if delivered:
            self._last_mcu_schedule_push = sig
            # Track applied as the schedule-desired state so a later host-path
            # fallback (if any) does not thrash.
            self._applied_led_state = self._desired_state("power_led")
            self._applied_beeper_state = self._desired_state("beeper")
        return delivered

    def _near_window_edge(self, device, slack=WINDOW_EDGE_SLACK_MINUTES):
        """True when local now is within `slack` minutes of a window edge."""
        settings = self.settings.get(device, {})
        if settings.get('mode') != 'timed':
            return False
        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute
        for key in ('start_time', 'end_time'):
            try:
                hour, minute = [int(x) for x in settings.get(key, '').split(':', 1)]
            except (ValueError, AttributeError):
                continue
            delta = abs(now_minutes - (hour * 60 + minute))
            if min(delta, 1440 - delta) <= slack:
                return True
        return False

    def _mcu_output_disagrees(self, reply):
        """Channels whose MCU-reported state differs from what we schedule.

        A disagreement outside the edge slack means the MCU is evaluating our
        windows against a clock that is not ours -- the failure mode that
        leaves the LED lit at 02:00 inside an 09:00-22:00 window.
        """
        bad = []
        for device, key in (('power_led', 'current_led'),
                            ('beeper', 'current_beeper')):
            current = reply.get(key)
            if current is None:
                continue
            if bool(current) == self._desired_state(device):
                continue
            if self._near_window_edge(device):
                continue
            bad.append(device)
        return bad

    def maintain_mcu_schedule(self):
        """Keep the MCU RTC honest while the MCU owns the schedule.

        Pushing the windows once is not enough: they are evaluated against the
        MCU's RTC, which drifts and does not survive an MCU reset. Nothing
        else re-syncs it (push_mcu_schedule is change-gated, so on a stable
        schedule the last sync is the one from boot), and every host-side view
        keeps looking correct while the window silently shifts. So re-sync
        hourly, and in between compare the engine's own reported output with
        ours -- a mismatch away from a window edge is the clock going wrong.
        """
        if not self.socket_client or not self.socket_client.sock_connected:
            return
        if not self._probe_mcu_schedule_owner():
            return
        if self._mcu_delivery not in ("named", "raw"):
            return  # host path re-asserts every minute anyway

        now = time.monotonic()
        if now - self._last_rtc_sync >= MCU_RTC_RESYNC_SECONDS:
            self.socket_client.pmu_date_time_sync_utc()
            self._last_rtc_sync = now
            pcat_logger.logger.debug("Re-synced MCU RTC to UTC")

        if self._mcu_delivery != "named":
            return  # raw passthrough is blind; nothing to read back
        if now - self._last_schedule_verify < MCU_VERIFY_SECONDS:
            return
        self._last_schedule_verify = now

        led = self._channel_utc_dict("power_led")
        beeper = self._channel_utc_dict("beeper")
        try:
            reply = self.socket_client.pmu_io_schedule_get(wait=True, timeout=1.5)
        except Exception as e:
            pcat_logger.logger.warning("MCU schedule verify failed: %s", e)
            return
        if reply is None:
            return

        if not schedule_reply_matches(reply, led, beeper):
            pcat_logger.logger.warning(
                "MCU no longer holds our schedule (reset?): %s -- re-pushing",
                reply)
        else:
            disagree = self._mcu_output_disagrees(reply)
            if not disagree:
                return
            pcat_logger.logger.warning(
                "MCU schedule output disagrees with ours for %s (mcu led=%s "
                "beeper=%s; host led=%s beeper=%s) -- MCU RTC is wrong, "
                "re-syncing and re-pushing",
                ",".join(disagree), reply.get("current_led"),
                reply.get("current_beeper"),
                self._desired_state("power_led"),
                self._desired_state("beeper"))

        with self._apply_lock:
            self.push_mcu_schedule(force=True)

    def apply_power_led_setting(self):
        """Apply power LED setting based on current configuration"""
        if not self.socket_client:
            pcat_logger.logger.warning("apply_power_led_setting: socket_client not available")
            return

        with self._apply_lock:
            # New MCU: push full schedule (both channels) and let the MCU own
            # the minute-by-minute evaluation. Single push covers beeper too.
            if self._probe_mcu_schedule_owner():
                if self.push_mcu_schedule(force=False):
                    return
                if self._mcu_delivery != "host-reassert":
                    return  # transient; retried on the next tick
                # Engine-bearing MCU above a daemon that can deliver nothing:
                # fall through to the host path. See _needs_apply bypass.

            desired = self._desired_state('power_led')
            actual = getattr(self.socket_client, 'status_led_v2_enabled', None)
            # In host-reassert the MCU engine re-applies its own (default,
            # everything-on) schedule at every minute boundary, silently
            # undoing what we set -- and the io-get cache cannot see that
            # (it only updates off our own commands). Change-gating is
            # therefore a lie here; re-send every tick.
            force_send = self._mcu_delivery == "host-reassert"
            if not force_send and not self._needs_apply(
                    desired, self._applied_led_state, actual):
                # Keep tracker in sync when hardware already matches.
                self._applied_led_state = desired
                return
            if not self.socket_client.sock_connected:
                return  # can't send now; keep tracker unset so the next tick retries

            mode = self.settings['power_led'].get('mode', 'on')
            if not force_send or self._applied_led_state != desired:
                pcat_logger.logger.info(f"Setting power LED {'ON' if desired else 'OFF'} (mode: {mode})")
            self.socket_client.pmu_io_set(status_led_v2_enabled=desired)
            self._applied_led_state = desired

    def apply_beeper_setting(self):
        """Apply beeper setting based on current configuration"""
        if not self.socket_client:
            return

        with self._apply_lock:
            if self._probe_mcu_schedule_owner():
                # LED apply already pushes both channels; still allow a force
                # path when only beeper settings changed.
                if self.push_mcu_schedule(force=False):
                    return
                if self._mcu_delivery != "host-reassert":
                    return  # transient; retried on the next tick

            desired = self._desired_state('beeper')
            actual = getattr(self.socket_client, 'beeper_enabled', None)
            # Same engine-stomp bypass as the LED path above.
            force_send = self._mcu_delivery == "host-reassert"
            if not force_send and not self._needs_apply(
                    desired, self._applied_beeper_state, actual):
                self._applied_beeper_state = desired
                return
            if not self.socket_client.sock_connected:
                return  # can't send now; keep tracker unset so the next tick retries

            mode = self.settings['beeper'].get('mode', 'on')
            if not force_send or self._applied_beeper_state != desired:
                pcat_logger.logger.info(f"Setting beeper {'ON' if desired else 'OFF'} (mode: {mode})")
            self.socket_client.pmu_io_set(beeper_enabled=desired)
            self._applied_beeper_state = desired

    def apply_smart_fan_control(self):
        """Apply smart fan control based on temperature and charging status"""
        try:
            # Get current fan mode
            fan_mode = pcat_config.get_fan_mode()

            # If manual mode, apply saved manual fan speed
            if fan_mode == "manual":
                user_speed = pcat_config.get_fan_speed()
                pcat_config.set_fan_speed_direct(user_speed)
                pcat_logger.logger.debug(f"[Smart Fan] Manual mode - applied saved speed {user_speed}")
                return

            # Get current user max fan speed setting
            user_max_speed = pcat_config.get_fan_speed()

            # Get current temperature
            temperature = self.socket_client.board_temperature if self.socket_client and self.socket_client.board_temperature is not None else None

            # Apply smart algorithm with current mode (smart_quiet or smart_cool)
            # Note: set_fan_speed_smart now calls is_device_charging() internally
            actual_speed = pcat_config.set_fan_speed_smart(temperature, user_max_speed, fan_mode)

            # Log only when the computed speed changes: an unconditional log
            # here wrote ~1440 lines/day of steady-state noise.
            if actual_speed != self._applied_fan_speed:
                is_charging = pcat_config.is_device_charging()
                temp_display = f"{temperature}°C" if temperature is not None else "N/A"
                charging_display = is_charging if is_charging is not None else "N/A"
                user_max_display = user_max_speed if user_max_speed is not None else "N/A"
                actual_display = actual_speed if actual_speed is not None else "N/A"
                pcat_logger.logger.info(f"[Smart Fan] mode={fan_mode}, temp={temp_display}, charging={charging_display}, user_max={user_max_display}, actual_speed={actual_display}")
                self._applied_fan_speed = actual_speed

        except Exception as e:
            # Log errors but don't let them crash the timer loop
            pcat_logger.logger.warning(f"[Smart Fan] Error in smart fan control: {e}")
    
    def apply_startup_settings(self):
        """Apply settings immediately on startup"""
        if self.socket_client:
            pcat_logger.logger.info("Applying startup settings for power LED, beeper, and smart fan control")
            self.apply_power_led_setting()
            self.apply_beeper_setting()
            self.apply_smart_fan_control()
            # Re-apply saved power mode to pcat-manager daemon
            saved_power_mode = pcat_config.get_power_mode()
            if saved_power_mode == "direct_power":
                self.socket_client.set_power_on_mode(2)
                pcat_logger.logger.info("Restored direct power mode (power-on-mode=2) from saved config")
        else:
            pcat_logger.logger.warning("Cannot apply startup settings: socket client not available")
    
    def force_apply_settings(self):
        """Force immediate application of all settings (for debugging)"""
        pcat_logger.logger.info("Force applying timer settings")
        # Drop the change-gating trackers so everything is re-sent even if we
        # believe the hardware is already in the right state. Also invalidate
        # the cached pmu-io-get snapshot: after an MCU reset the cache still
        # holds the pre-reset value, and hardware-aware gating would then skip
        # the re-send that recovery needs.
        self._applied_led_state = None
        self._applied_beeper_state = None
        self._applied_fan_speed = None
        self._last_mcu_schedule_push = None
        if self.socket_client is not None:
            if hasattr(self.socket_client, 'status_led_v2_enabled'):
                self.socket_client.status_led_v2_enabled = None
            if hasattr(self.socket_client, 'beeper_enabled'):
                self.socket_client.beeper_enabled = None
        if self._probe_mcu_schedule_owner():
            with self._apply_lock:
                delivered = self.push_mcu_schedule(force=True)
            if not delivered and self._mcu_delivery == "host-reassert":
                # Nothing can reach the engine; drive the GPIO directly so a
                # user save still has a visible effect right now.
                self.apply_power_led_setting()
                self.apply_beeper_setting()
        else:
            self.apply_power_led_setting()
            self.apply_beeper_setting()
        self.apply_smart_fan_control()
    
    def start(self):
        """Start the timer manager thread"""
        self.running = True
        super().start()
        # Apply settings immediately on startup
        self.apply_startup_settings()
        pcat_logger.logger.info("PcatTimerManager started")
    
    def stop(self):
        """Stop the timer manager thread"""
        self.running = False
        self._stop_event.set()
        pcat_logger.logger.info("PcatTimerManager stopped")
    
    def run(self):
        """Main timer loop"""
        pcat_logger.logger.info("PcatTimerManager thread running")

        # Run timer early on startup - check after 10 seconds
        early_startup_done = False

        while self.running:
            try:
                # Apply settings every minute (or 10s on first run)
                self.apply_power_led_setting()
                self.apply_beeper_setting()
                self.maintain_mcu_schedule()
                self.apply_smart_fan_control()

                # Early startup: sleep for 10 seconds on first run, then 60 seconds
                sleep_duration = 10 if not early_startup_done else 60
                early_startup_done = True

                # Single interruptible wait instead of 1s polling (one thread
                # wakeup per minute instead of sixty); stop() sets the event.
                self._stop_event.wait(sleep_duration)

            except Exception as e:
                pcat_logger.logger.error(f"Error in timer manager loop: {e}")
                self._stop_event.wait(10)  # Wait a bit before retrying