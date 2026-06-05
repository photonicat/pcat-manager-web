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
    self.charge_percent = 100 # socket 返回的数据是 0-10000
    self.prev_charge_percent = None # 上一次接收到的数值
    self.charge_percent_buf_queue = []
    self.battery_voltage = None # 单位毫伏
    self.charger_voltage = 5035 # 单位毫伏
    self.battery_current = None # 单位毫安
    self.battery_wattage = None # 单位瓦特
    self.board_temperature = None
    self.event_list = [] # Schedule event list

    # 根据充电器连接状态自动开关机（车载模式）
    self.charger_on_auto_start_mode = {"enabled": 'false', "timeout": 0, "countdown": 0}
    self.power_on_mode = 0  # 0: Manual, 1: Auto power-on, 2: Direct power mode
    self.modem_signal_strength = 0
    self.modem_mode = "" # Modem状态，可能的值：none / 2g / 3g / lte / 5g
    self.sim_state = "absent" # SIM卡状态，可能的值：absent / not-ready / ready / need-pin / need-puk / personalized-network / bad
    self.isp_name = "" # 网络服务提供商名称
    self.isp_plmn = "" # 网络服务提供商编号
    self.network_route_mode = "none" # 路由模式，可能的值：none / wired / mobile / unknown
    self.stm32_firmware_version = None # STM32固件版本号，默认不存在固件设备的话，为空字符串
    self.device_type = "none" # From pcat-manager
    self.modem_apn = None # type: Dict
    
    # PMU I/O settings
    self.status_led_v2_enabled = None # Status LED v2 enabled state
    self.beeper_enabled = None # Beeper enabled state
    self.charge_threshold = None # Battery charge limit threshold (80-100)

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
        self.on_charging = json_cmd.get("on-battery") == 0 # on-battery 0 -> 表示充电，1 -> 表示没有插电源，在用电池的电
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
        self.battery_current = int(open("/sys/class/power_supply/battery/current_now", "r").read().strip())/1000 # 单位毫安
        self.battery_wattage = int(self.battery_current) * int(self.battery_voltage) / 1000 / 1000 # 单位瓦特
    elif json_cmd.get("command") == "pmu-fw-version-get":
      if json_cmd.get("code") == 0:
        self.stm32_firmware_version = json_cmd.get("version")
    elif json_cmd.get("command") == "schedule-power-event-get":
      if json_cmd.get("code") == 0:
        l = json_cmd.get("event-list")
        self.event_list = l
    elif json_cmd.get("command") == "charger-on-auto-start-get":
      if json_cmd.get("code") == 0:
        state = json_cmd.get("state")
        self.charger_on_auto_start_mode["enabled"] = 'true' if state == 1 else 'false'
        self.charger_on_auto_start_mode["timeout"] = json_cmd.get("timeout", 0)
        countdown = json_cmd.get("countdown", 0)
        self.charger_on_auto_start_mode["countdown"] = 0 if countdown <= 0 else countdown
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
    elif json_cmd.get("command") == "modem-network-get":
      if json_cmd.get("code") == 0:
        self.device_type = json_cmd.get("device-type", "none")
        self.modem_apn = {"apn": json_cmd.get("apn"), "user": json_cmd.get("user"), "password": json_cmd.get("password"), "auth": json_cmd.get("auth") }
    elif json_cmd.get("command") == "pmu-io-get":
      if json_cmd.get("code") == 0:
        self.status_led_v2_enabled = json_cmd.get("status-led-v2-enabled")
        self.beeper_enabled = json_cmd.get("beeper-enabled")
    elif json_cmd.get("command") == "charge-threshold-get":
      if json_cmd.get("code") == 0:
        self.charge_threshold = json_cmd.get("value")
    elif json_cmd.get("command") == "charge-threshold-set":
      if json_cmd.get("code") == 0:
        # Read back after set
        self.charge_threshold_get()

  def __handle_recv_buffer(self):
    end_flag_i = self.recv_buffer.index(b'\0')
    while end_flag_i >= 0 and len(self.recv_buffer) > 0:
      got_bytes = self.recv_buffer[:end_flag_i]
      try:
        cmd_json = json.loads(got_bytes)
      except:
        pass
      try:
        cmd_json = json.loads(got_bytes)
        self.__handle_json_command(cmd_json)
      except Exception as e:
        if self.enable_log:
          pcat_logger.logger.error("Invalid recv cmd: %s", e)
      self.recv_buffer = self.recv_buffer[(end_flag_i+1):]
      if len(self.recv_buffer) > 0:
        end_flag_i = self.recv_buffer.index(b'\0')

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
    if beeper_enabled is not None:
      cmd["beeper-enabled"] = 1 if beeper_enabled else 0
    
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)

    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)
    
    # Wait for response and read back
    time.sleep(1)
    self.pmu_io_get()

  def pmu_io_get(self):
    if not self.sock or not self.sock_connected:
      if self.enable_log:
        pcat_logger.logger.warning("Failed to pmu_io_get: Not connected to server!")
      return
    cmd = { "command": "pmu-io-get" }
    cmd_str = json.dumps(cmd)
    if self.enable_log:
      pcat_logger.logger.debug("Add cmd data to queue: %s", cmd_str)
    self.send_cmd_queue.put(cmd_str.encode() + b'\0')
    if self.sock not in self.output_fds:
      self.output_fds.append(self.sock)

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
          ready_to_read, ready_to_write, in_error = \
            select.select(self.input_fds, self.output_fds, [], 0.1)
        except select.error:
          self.sock.shutdown(2)    # 0 = done receiving, 1 = done sending, 2 = both
          self.sock.close()
          # connection error event here, maybe reconnect
          if self.enable_log:
            pcat_logger.logger.warning("select error in socket connection")
          break
        if len(ready_to_read) > 0:
          recv_bytes = self.sock.recv(2048)
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
            self.output_fds.remove(self.sock)
            if self.enable_log:
              pcat_logger.logger.warning("socket_client: queue.Empty")
          except Exception as e:  #发送的时候客户端关闭了则会出现 writable 和 readable 同时有数据，会出现 send_cmd_queue 的 keyerror
            err_msg = "Send Data Error! ErrMsg:%s" % str(e)
            if self.enable_log:
              pcat_logger.logger.warning(err_msg)
            if self.sock in self.output_fds:
              self.output_fds.remove(self.sock)

  def run(self):
    pcat_logger.logger.info("PcSocketClient thread run.")
    self.running_flag = True
    self.connect_to_server()
