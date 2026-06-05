# -*- coding: utf-8 -*-
# Battery History - Records SOC, voltage, current every 5 minutes for up to 30 days

import sqlite3
import os
import time
import threading
from datetime import datetime

BATTERY_DB_PATH = "/etc/pcat-battery-history.db"
RECORD_INTERVAL = 300  # 5 minutes in seconds
MAX_AGE_DAYS = 30
_lock = threading.Lock()

SQL_CREATE = '''
CREATE TABLE IF NOT EXISTS battery_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  soc INTEGER,
  voltage REAL,
  current REAL,
  charging INTEGER DEFAULT 0,
  temperature REAL
);
CREATE INDEX IF NOT EXISTS idx_ts ON battery_history(ts);
'''

SQL_MIGRATE = [
    "ALTER TABLE battery_history ADD COLUMN temperature REAL",
]


def _get_conn():
    conn = sqlite3.connect(BATTERY_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the battery history database."""
    try:
        conn = _get_conn()
        for stmt in SQL_CREATE.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        # Run migrations for existing databases
        for sql in SQL_MIGRATE:
            try:
                conn.execute(sql)
            except Exception:
                pass  # Column likely already exists
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            with open("/tmp/battery_history_error.log", "a") as f:
                f.write(f"init_db error: {e}\n")
        except Exception:
            pass


def record(soc, voltage_v, current_ma, charging=False, temperature=None):
    """Record a battery snapshot. Call every 5 minutes."""
    ts = int(time.time())
    with _lock:
        try:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO battery_history (ts, soc, voltage, current, charging, temperature) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, soc, voltage_v, current_ma, 1 if charging else 0, temperature)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            import traceback
            try:
                with open("/tmp/battery_history_error.log", "a") as f:
                    f.write(f"{ts}: record() error: {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass


def cleanup():
    """Remove records older than MAX_AGE_DAYS."""
    cutoff = int(time.time()) - (MAX_AGE_DAYS * 86400)
    with _lock:
        try:
            conn = _get_conn()
            conn.execute("DELETE FROM battery_history WHERE ts < ?", (cutoff,))
            conn.execute("VACUUM")
            conn.commit()
            conn.close()
        except Exception:
            pass


def get_history(hours=24):
    """Get battery history for the last N hours."""
    cutoff = int(time.time()) - (hours * 3600)
    with _lock:
        try:
            conn = _get_conn()
            rows = conn.execute(
                "SELECT ts, soc, voltage, current, charging, temperature FROM battery_history WHERE ts >= ? ORDER BY ts ASC",
                (cutoff,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []


def get_record_count():
    """Get total number of records."""
    with _lock:
        try:
            conn = _get_conn()
            row = conn.execute("SELECT COUNT(*) FROM battery_history").fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0


# Initialize on import
init_db()
