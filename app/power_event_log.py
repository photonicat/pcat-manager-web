# -*- coding: utf-8 -*-
# Power Event Logger - Persistent storage for power-related events

import json
import os
import threading
import time
from datetime import datetime

EVENT_LOG_PATH = "/etc/pcat-power-events.json"
MAX_EVENTS = 200  # Keep last 200 events
_log_lock = threading.Lock()
_battery_snapshot_fn = None


def set_battery_snapshot_fn(fn):
    """Register a callback that returns {'voltage': V, 'current': mA, 'soc': %}"""
    global _battery_snapshot_fn
    _battery_snapshot_fn = fn


def _load_events():
    """Load events from disk."""
    try:
        if os.path.exists(EVENT_LOG_PATH):
            with open(EVENT_LOG_PATH, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except (IOError, json.JSONDecodeError):
        pass
    return []


def _save_events(events):
    """Save events to disk (must hold _log_lock)."""
    try:
        os.makedirs(os.path.dirname(EVENT_LOG_PATH), exist_ok=True)
        with open(EVENT_LOG_PATH, 'w') as f:
            json.dump(events[-MAX_EVENTS:], f, indent=2)
    except IOError:
        pass


def log_event(event_type, detail="", extra=None):
    """
    Log a power event.

    event_type: one of:
      "system_start", "system_shutdown",
      "idle_shutdown_triggered", "idle_countdown_started", "idle_countdown_reset",
      "low_battery_shutdown_triggered", "low_battery_warning",
      "car_mode_shutdown", "car_mode_power_on",
      "timer_shutdown", "timer_power_on",
      "force_power_mode_set",
      "power_mode_changed",
      "settings_changed",
      "power_dip" (voltage drop detected),
      "dc_plugged_in" (DC power connected),
      "dc_unplugged" (DC power disconnected)
    detail: human-readable description
    extra: optional dict with additional data (battery%, voltage, etc.)
    """
    now = datetime.now()

    # Auto-attach battery snapshot
    bat = None
    if _battery_snapshot_fn:
        try:
            bat = _battery_snapshot_fn()
        except Exception:
            pass

    with _log_lock:
        events = _load_events()
        last_id = events[-1]["id"] if events and "id" in events[-1] else 0
        entry = {
            "id": last_id + 1,
            "ts": int(time.time()),
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "type": event_type,
            "detail": detail,
        }
        if bat:
            entry["bat"] = bat
        if extra:
            entry["extra"] = extra
        events.append(entry)
        _save_events(events)


def get_events(limit=50, offset=0):
    """Get events in reverse chronological order."""
    with _log_lock:
        events = _load_events()
    # Return newest first
    events.reverse()
    if offset:
        events = events[offset:]
    if limit:
        events = events[:limit]
    return events


def get_event_count():
    """Get total number of events."""
    with _log_lock:
        return len(_load_events())


def clear_events():
    """Clear all events."""
    with _log_lock:
        _save_events([])
