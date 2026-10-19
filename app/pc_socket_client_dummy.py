# -*- coding: utf-8 -*-
# Dummy socket client for testing on x86/x64 machines without hardware

import threading
import time
import queue
import json
import random
from pcat_util import isp_dict, pcat_logger

class PcSocketClientDummy(threading.Thread):
    """
    Dummy socket client that simulates responses without actual Unix socket connection.
    Used for debugging on x86/x64 machines without Phonicat hardware.
    """
    
    def stm32_firmware_exist(self):
        if self.stm32_firmware_version == None or self.stm32_firmware_version == "":
            return False
        version_str = self.stm32_firmware_version[-3:-1]
        if version_str == '55':  # Home version
            return False
        elif self.stm32_firmware_version[-4:] == 'HOME':
            return False
        else:
            return True

    def __init__(self):
        threading.Thread.__init__(self)
        self.running_flag = False
        self.server_address = '/tmp/pcat-manager-dummy.sock'  # Dummy address
        self.output_fds = []
        self.input_fds = []
        self.send_cmd_queue = queue.Queue()
        self.recv_buffer = b''
        self.sock = None
        self.sock_connected = True  # Always "connected" in dummy mode
        self.enable_log = True
        
        # Initialize with dummy data
        self.on_charging = True
        self.charge_percent = 85
        self.prev_charge_percent = 85
        self.charge_percent_buf_queue = [8500, 8500, 8500]  # Simulated buffer
        self.battery_voltage = 3700  # millivolts
        self.charger_voltage = 5035  # millivolts
        self.battery_current = 500  # milliamps
        self.battery_wattage = 1.85  # watts
        self.board_temperature = 42  # Celsius
        self.event_list = []
        self.event_list_known = True
        
        self.charger_on_auto_start_mode = {"enabled": 'false', "timeout": 0, "countdown": 0}
        self.modem_signal_strength = 75
        self.modem_mode = "4G"
        self.sim_state = "ready"
        self.isp_name = "Test Carrier"
        self.isp_plmn = "46001"
        self.network_route_mode = "mobile"
        self.stm32_firmware_version = "PCAT_V2.0.0"
        self.pmu_hw_version = "PCAT2-REV2"
        self.device_type = "lte"
        self.modem_apn = {"apn": "internet", "user": "", "password": "", "auth": "none"}
        
        # PMU I/O settings
        self.status_led_v2_enabled = 1
        self.beeper_enabled = 1

        # Battery health / info (simulated). Mirrors PcSocketClient so the
        # /battery page renders real-looking cards in dummy mode instead of
        # the "unsupported" branch.
        self.battery_info_supported = True
        self.battery_full_capacity = 5000      # mAh
        self.battery_nominal_capacity = 5200   # mAh
        self.battery_cycle_count = 37
        self.battery_capacity_learned = True
        self.battery_learning_active = False
        self.battery_internal_resistance = 85  # mOhm (whole loop)
        # Resistance split (fw >= RA2E1260721001, proto v8): fixed board
        # parasitic and cell-only (total - parasitic). Missing these crashed
        # /api/v1/battery_info.json in dummy mode with an AttributeError.
        self.battery_internal_resistance_parasitic = 35  # mOhm, board constant
        self.battery_internal_resistance_battery = 50    # mOhm, cells only
        self.battery_runtime_seconds = 432000  # 120 h
        self.battery_mid_voltage = 3712        # 2P2S center-tap, mV
        # SOC display-factor diagnostics (fw >= RA2E1260719001)
        self.battery_soc_raw_x10 = 723         # raw SoC 72.3%
        self.battery_soc_empty_pct = 18        # learned empty point
        self.battery_soc_disp_a_x1000 = 1312   # display a
        self.battery_soc_disp_b_x100 = -2694   # display b
        self.battery_chg_gain_x1000 = 1042     # charge gain autolearn
        self.battery_cur_trim_a_x1000 = 1000   # current trim a (identity)
        self.battery_cur_trim_b_ma = 0         # current trim b
        # Remaining capacity / energy counters (fw >= RA2E1260719002)
        self.battery_remain_mah = 3615         # mAh
        self.battery_consumed_mwh = 10480      # mWh since anchor
        self.battery_charged_mwh = 21315       # mWh since anchor
        # Protocol version + MCU health + time estimates (fw >= RA2E1260719003)
        self.battery_proto_ver = 7
        self.battery_health_pct_mcu = 96       # %
        self.battery_minutes_to_empty = 213    # steady discharge in dummy mode
        self.battery_full_charge_seen = None   # proto 7: not reported
        self.battery_minutes_to_full = None    # not charging
        # Temperature-compensation diagnostics (fw >= RA2E1260719009, proto v7)
        self.battery_gauge_temp_c = 31           # C
        self.battery_temp_empty_extra_x10 = 0    # warm: no cold uplift
        self.battery_r_temp_factor_x1000 = 964   # ~31C -> slightly below 1.0
        # MCU uptime diagnostics (fw >= RA2E1260721006, proto v9)
        self.mcu_uptime_seconds = 1123200        # 13 days since MCU reset
        self.mcu_since_sleep_seconds = 273600    # 3.17 days since last standby wake
        self.mcu_sleep_count = 42                # standby entries since reset
        # Battery charge limit % (80-100); the real value lives in the MCU.
        self.charge_threshold = 90

        # SOC gauge diagnostic ring (fw >= RA2E1260727000, com=173). Replays a
        # real field case so /dev renders something meaningful without hardware:
        # discharged to a displayed 10%, python-side auto-shutdown, then on the
        # next power-on the 5% OCV hard calibration fired against a pack that
        # had not finished relaxing and slammed raw SoC 17.1% -> 6.2%, which the
        # display curve turns into 0%.
        self.soc_log_response = None
        self.soc_log_seq = 0
        self._dummy_soc_log = self._build_dummy_soc_log()

        # Start dummy data update timer
        self._start_dummy_update_timer()

    # Event codes mirror SOC_LOG_EVT_* in the MCU's src/soc_log.h.
    _EVT_PERIODIC, _EVT_BOOT, _EVT_RK_ON, _EVT_RK_OFF = 0, 1, 2, 3
    _EVT_OCV_CALIB, _EVT_NV_RESTORE, _EVT_SLEEP_DISC = 9, 11, 12
    # flags: bit0 charger, bit1 baseline trusted, bit2 RK on
    _F_TRUSTED, _F_RK_ON = 0x02, 0x04

    def _build_dummy_soc_log(self):
        """Synthesise the record set for the 10%-shutdown -> 0%-on-boot case."""
        recs = []
        seq = [120]

        def add(t, v, i, raw_x10, disp, event, pre=False, flags=None):
            recs.append({
                "seq": seq[0], "t": t, "v": v, "i": i, "vbus": 0,
                "soc-raw-x10": raw_x10, "soc-disp": disp, "temp": 33,
                "event": event, "pre": pre,
                "flags": self._F_TRUSTED | self._F_RK_ON if flags is None else flags,
            })
            seq[0] += 1

        # Tail of the discharge, RK up, 60s heartbeats.
        t = 1123200
        for raw, disp, v in ((201, 12, 7305), (194, 12, 7281), (187, 11, 7258),
                             (180, 11, 7236), (174, 10, 7214)):
            add(t, v, 1180, raw, disp, self._EVT_PERIODIC)
            t += 60
        # Userspace shutdown at a displayed 10%.
        add(t, 7208, 1163, 171, 10, self._EVT_RK_OFF)
        t += 42
        # RK down: standby, 300s heartbeats, pack relaxing back up.
        for raw, v, dt in ((171, 6952, 300), (171, 7014, 300), (171, 7061, 300)):
            add(t, v, 21, raw, 10, self._EVT_PERIODIC, flags=self._F_TRUSTED)
            t += dt
        add(t, 7068, 20, 171, 10, self._EVT_SLEEP_DISC, pre=True, flags=self._F_TRUSTED)
        add(t, 7068, 20, 170, 10, self._EVT_SLEEP_DISC, flags=self._F_TRUSTED)
        t += 30
        # The 5% hard calibration fires on a still-relaxing pack: the REST table
        # reads 6952-7068mV as ~6%, 10 points below the coulomb estimate.
        add(t, 7069, 19, 170, 10, self._EVT_OCV_CALIB, pre=True, flags=self._F_TRUSTED)
        add(t, 7069, 19, 62, 10, self._EVT_OCV_CALIB, flags=self._F_TRUSTED)
        t += 300
        # Display rate limiter (1%/10s) walks down to 0 over the next ~100s.
        for raw, disp, v in ((62, 6, 7092), (62, 2, 7098), (62, 0, 7101)):
            add(t, v, 20, raw, disp, self._EVT_PERIODIC, flags=self._F_TRUSTED)
            t += 60
        # User powers on again; reads 0%.
        add(t, 7094, 24, 62, 0, self._EVT_RK_ON)
        t += 60
        add(t, 6981, 1204, 60, 0, self._EVT_PERIODIC)
        return recs

    def soc_log_get(self, from_seq=None, max_records=256):
        recs = self._dummy_soc_log
        oldest = recs[0]["seq"] if recs else 0
        nxt = recs[-1]["seq"] + 1 if recs else 0
        if from_seq is None:
            sel = recs[-max_records:]
        else:
            sel = [r for r in recs if r["seq"] >= from_seq][:max_records]
        self.soc_log_response = {
            "command": "soc-log-get", "code": 0, "supported": True,
            "seq-oldest": oldest, "seq-next": nxt, "log-version": 1,
            "records": sel,
        }
        self.soc_log_seq += 1

    def _start_dummy_update_timer(self):
        """Periodically update dummy values to simulate real hardware"""
        def update_values():
            while self.running_flag:
                # Simulate battery discharge/charge
                if self.on_charging:
                    if self.charge_percent < 100:
                        self.charge_percent = min(100, self.charge_percent + random.randint(0, 2))
                else:
                    if self.charge_percent > 10:
                        self.charge_percent = max(10, self.charge_percent - random.randint(0, 1))
                
                # Simulate temperature variation
                self.board_temperature = 40 + random.randint(-5, 10)
                
                # Simulate signal strength variation
                self.modem_signal_strength = max(20, min(100, self.modem_signal_strength + random.randint(-5, 5)))
                
                # Simulate battery current variation
                self.battery_current = 400 + random.randint(-100, 200)
                self.battery_wattage = (self.battery_current * self.battery_voltage) / 1000 / 1000
                
                time.sleep(5)
        
        self._update_thread = threading.Thread(target=update_values, daemon=True)
        self._update_thread.start()

    def get_product_model(self):
        name = "Photonicat"  # Default for dummy mode
        if not self.stm32_firmware_exist():
            return name + " 家庭版"
        else:
            if self.device_type == "none":
                return name + " 移动版"
            elif self.device_type == "lte":
                return name + " 4G"
            elif self.device_type == "5g":
                return name + " 5G"

    def get_pmu_status(self):
        return {
            "on_charging": self.on_charging,
            "charge_percent": self.charge_percent,
            "battery_voltage": self.battery_voltage,
            "charger_voltage": self.charger_voltage,
            "stm32_firmware_version": self.stm32_firmware_version,
            "route_mode": self.network_route_mode
        }

    def schedule_power_events(self):
        """Return dummy schedule events"""
        return {"events": []}

    def query_pmu_status(self):
        """Dummy implementation - just log the query"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Query PMU status")

    def query_modem_status(self):
        """Dummy implementation - just log the query"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Query modem status")

    def query_network_route_mode_status(self):
        """Dummy implementation - just log the query"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Query network route mode status")

    def query_schedule_power_events(self):
        """Dummy implementation - just log the query"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Query schedule power events")

    def query_charger_on_auto_start(self):
        """Dummy implementation - just log the query"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Query charger on auto start")

    def set_charger_on_auto_start(self, enable, delay_timeout):
        """Dummy implementation - update internal state"""
        if self.enable_log:
            pcat_logger.logger.debug(f"[DUMMY] Set charger on auto start: enable={enable}, timeout={delay_timeout}")
        self.charger_on_auto_start_mode["enabled"] = 'true' if enable else 'false'
        self.charger_on_auto_start_mode["timeout"] = delay_timeout

    def set_rfkill_state(self, enable):
        """Dummy implementation - just log the command"""
        if self.enable_log:
            pcat_logger.logger.debug(f"[DUMMY] Set rfkill state: {enable}")

    def query_stm32_version(self):
        """Dummy implementation - version already set"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Query STM32 version")

    def query_pmu_hw_version(self):
        """Dummy implementation - revision already set"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Query PMU hardware revision")

    def modem_apn_setup(self, apn, user, password, auth):
        """Dummy implementation - update internal state"""
        if self.enable_log:
            pcat_logger.logger.debug(f"[DUMMY] Modem APN setup: apn={apn}, user={user}, auth={auth}")
        self.modem_apn = {"apn": apn, "user": user, "password": password, "auth": auth}

    def modem_apn_get(self):
        """Dummy implementation - return stored APN"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Get modem APN")

    def pmu_io_set(self, status_led_v2_enabled=None, beeper_enabled=None):
        """Dummy implementation - update internal state"""
        if status_led_v2_enabled is not None:
            self.status_led_v2_enabled = 1 if status_led_v2_enabled else 0
        if beeper_enabled is not None:
            self.beeper_enabled = 1 if beeper_enabled else 0
        if self.enable_log:
            pcat_logger.logger.debug(f"[DUMMY] PMU IO set: led={status_led_v2_enabled}, beeper={beeper_enabled}")

    def pmu_io_get(self, wait=False, timeout=1.5, max_age=None):
        """Dummy implementation - just log the query"""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Get PMU IO status")

    def pmu_date_time_sync_utc(self):
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] UTC time sync")

    def pmu_io_schedule_set(self, led, beeper):
        self.io_schedule = {
            "status": "ok", "code": 0, "supported": 1, "valid": 1,
            "led": led or {}, "beeper": beeper or {},
            "current_led": 1 if (led or {}).get("mode") != "off" else 0,
            "current_beeper": 1 if (beeper or {}).get("mode") != "off" else 0,
            "flags": 3, "time_valid": 1,
        }
        self.status_led_v2_enabled = self.io_schedule["current_led"]
        self.beeper_enabled = self.io_schedule["current_beeper"]
        self._pmu_io_schedule_seq = getattr(self, "_pmu_io_schedule_seq", 0) + 1
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] IO schedule set: %s %s", led, beeper)

    def pmu_io_schedule_get(self, wait=False, timeout=1.5):
        return getattr(self, "io_schedule", None)

    def pmu_raw_command(self, mcu_command, payload_hex="", need_ack=True,
                        wait=False, timeout=1.5):
        if self.enable_log:
            pcat_logger.logger.debug(
                "[DUMMY] raw MCU cmd %s payload=%s", mcu_command, payload_hex)
        return True

    def charge_threshold_get(self):
        """Dummy: value is already local; a real client re-queries the MCU."""
        if self.enable_log:
            pcat_logger.logger.debug("[DUMMY] Get charge threshold")

    def charge_threshold_set(self, value):
        """Dummy: store the charge limit like the MCU would"""
        try:
            self.charge_threshold = max(80, min(100, int(value)))
        except (TypeError, ValueError):
            pass
        if self.enable_log:
            pcat_logger.logger.debug(f"[DUMMY] Set charge threshold: {self.charge_threshold}")

    def beep_play(self, tones, loop=False):
        """Dummy implementation - just log the tone sequence"""
        if self.enable_log:
            pcat_logger.logger.debug(f"[DUMMY] Beep play: {len(tones)} steps, loop={loop}")

    def set_schedule_power_events(self, event_job_list):
        """Dummy implementation - update internal state"""
        if self.enable_log:
            pcat_logger.logger.debug(f"[DUMMY] Set schedule power events: {event_job_list}")
        self.event_list = event_job_list

    def connect_to_server(self):
        """Dummy implementation - no actual connection needed"""
        pcat_logger.logger.info("[DUMMY] Socket client simulating connection")
        # Just sleep to keep thread alive
        while self.running_flag:
            time.sleep(1)

    def run(self):
        pcat_logger.logger.info("[DUMMY] PcSocketClient thread running in dummy mode")
        self.running_flag = True
        self.connect_to_server()