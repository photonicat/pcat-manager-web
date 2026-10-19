#@Author    : qichunren

import os
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta

from database import init_db, connect_db, get_rows
from pcat_util import pcat_logger

WIRED_DEVICE = "eth0"

# LAN bridge whose member interfaces carry all client (NAT) traffic, regardless
# of which egress (ethernet WAN, cellular, or WiFi-as-WAN) is active.
LAN_BRIDGE = "br-lan"


def get_bridge_members(bridge):
  """Return the member interfaces of a Linux bridge (e.g. eth1, phy0-ap0).

  Reads /sys/class/net/<bridge>/brif/. The bridge's own counters undercount
  forwarded traffic (they only see host-local packets), so the live NAT speed
  is computed by summing the members instead. Returns [] if the bridge has no
  members or does not exist."""
  try:
    return os.listdir("/sys/class/net/{0}/brif".format(bridge))
  except (FileNotFoundError, NotADirectoryError, PermissionError):
    return []


def get_sta_devices():
  """Upstream WiFi STA netdevs (e.g. phy1-sta0), or [] when WiFi is not the
  upstream.

  Resolved per sample rather than hardcoded: which radio carries the STA is
  user-selectable (onboard/PCIe), so the name is phy0-sta0 or phy1-sta0, and
  the netdev only exists while the STA is up. Same shape as the egress test
  in app.get_active_egress()."""
  try:
    return [d for d in os.listdir("/sys/class/net")
            if d.startswith("phy") and "-sta" in d]
  except (FileNotFoundError, NotADirectoryError, PermissionError):
    return []

SQL = '''
CREATE TABLE network_stats(
  id INTEGER PRIMARY KEY,
  rx_bytes INTEGER,
  tx_bytes INTEGER,
  device VARCHAR(10),
  remark VARCHAR(30),
  created_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_network_stats_device_created ON network_stats(device, created_at);
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

  # Seconds between flash writes. Deltas are accumulated in RAM and written
  # as one batched transaction: the stats DB lives on the /etc overlay
  # (eMMC), and the previous insert-per-device-per-30s did ~5-10k fsynced
  # journal cycles per day. A power cut loses at most this window of byte
  # counters, which is acceptable for usage statistics.
  FLUSH_INTERVAL = 600
  # Keep ~13 months so month-over-month statistics survive a full year;
  # without retention the table grew (and slowed the SUM queries) forever.
  RETENTION_DAYS = 400

  def __init__(self, stats_db):
    self.stats_db = stats_db
    init_db(stats_db, SQL)
    connect_db(stats_db)
    # Existing DBs predate the index in SQL above; ensure it here.
    connect_db(stats_db, ["CREATE INDEX IF NOT EXISTS idx_network_stats_device_created ON network_stats(device, created_at)"])
    self.last_log_rx_speed = None
    self.last_log_tx_speed = None
    self.last_rx_bytes = {}
    self.last_tx_bytes = {}
    self._pending = {}  # device -> [rx_diff_sum, tx_diff_sum], awaiting flush
    self._pending_lock = threading.Lock()
    self._last_flush = time.time()

    if os.path.exists("/tmp/pcat-manager-web-bootup"):
      self.device_bootup = False
    else:
      f = open("/tmp/pcat-manager-web-bootup", "w")
      f.close()
      self.device_bootup = True

  # Egress interfaces, used as a fallback when the LAN bridge has no members.
  # eth2 is the FM350-GL (RNDIS), whose counter resets to 0 on every reconnect.
  SPEED_DEVICES = ("eth0", "usb0", "wwan0", "eth2")

  def _aggregate_speed(self, devices, direction, last_bytes):
    # Sum per-interface deltas instead of diffing the summed total. A single
    # interface resetting its counter (e.g. FM350 reconnect, or a WiFi client
    # roaming off the AP) would otherwise make the summed total drop and yield
    # a bogus negative speed that gets clamped to 0. Per-interface clamping
    # isolates each reset. Interfaces that vanish between samples (e.g. an AP
    # that went down) simply stop contributing.
    speed = 0
    first_sample = not last_bytes
    seen = set()
    for dev in devices:
      seen.add(dev)
      cur = get_device_x_bytes(dev, direction)
      prev = last_bytes.get(dev)
      if prev is not None and cur >= prev:
        speed += cur - prev
      elif prev is not None:
        speed += cur  # counter reset -> count from 0
      last_bytes[dev] = cur
    # Drop baselines for interfaces no longer present so a later reappearance
    # (with a fresh low counter) is treated as a first sample, not a huge jump.
    for dev in list(last_bytes.keys()):
      if dev not in seen:
        del last_bytes[dev]
    if first_sample:
      return 0
    return speed

  def _nat_speed(self, lan_direction, last_bytes):
    # Live NAT throughput = traffic across the LAN bridge members (all clients,
    # wired + WiFi), independent of which egress carries it (wifi-over-wifi,
    # eth->wifi, wwan->wifi all count). From the LAN side the directions are
    # mirrored vs the egress: bytes the router sends to clients (member tx) is
    # the clients' download. Falls back to the egress interfaces (same-direction)
    # when the bridge has no members (unusual configs / not yet up).
    members = get_bridge_members(LAN_BRIDGE)
    if members:
      return self._aggregate_speed(members, lan_direction, last_bytes)
    egress_direction = "rx" if lan_direction == "tx" else "tx"
    # Include the WiFi STA: with WiFi as the upstream it is the only egress
    # carrying traffic, so leaving it out made this fallback report 0.
    devices = tuple(self.SPEED_DEVICES) + tuple(get_sta_devices())
    return self._aggregate_speed(devices, egress_direction, last_bytes)

  def log_rx_speed(self):
    # Download as seen by clients: bytes the router *sends* to clients
    # (LAN member tx); egress fallback uses rx.
    if self.last_log_rx_speed is None:
      self.last_log_rx_speed = {}
    return self._nat_speed("tx", self.last_log_rx_speed)

  def log_tx_speed(self):
    # Upload as seen by clients: bytes received *from* clients (LAN member rx);
    # egress fallback uses tx.
    if self.last_log_tx_speed is None:
      self.last_log_tx_speed = {}
    return self._nat_speed("rx", self.last_log_tx_speed)

  def log_stats(self):
    pcat_logger.logger.debug("Log network usage stats")
    self.log_net_device_bytes("usb0", "")
    self.log_net_device_bytes("wwan0", "")
    self.log_net_device_bytes("eth2", "")
    self.log_net_device_bytes(WIRED_DEVICE, "")
    # WiFi upstream. Its bytes are not summed into either the "mobile" or the
    # "wired" totals (it is neither, and folding it into "mobile" would inflate
    # the SIM quota users track), but they are recorded so the history exists
    # once a WiFi category is added -- counters not sampled now are gone for
    # good. Whichever radio holds the STA, the name is resolved per sample.
    for sta in get_sta_devices():
      self.log_net_device_bytes(sta, "")
    if time.time() - self._last_flush >= self.FLUSH_INTERVAL:
      self.flush_pending()

  def flush_pending(self):
    """Write the accumulated per-device deltas as one transaction (one
    journal/fsync cycle instead of one per device per sample). Called on the
    periodic flush window and before the usage-sum queries, so the stats
    page never shows data more than one sample behind."""
    self._last_flush = time.time()
    with self._pending_lock:
      if not self._pending:
        return
      pending, self._pending = self._pending, {}
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(rx, tx, device, "", now_s) for device, (rx, tx) in pending.items()]
    try:
      con = sqlite3.connect(self.stats_db, timeout=15)
      with con:
        con.executemany(
          "insert into network_stats(rx_bytes,tx_bytes,device,remark,created_at)values(?,?,?,?,?)",
          rows)
      con.close()
    except Exception as e:
      pcat_logger.logger.warning(f"network stats flush failed, re-queueing: {e}")
      with self._pending_lock:
        for device, (rx, tx) in pending.items():
          pend = self._pending.setdefault(device, [0, 0])
          pend[0] += rx
          pend[1] += tx

  def cleanup_old(self):
    """Drop rows older than the retention window (run daily)."""
    cutoff = (datetime.now() - timedelta(days=self.RETENTION_DAYS)).strftime("%Y-%m-%d 00:00:00")
    connect_db(self.stats_db, ["delete from network_stats where created_at < '{0}'".format(cutoff)])

  def clear_net_device_bytes(self):
    with self._pending_lock:
      self._pending = {}  # drop unflushed deltas too, or they'd resurrect after the reset
    sql_command = "delete from network_stats"
    connect_db(self.stats_db, [sql_command, "VACUUM"])

  def log_net_device_bytes(self, device, remark = ""):
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
    # Counter reset detection: modem (RNDIS) interfaces like eth2 (FM350-GL)
    # reset their byte counters to 0 on every reconnect. When that happens the
    # new value is smaller than the previous one, so the diff is the new value
    # itself (traffic since the reset). Without this, the old high-water-mark is
    # never surpassed and the modem's usage is silently dropped forever.
    if rx_bytes >= prev_rx_bytes:
      rx_bytes_diff = rx_bytes - prev_rx_bytes
    else:
      rx_bytes_diff = rx_bytes  # counter reset -> count from 0
    if tx_bytes >= prev_tx_bytes:
      tx_bytes_diff = tx_bytes - prev_tx_bytes
    else:
      tx_bytes_diff = tx_bytes  # counter reset -> count from 0

    # print("     Debug: diff rx_bytes", rx_bytes_diff, "last_tx_bytes", tx_bytes_diff, "for device", device)
    # Always advance the baseline, even when no traffic is recorded, so a
    # counter reset is not re-counted on the next tick.
    self.last_rx_bytes[device] = rx_bytes
    self.last_tx_bytes[device] = tx_bytes
    if rx_bytes_diff > 0 or tx_bytes_diff > 0:
      # Accumulate in RAM; flush_pending() writes one batched transaction
      # per flush window instead of a flash commit per device per sample.
      with self._pending_lock:
        pend = self._pending.setdefault(device, [0, 0])
        pend[0] += rx_bytes_diff
        pend[1] += tx_bytes_diff

  # network_type: mobile
  # network_type: wired
  def today_bytes(self, network_type):
    self.flush_pending()  # make unwritten deltas visible to the SUM below
    now = datetime.now()
    time_string = now.strftime("%Y-%m-%d 00:00:00")
    if network_type == "mobile":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where (device='usb0' or device='wwan0' or device='eth2') and created_at >= '{0}'".format(time_string)
    elif network_type == "wired":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where device='{0}' and created_at >= '{1}'".format(WIRED_DEVICE, time_string)
    else:
      # Unknown category: sql_command would be unbound and the request
      # handler would crash with UnboundLocalError (bites the moment a new
      # usage category is added to the UI without extending this ladder).
      pcat_logger.logger.warning("unknown network_type '{}'".format(network_type))
      return 0
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
    self.flush_pending()  # make unwritten deltas visible to the SUM below
    today = date.today()
    start_day = today - timedelta(days=today.weekday())
    end_day = start_day + timedelta(days=6)
    start_ts = start_day.strftime("%Y-%m-%d 00:00:00")
    end_ts = end_day.strftime("%Y-%m-%d 23:59:59")
    if network_type == "mobile":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where (device='usb0' or device='wwan0' or device='eth2') and created_at >= '{0}' and created_at <= '{1}'".format(start_ts, end_ts)
    elif network_type == "wired":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where device='{0}' and created_at >= '{1}' and created_at <= '{2}'".format(WIRED_DEVICE, start_ts, end_ts)
    else:
      # Unknown category: sql_command would be unbound and the request
      # handler would crash with UnboundLocalError (bites the moment a new
      # usage category is added to the UI without extending this ladder).
      pcat_logger.logger.warning("unknown network_type '{}'".format(network_type))
      return 0
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
    self.flush_pending()  # make unwritten deltas visible to the SUM below
    start_day = month_date.replace(day=1)
    start_ts = start_day.strftime("%Y-%m-%d 00:00:00")

    next_month = month_date.replace(day=28) + timedelta(days=4)
    end_day = next_month - timedelta(days=next_month.day)
    end_ts = end_day.strftime("%Y-%m-%d 23:59:59")

    if network_type == "mobile":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where (device='usb0' or device='wwan0' or device='eth2') and created_at >= '{0}' and created_at <= '{1}'".format(start_ts, end_ts)
    elif network_type == "wired":
      sql_command = "select sum(rx_bytes),sum(tx_bytes) from network_stats where device='{0}' and created_at >= '{1}' and created_at <= '{2}'".format(WIRED_DEVICE, start_ts, end_ts)
    else:
      # Unknown category: sql_command would be unbound and the request
      # handler would crash with UnboundLocalError (bites the moment a new
      # usage category is added to the UI without extending this ladder).
      pcat_logger.logger.warning("unknown network_type '{}'".format(network_type))
      return 0
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



