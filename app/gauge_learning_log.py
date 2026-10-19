# -*- coding: utf-8 -*-
"""Gauge-learning bench log.

The MCU auto-learns per-unit fuel-gauge parameters: full-charge capacity, the
raw SoC at which this particular machine actually dies (the "display empty
point"), the display remap built from it, and the current/charge trim
coefficients. Those numbers matter when calibrating a unit on the bench and
mean nothing to an end user, so they are not on any page -- they land here.

Written to /tmp (tmpfs): this is diagnostic output, not state worth spending
flash write cycles on, and it is expected to vanish on reboot. Size-capped so
a long-running device cannot fill tmpfs.
"""

import os
import threading
import time

LOG_PATH = "/tmp/guage_learning.log"
MAX_BYTES = 256 * 1024          # roll at 256K -- ~1500 lines of history
_log_lock = threading.Lock()

# Skip writing a line when nothing the MCU learned has changed. The gauge
# re-learns on the scale of a discharge cycle, so at the caller's cadence an
# unconditional write would be thousands of identical lines a day.
_last_signature = None


def _fmt(v, digits=None):
    """Render a value for the log, '-' when the firmware did not send it."""
    if v is None:
        return "-"
    if digits is not None:
        try:
            return f"{float(v):.{digits}f}"
        except (TypeError, ValueError):
            return "-"
    return str(v)


def _roll_if_needed():
    """Truncate to the newest half once the file passes MAX_BYTES.

    Keeping the tail (rather than deleting outright) preserves the most recent
    calibration history, which is the part anyone reading this file wants.
    """
    try:
        if os.path.getsize(LOG_PATH) < MAX_BYTES:
            return
        with open(LOG_PATH, "rb") as f:
            f.seek(-(MAX_BYTES // 2), os.SEEK_END)
            f.readline()            # drop the partial line at the seek point
            tail = f.read()
        with open(LOG_PATH, "wb") as f:
            f.write(b"# --- rolled, older entries dropped ---\n")
            f.write(tail)
    except (OSError, ValueError):
        pass


def record(client, force=False):
    """Append one line of gauge-learning state.

    client is the socket client carrying the MCU's latest battery fields.
    Returns True when a line was written. Unchanged state is skipped unless
    force=True. Never raises: diagnostics must not take down the caller.
    """
    global _last_signature
    try:
        # Nothing to log on firmware that does not report the learned values.
        if getattr(client, "battery_soc_raw_x10", None) is None and \
           getattr(client, "battery_full_capacity", None) is None:
            return False

        def scaled(attr, div, digits):
            raw = getattr(client, attr, None)
            if raw is None:
                return None
            try:
                return round(int(raw) / div, digits)
            except (TypeError, ValueError):
                return None

        full_mah = getattr(client, "battery_full_capacity", None)
        nominal_mah = getattr(client, "battery_nominal_capacity", None)
        learned = bool(getattr(client, "battery_capacity_learned", False))
        active = bool(getattr(client, "battery_learning_active", False))
        empty_pct = getattr(client, "battery_soc_empty_pct", None)
        cycles = getattr(client, "battery_cycle_count", None)
        trim_b = getattr(client, "battery_cur_trim_b_ma", None)
        soc_raw = scaled("battery_soc_raw_x10", 10.0, 1)
        disp_a = scaled("battery_soc_disp_a_x1000", 1000.0, 3)
        disp_b = scaled("battery_soc_disp_b_x100", 100.0, 2)
        chg_gain = scaled("battery_chg_gain_x1000", 1000.0, 3)
        trim_a = scaled("battery_cur_trim_a_x1000", 1000.0, 3)

        state = "learned" if learned else ("measuring" if active else "unlearned")

        # soc_raw moves continuously and is context, not a learned value, so it
        # is deliberately excluded from the change signature -- including it
        # would defeat the dedup entirely.
        signature = (full_mah, nominal_mah, state, empty_pct, disp_a, disp_b,
                     chg_gain, trim_a, trim_b, cycles)
        if not force and signature == _last_signature:
            return False

        line = (
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"state={state} "
            f"full={_fmt(full_mah)}mAh nominal={_fmt(nominal_mah)}mAh "
            f"empty_point={_fmt(empty_pct)}% soc_raw={_fmt(soc_raw)}% "
            f"disp=a{_fmt(disp_a)}/b{_fmt(disp_b)} "
            f"chg_gain={_fmt(chg_gain)} "
            f"cur_trim=a{_fmt(trim_a)}/b{_fmt(trim_b)}mA "
            f"cycles={_fmt(cycles)}\n"
        )

        with _log_lock:
            _roll_if_needed()
            new_file = not os.path.exists(LOG_PATH)
            with open(LOG_PATH, "a") as f:
                if new_file:
                    f.write("# gauge learning log -- MCU self-calibration state\n")
                    f.write("# empty_point: raw SoC where this unit dies; "
                            "disp: displayed = a*raw + b; "
                            "cur_trim/chg_gain: meter calibration\n")
                f.write(line)
            _last_signature = signature
        return True
    except Exception:
        return False
