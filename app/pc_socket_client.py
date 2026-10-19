#@Author    : qichunren

import threading
# import pyprctl # patch:set real thread name
import socket
import select
import time
import queue
import json
import random

import pcat_config
from pcat_util import isp_dict, pcat_logger

# 电池放电电流死区（毫安）。电流小于 -DISCHARGE_DEADBAND_MA 时判定为对外放电（如 TYPE-C 充电宝模式），
# 避免接近满电/空载时电流轻微为负造成充放电状态抖动。
DISCHARGE_DEADBAND_MA = 50

class PcSocketClient (threading.Thread):

  # 当固件设备不存在时，固件版本号为空字符
  def stm32_firmware_exist(self):
    if self.stm32_firmware_version == None or self.stm32_firmware_version == "":
      return False
    version_str = self.stm32_firmware_version[-3:-1]
    if version_str == '55': # Home version
      return False
    elif self.stm32_firmware_version[-4:] == 'HOME':
      return False
    else:
      return True

  def __init__(self):
    threading.Thread.__init__(self)
    self.running_flag = False
    self.server_address = '/tmp/pcat-manager.sock'
    self.output_fds = []
    self.input_fds = []
    self.send_cmd_queue = queue.Queue()
    self.recv_buffer = b''
    self.sock = None
    self.sock_connected = False
    self.enable_log = False

    self.on_charging = False
    self.charger_connected = False # 充电器/DC电源是否插入（不管电流方向）
    self.pmu_status_received = False # 是否已收到首个 pmu-status（charge_percent 默认 100，收到前不可信）
    self.charge_percent = 100 # socket 返回的数据是 0-10000
    self.prev_charge_percent = None # 上一次接收到的数值
    self.charge_percent_buf_queue = []
    self.battery_voltage = None # 单位毫伏
    self.charger_voltage = 5035 # 单位毫伏
    self.battery_current = None # 单位毫安
    self.battery_wattage = None # 单位瓦特
    self.board_temperature = None
    self.event_list = [] # Schedule event list
    # False until the daemon has answered schedule-power-event-get once, so an
    # empty event_list can be told apart from "not read yet" (see app.py's
    # unknown-value poll).
    self.event_list_known = False

    # 根据充电器连接状态自动开关机（车载模式）
    # "known" distinguishes "the daemon has never answered" from "the daemon
    # says the timeout is 0". Without it the UI could not tell the two apart
    # and turned the placeholder into a made-up 10 minutes, so a device
    # configured for 5 displayed 10 while shutting down at 5.
    self.charger_on_auto_start_mode = {"enabled": 'false', "timeout": 0,
                                       "countdown": 0, "known": False}
    self.power_on_mode = 0  # 0: Manual, 1: Auto power-on, 2: Direct power mode
    self.modem_signal_strength = 0
    self.modem_mode = "" # Modem状态，可能的值：none / 2g / 3g / lte / 5g
    self.sim_state = "absent" # SIM卡状态，可能的值：absent / not-ready / ready / need-pin / need-puk / personalized-network / bad
    self.isp_name = "" # 网络服务提供商名称
    self.isp_plmn = "" # 网络服务提供商编号
    self.network_route_mode = "none" # 路由模式，可能的值：none / wired / mobile / unknown
    # Last modem-log-get reply (the daemon's in-memory modem dial log) and a
    # sequence counter so API handlers can wait for a fresh response.
    self.modem_log_response = None
    self.modem_log_seq = 0
    self.stm32_firmware_version = None # STM32固件版本号，默认不存在固件设备的话，为空字符串
    self.pmu_hw_version = None # 主板硬件版本：PCAT2-REV1(A1~A3)/PCAT2-REV2(A4)；旧MCU固件恒回 NT2421A4
    self.device_type = "none" # From pcat-manager
    self.modem_apn = None # type: Dict
    
    # PMU I/O settings
    self.status_led_v2_enabled = None # Status LED v2 enabled state
    self.beeper_enabled = None # Beeper enabled state
    # Bumped on every successful pmu-io-get response so callers can wait for
    # a fresh read-back instead of trusting a stale cache forever.
    self._pmu_io_seq = 0
    self._pmu_io_last_get_ts = 0.0
    self.charge_threshold = None # Battery charge limit threshold (80-100)
    # MCU-owned LED/beeper schedule (fw >= RA2E1260808000)
    self.io_schedule = None  # last pmu-io-schedule-get body
    self._pmu_io_schedule_seq = 0

    # Battery health, reported by the MCU (fw >= RA2E1260715004).
    # battery_info_supported stays False on older firmware, which does not
    # implement the query -- the page then says so instead of showing zeros.
    self.battery_info_supported = False
    self.battery_full_capacity = None    # learned capacity, mAh
    self.battery_nominal_capacity = None # rated capacity, mAh
    self.battery_cycle_count = None      # accumulated charge cycles
    self.battery_capacity_learned = False # False -> full_capacity is nominal, not measured
    self.battery_learning_active = False  # a measurement is in progress
    # True/False once the MCU reports whether the pack has ever reached a real
    # 100% full-charge anchor (battery-info proto >= 10, MCU fw >= RA2E1261016000);
    # None = unknown (older firmware). False means health is withheld: with a
    # charge limit below 100% the capacity measurement never runs.
    self.battery_full_charge_seen = None
    # Extended fields (MCU fw >= RA2E1260717006); stay None on older firmware.
    # battery_internal_resistance is the whole-loop measured DC resistance:
    # cells + wiring/connector + protection-board MOSFET.
    self.battery_internal_resistance = None # measured whole-loop DC resistance, mOhm
    self.battery_runtime_seconds = None     # cumulative machine-on time, seconds
    # Loop parasitic resistance (MCU fw >= RA2E1260721001, proto ver >= 8);
    # None on older firmware. The fixed non-cell part of the measured
    # resistance (wiring/connector ~20mOhm + MOSFET ~15mOhm = 35mOhm), a board
    # constant. Cell-only resistance = internal_resistance - parasitic (>=0).
    self.battery_internal_resistance_parasitic = None  # board parasitic, mOhm
    self.battery_internal_resistance_battery = None    # cell-only (total - parasitic), mOhm
    # Middle-point tap voltage (MCU fw >= RA2E1260718001); None on older firmware.
    self.battery_mid_voltage = None         # 2P2S pack center-tap voltage, mV
    # SOC display-factor diagnostics (MCU fw >= RA2E1260719001); None on older
    # firmware. The MCU auto-learns the per-unit "display empty point" (the raw
    # SoC at which the machine actually died from an empty battery, e.g. the
    # 20%-then-sudden-poweroff case) and linearly remaps the displayed SoC so
    # that point reads 0%, plus a Tesla-style curve (real 4%->1%, 96%->100%).
    # All transforms are ax+b.
    self.battery_soc_raw_x10 = None         # raw (pre-display) SoC estimate x10
    self.battery_soc_empty_pct = None       # learned empty point, raw %, 0 = not learned
    self.battery_soc_disp_a_x1000 = None    # display transform a x1000
    self.battery_soc_disp_b_x100 = None     # display transform b x100 (signed)
    self.battery_chg_gain_x1000 = None      # charge-direction gain autolearn x1000
    self.battery_cur_trim_a_x1000 = None    # per-unit current trim a x1000 (multimeter cal)
    self.battery_cur_trim_b_ma = None       # per-unit current trim b, mA (signed)
    # Remaining capacity / energy counters (MCU fw >= RA2E1260719002); None on
    # older firmware. Raw coulomb-counter values (not display-factor adjusted);
    # the energy accumulators reset at RK boot re-anchor and full-charge anchor.
    self.battery_remain_mah = None          # estimated remaining capacity, mAh
    self.battery_consumed_mwh = None        # consumed energy since anchor, mWh
    self.battery_charged_mwh = None         # charged-in energy since anchor, mWh
    # Explicit protocol version + MCU-computed health + time estimates
    # (MCU fw >= RA2E1260719003); None on older firmware. The daemon already
    # strips the sentinels (health 0xFF, minutes 0xFFFF) into absent keys.
    self.battery_proto_ver = None           # battery-info protocol version
    self.battery_health_pct_mcu = None      # MCU health % (learned/nominal, cap 100)
    self.battery_minutes_to_empty = None    # est. minutes to displayed 0%
    self.battery_minutes_to_full = None     # est. minutes to full
    # Temperature-compensation diagnostics (MCU fw >= RA2E1260719009, proto
    # ver >= 7); None on older firmware. The gauge feeds the on-board NTC into
    # the coulomb counter: cold weather raises internal resistance and shrinks
    # usable capacity, so displayed SoC walks down earlier (iPhone-style).
    self.battery_gauge_temp_c = None        # temperature the gauge is using, C
    self.battery_temp_empty_extra_x10 = None  # cold display-empty uplift x10 %
    self.battery_r_temp_factor_x1000 = None   # R_int temp factor k(T) x1000 (1000=25C)
    # MCU uptime diagnostics (MCU fw >= RA2E1260721006, proto ver >= 9); None
    # on older firmware. mcu_uptime_seconds is the wall-clock time since the
    # MCU last reset (awake + standby; a watchdog/fault reset zeroes it, so it
    # reads as "how long without a reset"); mcu_since_sleep_seconds is the
    # time since the MCU last woke from standby (= uptime if it never slept);
    # mcu_sleep_count is the standby-entry count (the ~50s AGT poll wake while
    # the RK is off re-enters standby each round, so large values are normal).
    self.mcu_uptime_seconds = None          # seconds since MCU reset
    self.mcu_since_sleep_seconds = None     # seconds since last standby wake
    self.mcu_sleep_count = None             # standby entries since MCU reset
    # SOC gauge diagnostic ring (MCU fw >= RA2E1260727000, com=173). The MCU
    # keeps 32 fixed-size records in RAM covering the window the daemon can't
    # see -- RK powered down, MCU awake/standby -- writing a before/after pair
    # at every anchor that moves SoC. The daemon drains that ring and stitches
    # it into its own long history in RAM, same shape as the modem dial log.
    # Reply lands in self.soc_log_response and bumps self.soc_log_seq.
    self.soc_log_response = None
    self.soc_log_seq = 0

  def get_product_model(self):
    name = pcat_config.get_oem_name()
    if not self.stm32_firmware_exist():
      return name + " Home"
    else:
      if self.device_type == "none":
        return name + " Cellular"
      elif self.device_type == "lte":
        return name + " 4G"
      elif self.device_type == "5g":
        return name + " 5G"

  def get_pmu_status(self):
    return {"on_charging":self.on_charging, "charge_percent":self.charge_percent, "battery_voltage":self.battery_voltage, 
      "charger_voltage":self.charger_voltage, "stm32_firmware_version": self.stm32_firmware_version, "route_mode": self.network_route_mode}

  def schedule_power_events(self):
    events = {}
    job_list = []
    # "event-list": [ { "enabled": 1, "enable-bits": 56, "action": 0, "year": 0, "month": 0, "day": 0, "hour": 0, "minute": 0, "dow-bits": 1 },
    #  { "enabled": 1, "enable-bits": 56, "action": 1, "year": 0, "month": 0, "day": 0, "hour": 5, "minute": 0, "dow-bits": 127 } ]
    for event_item in self.event_list:
      enabled_flag = event_item.get("enabled")
      enable_bits  = event_item.get("enable-bits", 0)
      action       = event_item.get("action")
      year         = event_item.get("year")
      month        = event_item.get("month")
      day          = event_item.get("day")
      hour         = event_item.get("hour")
      minute       = event_item.get("minute")
      dow_bits     = event_item.get("dow-bits", 0)
      job_item = {}
      days = []
      if (dow_bits & (1<<0)) > 0:
        days.append(0)
      if (dow_bits & (1<<1)) > 0:
        days.append(1)
      if (dow_bits & (1<<2)) > 0:
        days.append(2)
      if (dow_bits & (1<<3)) > 0:
        days.append(3)
      if (dow_bits & (1<<4)) > 0:
        days.append(4)
      if (dow_bits & (1<<5)) > 0:
        days.append(5)
      if (dow_bits & (1<<6)) > 0:
        days.append(6)

      if (enable_bits & (1<<0)) > 0:
        job_item["year"] = year
      if (enable_bits & (1<<1)) > 0:
        job_item["month"] = month
      if (enable_bits & (1<<2)) > 0:
        job_item["day"] = day
      if (enable_bits & (1<<3)) > 0:
        job_item["hour"] = hour
      if (enable_bits & (1<<4)) > 0:
        job_item["minute"] = minute
      if (enable_bits & (1<<5)) > 0:
        job_item["days"] = days 

      job_item["id"] = random.randint(0, 1000000)
      job_item["time"] = "{:0>2}:{:0>2}".format(hour, minute)
      job_item["hour"] = hour
      job_item["minute"] = minute
      job_item["action"] = "shutdown" if action == 0 else "startup"
      
      job_list.append(job_item)
    events["events"] = job_list
    return events

  def __handle_json_command(self, json_cmd):
    if json_cmd.get("command") == "pmu-status":
      # { "command": "pmu-status", "code": 0, "battery-voltage": 0, "charger-voltage": 0, "on-battery": 0, "charge-percentage": 0, "board-temperature" }
      if json_cmd.get("code") == 0:
        # 先读取电池电流，单位毫安。约定：> 0 流入电池（充电），< 0 流出电池（放电）
        try:
          self.battery_current = int(open("/sys/class/power_supply/battery/current_now", "r").read().strip()) / 1000 # 单位毫安
        except (OSError, ValueError):
          self.battery_current = None

        # on-battery 只表示充电器是否插入，无法区分 TYPE-C 对外放电（充电宝模式）。
        # 当电流明显流出电池时（< -DISCHARGE_DEADBAND_MA），即使充电器已连接也判定为放电。修复 issue #84。
        charger_connected = json_cmd.get("on-battery") == 0 # on-battery 0 -> 充电器已连接，1 -> 仅用电池
        self.charger_connected = charger_connected
        if self.battery_current is not None and self.battery_current < -DISCHARGE_DEADBAND_MA:
          self.on_charging = False
        else:
          self.on_charging = charger_connected
        percent = json_cmd.get("charge-percentage")
        if self.prev_charge_percent is None:
          self.prev_charge_percent = percent

        raw_temp = json_cmd.get("board-temperature")
        if raw_temp is not None and raw_temp != -40:
          self.board_temperature = raw_temp
        else:
          self.board_temperature = None

        # 在充电状态下，要保证每一次接收到的充电百分比不能小于上一次的数值
        if (not self.on_charging) or (self.on_charging and percent >= self.prev_charge_percent):
          if len(self.charge_percent_buf_queue) < 10:
            self.charge_percent_buf_queue.append(percent)
          else:
            self.charge_percent_buf_queue.pop(0)
            self.charge_percent_buf_queue.append(percent)
          percent_sum = sum(self.charge_percent_buf_queue)
          self.charge_percent = percent_sum // len(self.charge_percent_buf_queue) // 100 # 充电百分比

        self.prev_charge_percent = percent

        self.battery_voltage = json_cmd.get("battery-voltage") # 单位毫伏 1000mv = 1v
        self.charger_voltage = json_cmd.get("charger-voltage") # 单位毫伏
        if self.battery_current is not None and self.battery_voltage is not None:
          self.battery_wattage = int(self.battery_current) * int(self.battery_voltage) / 1000 / 1000 # 单位瓦特（放电为负）
        self.pmu_status_received = True
    elif json_cmd.get("command") == "pmu-fw-version-get":
      if json_cmd.get("code") == 0:
        self.stm32_firmware_version = json_cmd.get("version")
    elif json_cmd.get("command") == "pmu-hw-version-get":
      if json_cmd.get("code") == 0:
        # "" = pcat-manager 尚未收到 MCU 应答；保持 None 让未知值轮询继续重试
        version = json_cmd.get("version")
        if version:
          self.pmu_hw_version = version
    elif json_cmd.get("command") == "schedule-power-event-get":
      if json_cmd.get("code") == 0:
        l = json_cmd.get("event-list")
        self.event_list = l
        self.event_list_known = True
    elif json_cmd.get("command") == "charger-on-auto-start-get":
      if json_cmd.get("code") == 0:
        state = json_cmd.get("state")
        self.charger_on_auto_start_mode["enabled"] = 'true' if state == 1 else 'false'
        self.charger_on_auto_start_mode["timeout"] = json_cmd.get("timeout", 0)
        countdown = json_cmd.get("countdown", 0)
        self.charger_on_auto_start_mode["countdown"] = 0 if countdown <= 0 else countdown
        self.charger_on_auto_start_mode["known"] = True
    elif json_cmd.get("command") == "power-on-mode-get":
      if json_cmd.get("code") == 0:
        self.power_on_mode = json_cmd.get("mode", 0)
    elif json_cmd.get("command") == "modem-status-get":
      if json_cmd.get("code") == 0:
        self.modem_signal_strength = json_cmd.get("signal-strength") #maybe not correct.
        self.modem_mode = json_cmd.get("mode")
        if self.modem_mode == "lte":
          self.modem_mode = "4G"
        elif self.modem_mode == "none":
          self.modem_mode = ""
        else:
          self.modem_mode = self.modem_mode.title()
        self.sim_state = json_cmd.get("sim-state")
        self.isp_lpmn = json_cmd.get("isp-lpmn")
        self.isp_name = isp_dict.get_name(self.isp_lpmn)
    elif json_cmd.get("command") == "network-route-mode-get":
      if json_cmd.get("code") == 0:
        self.network_route_mode = json_cmd.get("mode")
    elif json_cmd.get("command") == "modem-log-get":
      self.modem_log_response = json_cmd
      self.modem_log_seq += 1
    elif json_cmd.get("command") == "soc-log-get":
      self.soc_log_response = json_cmd
      self.soc_log_seq += 1
    elif json_cmd.get("command") == "modem-network-get":
      if json_cmd.get("code") == 0:
        self.device_type = json_cmd.get("device-type", "none")
        self.modem_apn = {"apn": json_cmd.get("apn"), "user": json_cmd.get("user"), "password": json_cmd.get("password"), "auth": json_cmd.get("auth") }
    elif json_cmd.get("command") == "pmu-io-get":
      if json_cmd.get("code") == 0:
        self.status_led_v2_enabled = json_cmd.get("status-led-v2-enabled")
        self.beeper_enabled = json_cmd.get("beeper-enabled")
        self._pmu_io_seq += 1
        self._pmu_io_last_get_ts = time.time()
    elif json_cmd.get("command") == "pmu-io-schedule-get":
      # Cache even on code!=0 so "supported:0" is visible to the version gate.
      self.io_schedule = json_cmd
      if json_cmd.get("current_led") is not None:
        self.status_led_v2_enabled = json_cmd.get("current_led")
      if json_cmd.get("current_beeper") is not None:
        self.beeper_enabled = json_cmd.get("current_beeper")
      self._pmu_io_schedule_seq += 1
    elif json_cmd.get("command") == "pmu-io-schedule-set":
      # SET ack; a follow-up get is usually queued by the daemon.
      if json_cmd.get("supported"):
        if self.io_schedule is None:
          self.io_schedule = {}
        self.io_schedule["supported"] = json_cmd.get("supported")
    elif json_cmd.get("command") == "pmu-raw-command":
      # code 0 = the daemon framed and queued the raw MCU command. The MCU's
      # reply is NOT routed back; this only proves the passthrough exists,
      # which is exactly what the schedule fallback needs to know.
      self._pmu_raw_last_code = json_cmd.get("code")
      self._pmu_raw_seq = getattr(self, "_pmu_raw_seq", 0) + 1
    elif json_cmd.get("command") == "charge-threshold-get":
      if json_cmd.get("code") == 0:
        self.charge_threshold = json_cmd.get("value")
    elif json_cmd.get("command") == "charge-threshold-set":
      if json_cmd.get("code") == 0:
        # Read back after set
        self.charge_threshold_get()
    elif json_cmd.get("command") == "battery-info-get":
      # The daemon reports supported=false on MCU firmware that predates the
      # battery info command; keep the previous values in that case.
      if json_cmd.get("code") == 0 and json_cmd.get("supported"):
        self.battery_info_supported = True
        self.battery_full_capacity = json_cmd.get("full-capacity")
        self.battery_nominal_capacity = json_cmd.get("nominal-capacity")
        self.battery_cycle_count = json_cmd.get("cycle-count")
        self.battery_capacity_learned = bool(json_cmd.get("capacity-learned"))
        self.battery_learning_active = bool(json_cmd.get("learning-active"))
        # Absent on MCU firmware older than RA2E1260717006. This is the
        # whole-loop measured resistance (cells + wiring/connector + MOSFET).
        self.battery_internal_resistance = json_cmd.get("internal-resistance-mohm")
        self.battery_runtime_seconds = json_cmd.get("runtime-seconds")
        # Resistance split (MCU fw >= RA2E1260721001): the fixed parasitic
        # (wiring/connector + MOSFET) and the cell-only resistance the daemon
        # already derived (total - parasitic, floored at 0). Absent otherwise.
        self.battery_internal_resistance_parasitic = json_cmd.get(
            "internal-resistance-parasitic-mohm")
        self.battery_internal_resistance_battery = json_cmd.get(
            "internal-resistance-battery-mohm")
        # 电池中心抽头电压 (2P2S中点, P015/180K/100K分压), 单位毫伏。
        # Absent on MCU firmware older than RA2E1260718001.
        self.battery_mid_voltage = json_cmd.get("mid-voltage-mv")
        # SOC display-factor diagnostics.
        # Absent on MCU firmware older than RA2E1260719001.
        self.battery_soc_raw_x10 = json_cmd.get("soc-raw-x10")
        self.battery_soc_empty_pct = json_cmd.get("soc-empty-learned-pct")
        self.battery_soc_disp_a_x1000 = json_cmd.get("soc-disp-a-x1000")
        self.battery_soc_disp_b_x100 = json_cmd.get("soc-disp-b-x100")
        self.battery_chg_gain_x1000 = json_cmd.get("chg-gain-x1000")
        self.battery_cur_trim_a_x1000 = json_cmd.get("cur-trim-a-x1000")
        self.battery_cur_trim_b_ma = json_cmd.get("cur-trim-b-ma")
        # Remaining capacity / energy counters.
        # Absent on MCU firmware older than RA2E1260719002.
        self.battery_remain_mah = json_cmd.get("remain-mah")
        self.battery_consumed_mwh = json_cmd.get("consumed-mwh")
        self.battery_charged_mwh = json_cmd.get("charged-mwh")
        # Protocol version + MCU health + time estimates.
        # Absent on MCU firmware older than RA2E1260719003.
        self.battery_proto_ver = json_cmd.get("protocol-ver")
        self.battery_health_pct_mcu = json_cmd.get("health-pct")
        # Absent (None) below battery-info proto 10 (MCU fw < RA2E1261016000).
        self.battery_full_charge_seen = json_cmd.get("full-charge-seen")
        self.battery_minutes_to_empty = json_cmd.get("minutes-to-empty")
        self.battery_minutes_to_full = json_cmd.get("minutes-to-full")
        # Temperature-compensation diagnostics.
        # Absent on MCU firmware older than RA2E1260719009 (proto ver < 7).
        self.battery_gauge_temp_c = json_cmd.get("gauge-temp-c")
        self.battery_temp_empty_extra_x10 = json_cmd.get("temp-empty-extra-x10")
        self.battery_r_temp_factor_x1000 = json_cmd.get("r-temp-factor-x1000")
        # MCU uptime diagnostics.
        # Absent on MCU firmware older than RA2E1260721006 (proto ver < 9).
        self.mcu_uptime_seconds = json_cmd.get("mcu-uptime-seconds")
        self.mcu_since_sleep_seconds = json_cmd.get("mcu-since-sleep-seconds")
        self.mcu_sleep_count = json_cmd.get("mcu-sleep-count")
      else:
        self.battery_info_supported = False

  def __handle_recv_buffer(self):
    # Messages are null-terminated but a single recv() may deliver a partial
    # message (no '\0' yet) or several messages plus a trailing fragment.
    # Use find() (returns -1) not index() (raises ValueError) so an
    # unterminated fragment just stays buffered for the next recv instead of
    # crashing this thread. Consume every complete message; keep the tail.
    end_flag_i = self.recv_buffer.find(b'\0')
    while end_flag_i >= 0:
      got_bytes = self.recv_buffer[:end_flag_i]
      self.recv_buffer = self.recv_buffer[(end_flag_i + 1):]
      if got_bytes:
        try:
          cmd_json = json.loads(got_bytes)
          self.__handle_json_command(cmd_json)
        except Exception as e:
          if self.enable_log:
            pcat_logger.logger.error("Invalid recv cmd: %s", e)
      end_flag_i = self.recv_buffer.find(b'\0')

  def query_pmu_status(self):
    if not self.sock or not self.sock_connected:
      return
    cmd = { "command": "pmu-status" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def query_modem_status(self):
    if not self.sock or not self.sock_connected:
      # print("Failed to query modem status: Not connected to server!")
      return
    cmd = { "command": "modem-status-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def query_network_route_mode_status(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to query network route mode status: Not connected to server!")
      return
    cmd = { "command": "network-route-mode-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def modem_log_get(self, offset=None, max_size=65536):
    """Request a chunk of the daemon's in-memory modem dial log (fm350-mm /
    quectel-cm stdout, up to 16MB retained). offset is absolute (monotonic
    since daemon start); None returns the last max_size bytes. The reply
    lands in self.modem_log_response and bumps self.modem_log_seq."""
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to query modem log: Not connected to server!")
      return
    cmd = { "command": "modem-log-get", "max-size": int(max_size) }
    if offset is not None:
      cmd["offset"] = int(offset)
    cmd_str = json.dumps(cmd)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def soc_log_get(self, from_seq=None, max_records=256):
    """Request SOC gauge diagnostic records from the daemon's stitched history.

    from_seq is the MCU's monotonic record sequence number; None asks for the
    newest max_records. The daemon drains the MCU's 32-slot RAM ring over
    com=173 and keeps the stitched result in its own RAM history, so from_seq
    can reach further back than the ring itself. The reply lands in
    self.soc_log_response and bumps self.soc_log_seq.

    Daemon contract (pcat-manager side, mirrors modem-log-get):
      request { "command": "soc-log-get", "from-seq": <int>, "max-records": <int> }
      reply   { "command": "soc-log-get", "code": 0, "supported": true,
                "seq-oldest": <int>,      # oldest record the daemon still holds
                "seq-next":   <int>,      # next seq the MCU will write
                "log-version": 1,
                "records": [ { "seq", "t", "v", "i", "vbus", "soc-raw-x10",
                               "soc-disp", "temp", "event", "pre", "flags" } ] }
    Field semantics are the MCU's soc_log_rec_t (freertos_ek_ra2e1_ep/src/soc_log.h).
    supported=false on MCU firmware older than RA2E1260727000."""
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to query SOC log: Not connected to server!")
      return
    cmd = { "command": "soc-log-get", "max-records": int(max_records) }
    if from_seq is not None:
      cmd["from-seq"] = int(from_seq)
    cmd_str = json.dumps(cmd)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def query_schedule_power_events(self):
    if not self.sock or not self.sock_connected:
      # print("Failed to query schedule power events: Not connected to server!")
      return
    cmd = { "command": "schedule-power-event-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  # 查询当前是否启用车载模式
  def query_charger_on_auto_start(self):
    if not self.sock or not self.sock_connected:
      # print("Failed to query charger-on-auto-start-get: Not connected to server!")
      return
    cmd = { "command": "charger-on-auto-start-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def set_charger_on_auto_start(self, enable, delay_timeout):
    if not self.sock or not self.sock_connected:
      # print("Failed to query charger-on-auto-start-set: Not connected to server!")
      return
    if enable:
      cmd = { "command": "charger-on-auto-start-set", "state": 1, "timeout": delay_timeout }
    else:
      cmd = { "command": "charger-on-auto-start-set", "state": 0, "timeout": delay_timeout }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    # Re-read right after the set: the daemon handles commands in order, so
    # this returns the post-set state. The periodic poll for this setting is
    # 60s (it rarely changes), so without this the UI could show stale state.
    self.query_charger_on_auto_start()

  def query_power_on_mode(self):
    if not self.sock or not self.sock_connected:
      return
    cmd = { "command": "power-on-mode-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def set_power_on_mode(self, mode):
    """
    Set power-on mode for the device.

    Args:
        mode (int): Power mode value
            0 - Manual mode (default): Do nothing when device is powered
            1 - Auto power-on: Auto power-on when device is powered
            2 - Force power mode: Auto power-on when external power connects,
                instant power-off when disconnected (ignores battery checks)
    """
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to set power-on-mode: Not connected to server!")
      return
    cmd = { "command": "power-on-mode-set", "mode": mode }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    # Re-read right after the set (periodic poll for this setting is 60s)
    self.query_power_on_mode()

  # 设置飞行模式
  def set_rfkill_state(self, enable):
    if not self.sock or not self.sock_connected:
      return
    state = 1 if enable else 0
    cmd = { "command": "modem-rfkill-mode-set", "state": state }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def query_stm32_version(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to query stm32 version: Not connected to server!")
      return
    cmd = { "command": "pmu-fw-version-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def query_pmu_hw_version(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to query PMU hardware revision: Not connected to server!")
      return
    cmd = { "command": "pmu-hw-version-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def set_auto_wan(self, auto_wan):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to set auto wan: Not connected to server!")
      return
    cmd = { "command": "modem-network-setup", "modem-iface-auto-stop-if-wired": auto_wan }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
  
  def get_auto_wan(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to get auto wan: Not connected to server!")
      return
    cmd = { "command": "modem-network-setup" } 
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def modem_apn_setup(self, apn, user, password, auth):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to modem_apn_setup: Not connected to server!")
      return
    cmd = { "command": "modem-network-setup", "apn": apn, "user": user, "password": password, "auth": auth }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock) 

  def modem_apn_get(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to modem_apn_get: Not connected to server!")
      return
    cmd = { "command": "modem-network-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def pmu_io_set(self, status_led_v2_enabled=None, beeper_enabled=None):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to pmu_io_set: Not connected to server!")
      return
    
    cmd = { "command": "pmu-io-set" }
    if status_led_v2_enabled is not None:
      cmd["status-led-v2-enabled"] = 1 if status_led_v2_enabled else 0
      # Optimistic local cache: the follow-up pmu-io-get is async and the
      # device_control "Current" badge polls this field every 5s. Without this
      # the UI stays on the previous value even when the LED already changed.
      self.status_led_v2_enabled = cmd["status-led-v2-enabled"]
    if beeper_enabled is not None:
      cmd["beeper-enabled"] = 1 if beeper_enabled else 0
      self.beeper_enabled = cmd["beeper-enabled"]
    
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)

    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    
    # Wait for the set to land, then confirm with a blocking read-back so
    # callers (and the next status poll) see the PMU's actual state.
    time.sleep(1)
    self.pmu_io_get(wait=True, timeout=1.5)

  def pmu_date_time_sync_utc(self):
    """Push host UTC wall clock to the MCU RTC."""
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to pmu_date_time_sync_utc: Not connected!")
      return
    cmd = {"command": "pmu-date-time-sync"}
    cmd_str = json.dumps(cmd)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def pmu_io_schedule_set(self, led, beeper):
    """Push MCU-owned LED/beeper schedule. led/beeper are dicts with
    mode/start_hour/start_minute/end_hour/end_minute (UTC)."""
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to pmu_io_schedule_set: Not connected!")
      return
    cmd = {
        "command": "pmu-io-schedule-set",
        "led": led or {},
        "beeper": beeper or {},
    }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    # Give the daemon a beat to UART the set+get before callers read cache,
    # and hand the read-back to the caller: a push that cannot be verified
    # against it must not be recorded as delivered (the daemon this shipped
    # against dropped these commands silently -- forum report r7897).
    time.sleep(0.3)
    return self.pmu_io_schedule_get(wait=True, timeout=1.5)

  def pmu_io_schedule_get(self, wait=False, timeout=1.5):
    """Queue pmu-io-schedule-get. On wait, block until a reply updates cache."""
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to pmu_io_schedule_get: Not connected!")
      return None
    seq_before = getattr(self, "_pmu_io_schedule_seq", 0)
    cmd = {"command": "pmu-io-schedule-get"}
    cmd_str = json.dumps(cmd)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    if not wait:
      return getattr(self, "io_schedule", None)
    deadline = time.time() + max(0.1, float(timeout))
    while time.time() < deadline:
      if getattr(self, "_pmu_io_schedule_seq", 0) != seq_before:
        break
      time.sleep(0.05)
    return getattr(self, "io_schedule", None)

  def pmu_raw_command(self, mcu_command, payload_hex="", need_ack=True,
                      wait=False, timeout=1.5):
    """Send a raw MCU command through the daemon's pmu-raw-command
    passthrough (an even-length hex payload, framed verbatim).

    The MCU's own reply is never routed back, so this is fire-and-forget at
    the MCU level. With wait=True it blocks for the daemon's queueing reply
    and returns True only when the daemon answered code 0 -- which is how the
    schedule fallback tells "old daemon, no passthrough" (timeout) apart from
    "queued to the MCU". Without wait it returns True once the command is on
    the send queue.
    """
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to pmu_raw_command: Not connected!")
      return False
    cmd = {
        "command": "pmu-raw-command",
        "mcu-command": int(mcu_command),
        "payload": payload_hex,
        "need-ack": 1 if need_ack else 0,
    }
    seq_before = getattr(self, "_pmu_raw_seq", 0)
    self.send_cmd_queue.put(json.dumps(cmd).encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    if not wait:
      return True
    deadline = time.time() + max(0.1, float(timeout))
    while time.time() < deadline:
      if getattr(self, "_pmu_raw_seq", 0) != seq_before:
        return self._pmu_raw_last_code == 0
      time.sleep(0.05)
    return False

  def pmu_io_get(self, wait=False, timeout=1.5, max_age=None):
    """Queue a pmu-io-get.

    wait: block until a response arrives (or timeout).
    max_age: if the last successful get is newer than this many seconds,
             skip issuing another command (shared by LED + beeper status
             polls so they do not double-hit the STM32 every 5s).
    """
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to pmu_io_get: Not connected to server!")
      return
    if (max_age is not None and self._pmu_io_last_get_ts
            and (time.time() - self._pmu_io_last_get_ts) < float(max_age)):
      return
    seq_before = self._pmu_io_seq
    cmd = { "command": "pmu-io-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    if not wait:
      return
    # Socket replies land on the PcSocketClient thread; poll until we see a
    # new pmu-io-get response or the timeout elapses.
    deadline = time.time() + max(0.1, float(timeout))
    while time.time() < deadline:
      if self._pmu_io_seq != seq_before:
        return
      time.sleep(0.05)

  def charge_threshold_set(self, value):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to charge_threshold_set: Not connected to server!")
      return
    cmd = { "command": "charge-threshold-set", "value": value }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def charge_threshold_get(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to charge_threshold_get: Not connected to server!")
      return
    cmd = { "command": "charge-threshold-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def battery_info_get(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to battery_info_get: Not connected to server!")
      return
    cmd = { "command": "battery-info-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  def beep_play(self, tones, loop=False):
    # Play a tone sequence on the MCU buzzer (pmu-beep-play). Each tone is
    # {"hz": .., "ms": ..}; hz 0 is a rest. The MCU takes at most 20 steps per
    # command, so callers chunk longer sequences and pace the chunks. A new
    # sequence interrupts the current one; loop=True repeats until interrupted.
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to beep_play: Not connected to server!")
      return
    cmd = { "command": "pmu-beep-play", "tones": tones, "loop": 1 if loop else 0 }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

  # @param event_job : {"action":0,"enabled":1,"enable-bits":56,"hour":23,"minute":59,"dow-bits":1}
  # action: 0 -> poweroff; 1 -> bootup
  # enabled: 0 -> Disabled; 1 -> Enabled
  # year, month, day ignored
  # enable-bits: 8 bits: 0, day of week, minute, hour, day, month, year
  # dow-bites: Day of week repeat flag: 8 bits: 0b00000001 -> Repeat on weekend; 0b00000011 -> Repeat on weekend and Monday
  def set_schedule_power_events(self, event_job_list):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to query schedule power events: Not connected to server!")
      return
    cmd = { "command": "schedule-power-event-set", "event-list": event_job_list }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    # Re-read right after the set (periodic poll for this setting is 60s)
    self.query_schedule_power_events()

  def __teardown_socket(self):
    """Close the current socket and reset every per-connection field so the
    outer connect loop restarts from a clean slate. A stale (closed) fd left
    in output_fds makes the next select() raise immediately, and a half-read
    recv_buffer fragment from the dead connection would corrupt the first
    message of the new one -- so both are cleared here."""
    self.sock_connected = False
    sock = self.sock
    self.sock = None
    self.output_fds = []
    self.input_fds = []
    self.recv_buffer = b''
    if sock is not None:
      try:
        sock.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass
      try:
        sock.close()
      except OSError:
        pass

  def connect_to_server(self):
    while self.running_flag:
      try:
        if self.enable_log:
          pcat_logger.logger.debug("Try to connect to unix socket ctrl server.")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR, 1)
        self.sock.connect(self.server_address)
      except Exception as e:
        self.sock_connected = False
        if self.enable_log:
          pcat_logger.logger.warning("Failed to connect to unix socket ctrl server: %s", e)
        time.sleep(5)
        continue

      self.sock_connected = True
      if self.enable_log:
        pcat_logger.logger.info("Connected to unix socket ctrl server.\n") 
      while True:
        # pcat_logger.logger.debug("socket_client: select checking.")
        self.input_fds = [self.sock]
        try:
          # 1s timeout: incoming data still wakes select immediately; only
          # outbound commands queued mid-wait are delayed (up to 1s), which
          # is acceptable for the 10s-cycle status queries this carries.
          # 0.1s here meant 10 wakeups/sec forever, wasting CPU when idle.
          ready_to_read, ready_to_write, in_error = \
            select.select(self.input_fds, self.output_fds, [], 1.0)
        except select.error:
          # Typically a stale/closed fd left in output_fds. Tear everything
          # down and let the outer loop reconnect from scratch.
          if self.enable_log:
            pcat_logger.logger.warning("select error in socket connection")
          self.__teardown_socket()
          break
        if len(ready_to_read) > 0:
          try:
            recv_bytes = self.sock.recv(2048)
          except (BlockingIOError, InterruptedError):
            # select reported readable but the read would block / was
            # interrupted; nothing to consume this round.
            recv_bytes = None
          except OSError as e:
            if self.enable_log:
              pcat_logger.logger.warning("socket recv error: %s", e)
            self.__teardown_socket()
            break
          if recv_bytes == b'':
            # EOF: the daemon closed the connection. A closed fd stays
            # readable forever, so without this the loop would spin at 100%
            # CPU (recv() -> b'' with no reconnect), freezing all PMU / modem
            # / battery status until the process restarts. Reconnect instead.
            if self.enable_log:
              pcat_logger.logger.warning("socket peer closed (EOF), reconnecting")
            self.__teardown_socket()
            break
          if recv_bytes:
            if self.enable_log:
              pcat_logger.logger.debug(f"Recv socket data: {recv_bytes}")
            self.recv_buffer = self.recv_buffer + recv_bytes
            self.__handle_recv_buffer()
        if len(ready_to_write) > 0:
          try:
            next_msg = self.send_cmd_queue.get_nowait()  #非阻塞获取
            if self.enable_log:
              pcat_logger.logger.debug("Ready to write unix socket: %s", next_msg)
            self.sock.send(next_msg)
            if self.enable_log:
              pcat_logger.logger.debug("Written to unix socket: %s", next_msg)
          except queue.Empty:
            if self.sock in self.output_fds:
              self.output_fds.remove(self.sock)
            if self.enable_log:
              pcat_logger.logger.warning("socket_client: queue.Empty")
          except Exception as e:  #发送的时候客户端关闭了则会出现 writable 和 readable 同时有数据
            # A send failure means the peer is gone; reconnect rather than
            # spin on a dead socket.
            err_msg = "Send Data Error! ErrMsg:%s" % str(e)
            if self.enable_log:
              pcat_logger.logger.warning(err_msg)
            self.__teardown_socket()
            break

  def run(self):
    pcat_logger.logger.info("PcSocketClient thread run.")
    self.running_flag = True
    # Supervisor: connect_to_server() should loop forever, but if any
    # unexpected exception escapes it the thread would die and all PMU /
    # battery / modem status updates would stop until a full process restart.
    # Restart the connection loop after a short backoff instead.
    while self.running_flag:
      try:
        self.connect_to_server()
      except Exception as e:
        pcat_logger.logger.error("PcSocketClient connect loop crashed, restarting: %s", e)
        time.sleep(2)
