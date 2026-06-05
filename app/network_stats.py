#@Author    : qichunren

import os
from datetime import datetime, date, timedelta

from database import init_db, connect_db, get_rows
from pcat_util import pcat_logger

WIRED_DEVICE = "eth0"

SQL = '''
CREATE TABLE network_stats(
  id INTEGER PRIMARY KEY,
  rx_bytes INTEGER,
  tx_bytes INTEGER,
  device VARCHAR(10),
  remark VARCHAR(30),
  created_at DATETIME NOT NULL
);
'''

# device: usb0 -> 5G (Qualcomm)
# device: wwan0 -> 4G (Qualcomm)
# device: eth2 -> FM350-GL
# direction: rx or tx
def get_device_x_bytes(device, direction):
  try:
    with open("/sys/class/net/{0}/statistics/{1}_bytes".format(device, direction)) as f:
      return int(f.readline().strip())
  except (FileNotFoundError, ValueError):
    return 0
  
class NetworkStats:

  def __init__(self, stats_db):
    self.stats_db = stats_db
    init_db(stats_db, SQL)
    connect_db(stats_db)
    self.last_log_rx_speed = None
    self.last_log_tx_speed = None
    self.last_rx_bytes = {}
    self.last_tx_bytes = {}
  
    if os.path.exists("/tmp/pcat-manager-web-bootup"):
      self.device_bootup = False
    else:
      f = open("/tmp/pcat-manager-web-bootup", "w")
      f.close()
      self.device_bootup = True

  def log_rx_speed(self):
    bytes_0 = get_device_x_bytes("eth0", "rx")
    bytes_1 = get_device_x_bytes("usb0", "rx")
    bytes_2 = get_device_x_bytes("wwan0", "rx")
    bytes_3 = get_device_x_bytes("eth2", "rx")
    if self.last_log_rx_speed is None:
      speed = bytes_0 + bytes_1 + bytes_2 + bytes_3
      self.last_log_rx_speed = speed
      return 0
    else:
      speed_0 = bytes_0 + bytes_1 + bytes_2 + bytes_3
      speed = speed_0 - self.last_log_rx_speed
      self.last_log_rx_speed = speed_0
      return speed

  def log_tx_speed(self):
    bytes_0 = get_device_x_bytes("eth0", "tx")
    bytes_1 = get_device_x_bytes("usb0", "tx")
    bytes_2 = get_device_x_bytes("wwan0", "tx")
    bytes_3 = get_device_x_bytes("eth2", "tx")
    if self.last_log_tx_speed is None:
      speed = bytes_0 + bytes_1 + bytes_2 + bytes_3
      self.last_log_tx_speed = speed
      return 0
    else:
      speed_0 = bytes_0 + bytes_1 + bytes_2 + bytes_3
      speed = speed_0 - self.last_log_tx_speed
      self.last_log_tx_speed = speed_0
      return speed

  def log_stats(self):
    pcat_logger.logger.debug("Log network usage stats")
    self.log_net_device_bytes("usb0", "")
    self.log_net_device_bytes("wwan0", "")
    self.log_net_device_bytes("eth2", "")
    self.log_net_device_bytes(WIRED_DEVICE, "")

  def clear_net_device_bytes(self):
    sql_command = "delete from network_stats"
    connect_db(self.stats_db, [sql_command, "VACUUM"])

  def log_net_device_bytes(self, device, remark = ""):
    now = datetime.now()
    time_string = now.strftime("%Y-%m-%d %H:%M:%S")
    rx_bytes = get_device_x_bytes(device, "rx")
    tx_bytes = get_device_x_bytes(device, "tx")
    # print("#### Debug rx_bytes from file:", rx_bytes, "tx_bytes", tx_bytes, "for device", device)
    prev_rx_bytes = self.last_rx_bytes.get(device)
    prev_tx_bytes = self.last_tx_bytes.get(device)
    if prev_rx_bytes is None:
      self.last_rx_bytes[device] = rx_bytes
    if prev_tx_bytes is None:         
      self.last_tx_bytes[device] = tx_bytes
    if prev_rx_bytes is None or prev_tx_bytes is None:
      return      

    # print("     Debug: last rx_bytes", prev_rx_bytes, "last_tx_bytes", prev_tx_bytes, "for device", device)
    rx_bytes_diff = 0
    tx_bytes_diff = 0
    if prev_rx_bytes is not None and rx_bytes >= prev_rx_bytes :
      rx_bytes_diff = rx_bytes - prev_rx_bytes
    if prev_tx_bytes is not None and tx_bytes >= prev_tx_bytes:
      tx_bytes_diff = tx_bytes - prev_tx_bytes

    # print("     Debug: diff rx_bytes", rx_bytes_diff, "last_tx_bytes", tx_bytes_diff, "for device", device)
    if rx_bytes_diff > 0 or tx_bytes_diff > 0:
      sql_command = "insert into network_stats(rx_bytes,tx_bytes,device,remark,created_at)values({0},{1},'{2}','{3}','{4}')".format(rx_bytes_diff, tx_bytes_diff, device, remark, time_string)
      connect_db(self.stats_db, [sql_command])
      self.last_rx_bytes[device] = rx_bytes
      self.last_tx_bytes[device] = tx_bytes

  # network_type: mobile
  # network_type: wired
  def today_bytes(self, network_type):
    now = datetime.now()
    time_string = now.strftime("%Y-%m-%d 00:00:00")
    if network_type == "mobile":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where (device='usb0' or device='wwan0' or device='eth2') and created_at >= '{0}'".format(time_string)
    elif network_type == "wired":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where device='{0}' and created_at >= '{1}'".format(WIRED_DEVICE, time_string)
    rows = get_rows(self.stats_db, sql_command)
    if len(rows) > 0:
      a = rows[0][0]
      b = rows[0][1]
      if a is None:
        a = 0
      if b is None:
        b = 0
      return a + b

  def this_week_bytes(self, network_type):
    today = date.today()
    start_day = today - timedelta(days=today.weekday())
    end_day = start_day + timedelta(days=6)
    start_ts = start_day.strftime("%Y-%m-%d 00:00:00")
    end_ts = end_day.strftime("%Y-%m-%d 23:59:59")
    if network_type == "mobile":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where (device='usb0' or device='wwan0' or device='eth2') and created_at >= '{0}' and created_at <= '{1}'".format(start_ts, end_ts)
    elif network_type == "wired":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where device='{0}' and created_at >= '{1}' and created_at <= '{2}'".format(WIRED_DEVICE, start_ts, end_ts)
    rows = get_rows(self.stats_db, sql_command)
    if len(rows) > 0:
      a = rows[0][0]
      b = rows[0][1]
      if a is None:
        a = 0
      if b is None:
        b = 0
      return a + b

  def month_bytes(self, month_date, network_type):
    start_day = month_date.replace(day=1)
    start_ts = start_day.strftime("%Y-%m-%d 00:00:00")

    next_month = month_date.replace(day=28) + timedelta(days=4)
    end_day = next_month - timedelta(days=next_month.day)
    end_ts = end_day.strftime("%Y-%m-%d 23:59:59")

    if network_type == "mobile":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where (device='usb0' or device='wwan0' or device='eth2') and created_at >= '{0}' and created_at <= '{1}'".format(start_ts, end_ts)
    elif network_type == "wired":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where device='{0}' and created_at >= '{1}' and created_at <= '{2}'".format(WIRED_DEVICE, start_ts, end_ts)
    rows = get_rows(self.stats_db, sql_command)
    if len(rows) > 0:
      a = rows[0][0]
      b = rows[0][1]
      if a is None:
        a = 0
      if b is None:
        b = 0
      return a + b

## For self test
if __name__ == "__main__":
  a1 = get_device_x_bytes("enp0s9", "rx")
  a2 = get_device_x_bytes("enp0s9", "tx")
  print("enp0s9 rx_bytes", a1, "and tx bytes", a2)
  network_stat = NetworkStats()
  print("Is first bootup?", network_stat.device_bootup)
  a = network_stat.today_bytes()
  print(a)
  a = network_stat.this_week_bytes()
  print(a)
  six_m = datetime(2022, 6, 17)
  five_m = datetime(2022, 5, 17)
  a = network_stat.month_bytes(six_m)
  print(a)
  a = network_stat.month_bytes(five_m)
  print(a)



