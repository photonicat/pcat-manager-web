#@Author    : qichunren

import sqlite3
import os
import re
from pcat_util import pcat_logger

# /sys/class/net/wlan0/statistics/rx_bytes
# /sys/class/net/wlan0/statistics/tx_bytes

# python3 sqlite3 doc: https://docs.python.org/3/library/sqlite3.html

# Wait up to this long for a concurrently-held write lock instead of failing
# with "database is locked". The SMS/phone clients open a fresh connection per
# op from several threads, so contention is expected and transient.
_DB_TIMEOUT = 15.0


def init_db(db_path, create_sql):
  file_exists = os.path.exists(db_path)
  con = None
  try:
    con = sqlite3.connect(db_path, timeout=_DB_TIMEOUT)
  except sqlite3.OperationalError:
    if file_exists:
      pcat_logger.logger.warning("sqlite3.OperationalError, failed to read " + db_path)
      os.remove(db_path)
      con = sqlite3.connect(db_path, timeout=_DB_TIMEOUT)
      # The connection now points at a brand-new empty file, so fall through
      # to schema creation below. Leaving this True hit the early "exists,
      # no need to create" return and produced a permanently table-less DB
      # that no later boot repaired (init_db saw the file and no-op'ed).
      file_exists = False
    else:
      pcat_logger.logger.warning("sqlite3.OperationalError, failed to create " + db_path)
  finally:
    if con is None:
      pcat_logger.logger.warning("Failed to connect to db {}".format(db_path))
      return False

  if file_exists:
    pcat_logger.logger.info("Db {} exist, not need create new one.".format(db_path))
    con.close()
    return True
  
  pcat_logger.logger.info("Db {} not exist, try to create one.".format(db_path))
  lines = create_sql.strip().split(";")
  s_commands = []
  for line in lines:
    command = re.sub(r'\n+\s*', '', line)
    command = re.sub(r'\s+', ' ', command)
    if len(command) > 0:
      s_commands.append(command)
  for sqlite_command in s_commands:
    pcat_logger.logger.info("Exec sql: {}".format(sqlite_command))
    con.cursor().execute(sqlite_command)
  if len(s_commands) > 0:
    con.commit()
  con.close()  
  return True

def connect_db(db_path, commands = []):
  file_exists = os.path.exists(db_path)
  con = None
  try:
    con = sqlite3.connect(db_path, timeout=_DB_TIMEOUT)
  except sqlite3.OperationalError:
    if file_exists:
      pcat_logger.logger.warning("sqlite3.OperationalError, failed to read " + db_path)
    else:
      pcat_logger.logger.warning("sqlite3.OperationalError, failed to create " + db_path)
    con = None
  finally:
    if con is None:
      return False
    
  if len(commands) > 0:
    for sqlite_command in commands:
      # debug, not info: this runs for every periodic stats insert and would
      # add thousands of log lines per day at the default INFO level
      pcat_logger.logger.debug("Exec sql: {}".format(sqlite_command))
      con.cursor().execute(sqlite_command)
      con.commit()
  con.close()
  return True

def connect_db_param(db_path, commands_with_params = []):
  """Execute parameterized SQL commands to prevent SQL injection"""
  file_exists = os.path.exists(db_path)
  con = None
  try:
    con = sqlite3.connect(db_path, timeout=_DB_TIMEOUT)
  except sqlite3.OperationalError:
    if file_exists:
      pcat_logger.logger.warning("sqlite3.OperationalError, failed to read " + db_path)
    else:
      pcat_logger.logger.warning("sqlite3.OperationalError, failed to create " + db_path)
    con = None
  finally:
    if con is None:
      return False
    
  if len(commands_with_params) > 0:
    for command_data in commands_with_params:
      if isinstance(command_data, tuple):
        sqlite_command, params = command_data
      else:
        sqlite_command = command_data
        params = []
      # debug, not info: hot path, and params may carry user content (SMS text)
      pcat_logger.logger.debug("Exec parameterized sql: {} with params: {}".format(sqlite_command, params))
      con.cursor().execute(sqlite_command, params)
      con.commit()
  con.close()
  return True

def get_rows(db_path, select_cmd):
  # Read path: NEVER delete the DB on error. A transient "database is locked"
  # (OperationalError) previously triggered os.remove(), destroying all SMS /
  # call history. Wait for the lock via timeout; on any error return [] and
  # leave the file intact.
  con = None
  try:
    con = sqlite3.connect(db_path, timeout=_DB_TIMEOUT)
    con.row_factory = sqlite3.Row
    rows = list(con.cursor().execute(select_cmd))
    return rows
  except sqlite3.Error as e:
    pcat_logger.logger.warning("get_rows failed on %s: %s", db_path, e)
    return []
  finally:
    if con is not None:
      con.close()

def get_rows_param(db_path, select_cmd, params=[]):
  """Execute parameterized SELECT queries to prevent SQL injection"""
  # Read path: never delete the DB on error (see get_rows).
  con = None
  try:
    con = sqlite3.connect(db_path, timeout=_DB_TIMEOUT)
    con.row_factory = sqlite3.Row
    rows = list(con.cursor().execute(select_cmd, params))
    return rows
  except sqlite3.Error as e:
    pcat_logger.logger.warning("get_rows_param failed on %s: %s", db_path, e)
    return []
  finally:
    if con is not None:
      con.close()