"""
Thin wrappers around OpenWrt ubus and uci.

Uses native python3-ubus for ubus calls (persistent socket connection).
Uses subprocess for uci calls (python3-uci module is broken on this platform).
Both ubus calls fall back to subprocess on failure.
"""

import json
import subprocess
import threading
import time

# ─── ubus (native python3-ubus + subprocess fallback) ────────────────────────

_ubus_mod = None
_ubus_lock = threading.Lock()
_ubus_connected = False
_UBUS_SOCKET = "/var/run/ubus/ubus.sock"

try:
    import ubus as _ubus_mod_raw
    _ubus_mod = _ubus_mod_raw
    try:
        _ubus_mod.connect(_UBUS_SOCKET)
        _ubus_connected = True
        _test_result = _ubus_mod.call("system", "board", {})
        print(f"[openwrt] python3-ubus connected OK, test keys={list(_test_result[0].keys()) if isinstance(_test_result, list) and _test_result else 'N/A'}")
    except Exception as e:
        print(f"[openwrt] python3-ubus connect/test failed: {e}")
    print("[openwrt] Using native python3-ubus module")
except ImportError:
    print("[openwrt] python3-ubus not available, using subprocess")


def _ensure_ubus_connected():
    """Connect to ubus if not already connected."""
    global _ubus_connected
    if _ubus_mod is None:
        return
    if _ubus_connected:
        return
    try:
        _ubus_mod.connect(_UBUS_SOCKET)
        _ubus_connected = True
    except Exception as e:
        if "already connected" in str(e).lower():
            _ubus_connected = True
        else:
            print(f"[openwrt] ubus connect failed: {e}")


# The native ubus connection is a single shared socket, so calls are serialized
# under _ubus_lock. A ubus method can block (a busy netifd/hostapd), and the
# native call takes no timeout, so without a bound one stalled call would hold
# the lock and freeze every ubus caller in every thread. Cap how long a thread
# waits for the lock: past this it gives up the native path and uses the
# subprocess fallback (which has its own 5s timeout), so a single stuck call
# only sacrifices its own thread instead of the whole app.
_UBUS_LOCK_TIMEOUT = 2.0

# Internal sentinel: an infrastructure failure (ubus unreachable / timeout /
# lock stuck), as opposed to {} which is a valid "object absent / empty" answer.
_UBUS_FAIL = object()


def _ubus_call_core(path, method, args):
    """Native call with subprocess fallback. Returns the result dict, {} when the
    object/method is not found (a valid absent answer), or _UBUS_FAIL on an
    infrastructure failure so the public wrappers can choose how to report it."""
    if _ubus_mod is not None:
        got = _ubus_lock.acquire(timeout=_UBUS_LOCK_TIMEOUT)
        if got:
            try:
                _ensure_ubus_connected()
                result = _ubus_mod.call(path, method, args)
                if isinstance(result, list) and len(result) > 0:
                    return result[0]
                return result if isinstance(result, dict) else {}
            except Exception as e:
                err_msg = str(e).lower()
                # "not found" means the object doesn't exist (e.g. hostapd
                # interface not up yet) -- a valid empty answer, not a failure.
                if "not found" in err_msg:
                    return {}
                global _ubus_connected
                _ubus_connected = False
                print(f"[openwrt] ubus native {path}.{method} failed ({e}), trying subprocess")
            finally:
                _ubus_lock.release()
        else:
            # Another call is holding the lock too long (a stalled ubus method);
            # don't block behind it -- fall through to the subprocess path.
            print(f"[openwrt] ubus lock busy for {path}.{method}, using subprocess")
    # Subprocess fallback (also used when the native call fails or the lock is stuck)
    try:
        cmd = ["ubus", "call", path, method]
        if args:
            cmd.append(json.dumps(args))
        output = subprocess.check_output(cmd, timeout=5, text=True)
        return json.loads(output) if output.strip() else {}
    except subprocess.CalledProcessError as e:
        # The ubus CLI exit code is the ubus status: 3 method-not-found,
        # 4 not-found, 5 no-data are valid "absent" answers ({}); anything else
        # (timeout 7, connection-failed 10, ...) is an infrastructure failure.
        return {} if e.returncode in (3, 4, 5) else _UBUS_FAIL
    except Exception:
        return _UBUS_FAIL


def ubus_call(path, method, args=None):
    """
    Call a ubus method and return the parsed result dict.
    Returns {} on any error (back-compatible; most callers use `or {}`). Use
    ubus_call_strict() when you must distinguish an infrastructure failure from
    a genuinely empty/absent result.
    """
    result = _ubus_call_core(path, method, {} if args is None else args)
    return {} if result is _UBUS_FAIL else result


def ubus_call_strict(path, method, args=None):
    """
    Like ubus_call, but returns None on an infrastructure failure (ubus
    unreachable / timeout / lock stuck), distinct from {} for a not-found or
    empty result. Lets a caller avoid treating "couldn't ask" as a real answer
    -- e.g. the netifd reload fallback, and the WAN health probes that must not
    demote a live uplink just because ubus hiccupped.
    """
    result = _ubus_call_core(path, method, {} if args is None else args)
    return None if result is _UBUS_FAIL else result


def ubus_call_raw(path, method, args=None):
    """
    Call a ubus method and return raw JSON string (for backward compat).
    Returns "" on error.
    """
    result = ubus_call(path, method, args)
    if result:
        return json.dumps(result)
    return ""


# ─── uci (subprocess only) ───────────────────────────────────────────────────
# python3-uci is broken on this platform (raises empty exceptions).
# subprocess `uci` is fast enough for the infrequent UCI operations.

print("[openwrt] Using subprocess for uci")


def uci_get(key):
    """
    Get a UCI value by dotted key (e.g. 'wireless.radio0.disabled').
    Returns the value as a string, or "" on error.
    """
    try:
        result = subprocess.run(
            ["uci", "get", key],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""
    except Exception:
        return ""


# Short-lived snapshot of whole UCI configs ('uci show <config>' parsed into a
# dict). Status checks (dashboard, network interfaces) used to fork one
# 'uci get' per key, ~8+ forks per refresh on the ARM target; with this, all
# keys of a config cost one fork per TTL window. Writes invalidate the cache.
_uci_show_cache = {}
_uci_show_lock = threading.Lock()
# Must exceed the dashboard poll interval (4s) or every poll misses the cache
# and forks 'uci show'. Writes through uci_set/uci_commit invalidate, so only
# out-of-band edits (ssh, Luci) wait the full TTL to become visible.
UCI_SHOW_CACHE_TTL = 10  # seconds


def uci_show_dict(config, max_age=UCI_SHOW_CACHE_TTL):
    """
    Return all values of a UCI config as a {'config.section.option': 'value'}
    dict, cached for max_age seconds. Returns {} on error.
    """
    now = time.time()
    with _uci_show_lock:
        entry = _uci_show_cache.get(config)
        if entry is not None and now - entry['time'] < max_age:
            return entry['data']

    data = {}
    output = uci_show(config)
    for line in output.decode("utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
            value = value[1:-1]
        data[key] = value

    # Never cache a failed read: uci_show returns b"" on error, and caching
    # the resulting {} made the whole app see a blank config for a full TTL
    # after uci had already recovered (dashboard/wireless page went empty for
    # up to 10s on a transient fork failure).
    if output:
        with _uci_show_lock:
            _uci_show_cache[config] = {'data': data, 'time': now}
    return data


def invalidate_uci_cache():
    """Drop cached 'uci show' snapshots (called after uci set/commit)."""
    with _uci_show_lock:
        _uci_show_cache.clear()


def uci_set(key_value):
    """
    Set a UCI value (e.g. 'wireless.radio0.disabled=0').
    Returns True on success.
    """
    try:
        result = subprocess.run(
            ["uci", "set", key_value],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
        )
        invalidate_uci_cache()
        return result.returncode == 0
    except Exception:
        return False


def uci_add_list(key_value):
    """
    Append a value to a UCI list (e.g. 'firewall.@zone[1].network=wan_wifi').
    Returns True on success.
    """
    try:
        result = subprocess.run(
            ["uci", "add_list", key_value],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
        )
        invalidate_uci_cache()
        return result.returncode == 0
    except Exception:
        return False


def uci_delete(key):
    """
    Delete a UCI option or section (e.g. 'wireless.wifinet_sta.key').
    Returns True on success (False when the key didn't exist).
    """
    try:
        result = subprocess.run(
            ["uci", "delete", key],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
        )
        invalidate_uci_cache()
        return result.returncode == 0
    except Exception:
        return False


def uci_commit(config):
    """
    Commit a UCI config section (e.g. 'wireless').
    Returns True on success.

    Failures are LOGGED, never silent: a commit that dies quietly (subprocess
    timeout on a busy box, transient error) leaves the change staged in
    /tmp/.uci — everything keeps working (uci show and netifd both read the
    staging delta) until the next reboot wipes /tmp and the "committed"
    config evaporates. That exact mode cost a device its WiFi-upstream STA
    section across reboots. The timeout is generous for the same reason: the
    commits that matter most often land during a wifi-reload CPU spike.
    """
    try:
        result = subprocess.run(
            ["uci", "commit", config],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15
        )
        invalidate_uci_cache()
        if result.returncode != 0:
            from pcat_util import pcat_logger
            pcat_logger.logger.error(
                "uci commit {} failed: rc={} {}".format(
                    config, result.returncode,
                    (result.stderr or b"").decode("utf-8", "replace").strip()))
        return result.returncode == 0
    except Exception as e:
        try:
            from pcat_util import pcat_logger
            pcat_logger.logger.error("uci commit {} failed: {}".format(config, e))
        except Exception:
            pass
        return False


def uci_commit_verified(config, expect=(), attempts=2):
    """Commit `config` and confirm the values actually reached /etc/config/.

    uci_commit() reports a commit that fails *loudly*. The mode that costs a
    user their settings is the one that fails quietly: the change stays in the
    /tmp/.uci staging delta, which `uci show` and netifd both honour, so
    everything works for the rest of the session and evaporates at the next
    reboot -- "my WiFi name and password went back to the defaults after a
    restart". Reading the persistent file back is the only way to tell a real
    commit from that one.

    `expect` is the values that must be present, matched the way UCI writes
    them (`option ssid 'name'`). A value containing a quote is not checked
    rather than guessed at -- uci escapes those -- so verification can never
    fail over a name it simply cannot spell. When the file cannot be read at
    all the commit's own return code is trusted: refusing an otherwise good
    save because /etc/config is unreadable helps nobody.

    Returns True when the commit landed.
    """
    wanted = [v for v in expect if v and "'" not in v]
    committed = False
    for attempt in range(1, attempts + 1):
        committed = uci_commit(config)
        try:
            with open("/etc/config/{}".format(config)) as f:
                text = f.read()
        except OSError:
            return committed
        if committed and all("'{}'".format(v) in text for v in wanted):
            return True
        try:
            from pcat_util import pcat_logger
            pcat_logger.logger.error(
                "uci commit {} did not reach /etc/config/{} (attempt {})".format(
                    config, config, attempt))
        except Exception:
            pass
    return False


def uci_show(key):
    """
    Run 'uci show <key>' and return output as bytes (for backward compat).
    Returns b"" on error.
    """
    try:
        return subprocess.check_output(
            ["uci", "show", key],
            stderr=subprocess.PIPE, timeout=5
        )
    except Exception:
        return b""


def is_native_ubus():
    """Check if native ubus module is available."""
    return _ubus_mod is not None


def is_native_uci():
    """Check if native uci module is available."""
    return False


def debug_status():
    """Return debug info about native module status."""
    info = {
        "ubus_native": _ubus_mod is not None,
        "ubus_connected": _ubus_connected,
        "ubus_socket": _UBUS_SOCKET,
        "uci_native": False,
        "uci_mode": "subprocess",
    }
    # Test ubus native
    if _ubus_mod is not None:
        try:
            _ensure_ubus_connected()
            result = _ubus_mod.call("system", "board", {})
            info["ubus_test"] = "OK"
            info["ubus_hostname"] = result[0].get("hostname") if isinstance(result, list) and result else None
        except Exception as e:
            info["ubus_test"] = f"FAIL: {e}"
    # Test subprocess
    try:
        out = subprocess.check_output(["ubus", "call", "system", "board"], timeout=5, text=True)
        d = json.loads(out)
        info["subprocess_ubus_test"] = "OK"
        info["subprocess_hostname"] = d.get("hostname")
    except Exception as e:
        info["subprocess_ubus_test"] = f"FAIL: {e}"
    try:
        out = subprocess.run(["uci", "get", "system.@system[0].hostname"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, text=True)
        info["subprocess_uci_test"] = "OK" if out.returncode == 0 else f"FAIL: rc={out.returncode}"
        info["subprocess_uci_hostname"] = out.stdout.strip() if out.returncode == 0 else None
    except Exception as e:
        info["subprocess_uci_test"] = f"FAIL: {e}"
    return info
