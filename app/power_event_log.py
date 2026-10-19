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


# Event types that mean "the machine stopped running here". Time between one of
# these and the following system_start is powered-off time and must not be
# counted as on-battery time.
_STOP_EVENTS = (
    "system_shutdown",
    "idle_shutdown_triggered",
    "low_battery_shutdown_triggered",
    "car_mode_shutdown",
    "timer_shutdown",
)


def get_on_battery_session(charger_connected=None, now=None):
    """Awake-on-battery time for the current session.

    Returns {"seconds": int, "active": bool, "since": int|None} or None when the
    log holds no dc_unplugged event to measure from.

    "Session" is the stretch since DC was last unplugged: a dc_plugged_in event
    ends it, so the counter restarts from zero on the next unplug. While DC is
    connected the last completed run is reported with active=False.

    Powered-off gaps are excluded. Runs on battery frequently end with the
    machine dying or shutting down and later booting back up on the same
    battery; only the awake spans between system_start and the next stop event
    are summed, so time spent off never inflates the total.

    charger_connected: current DC state. Passed in rather than read here so this
    module stays free of hardware imports; None falls back to whichever plug
    event came last in the log.
    """
    if now is None:
        now = int(time.time())

    with _log_lock:
        events = _load_events()

    # Oldest-first, and only entries carrying a usable timestamp.
    events = [e for e in events if isinstance(e.get("ts"), int)]
    events.sort(key=lambda e: e["ts"])
    if not events:
        return None

    # Session start = the last unplug that has not been closed by a later plug-in.
    start_idx = None
    for i in range(len(events) - 1, -1, -1):
        etype = events[i].get("type")
        if etype == "dc_unplugged":
            start_idx = i
            break
        if etype == "dc_plugged_in":
            # Everything before this belongs to an earlier session; keep looking
            # backwards for the unplug that opened the run this plug-in closed.
            for j in range(i - 1, -1, -1):
                if events[j].get("type") == "dc_unplugged":
                    start_idx = j
                    break
            break
    if start_idx is None:
        return None

    session = events[start_idx:]
    since = session[0]["ts"]

    if charger_connected is None:
        charger_connected = any(e.get("type") == "dc_plugged_in" for e in session)

    # Walk the session summing awake spans. The device is awake at the unplug
    # (it logged the event), so the first span opens there.
    total = 0
    awake_from = since
    end_ts = now
    for e in session[1:]:
        etype = e.get("type")
        ts = e["ts"]
        if etype == "dc_plugged_in":
            # Session ends here — DC is back, the counter stops and will reset.
            end_ts = ts
            break
        if etype in _STOP_EVENTS:
            if awake_from is not None:
                total += max(0, ts - awake_from)
                awake_from = None
        elif etype == "system_start":
            # Back up after a shutdown or an unlogged death (battery ran flat,
            # power cut): resume counting from the boot, skipping the gap.
            if awake_from is None:
                awake_from = ts
    if awake_from is not None:
        total += max(0, end_ts - awake_from)

    return {
        "seconds": int(total),
        "active": not charger_connected,
        "since": since,
    }


def clear_events():
    """Clear all events."""
    with _log_lock:
        _save_events([])
