"""
Thin wrappers around OpenWrt ubus and uci.

Uses native python3-ubus for ubus calls (persistent socket connection).
Uses subprocess for uci calls (python3-uci module is broken on this platform).
Both ubus calls fall back to subprocess on failure.
"""

import json
import subprocess
import threading

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


def ubus_call(path, method, args=None):
    """
    Call a ubus method and return the parsed result dict.
    Returns {} on any error.
    """
    if args is None:
        args = {}

    if _ubus_mod is not None:
        with _ubus_lock:
            try:
                _ensure_ubus_connected()
                result = _ubus_mod.call(path, method, args)
                if isinstance(result, list) and len(result) > 0:
                    return result[0]
                return result if isinstance(result, dict) else {}
            except Exception as e:
                err_msg = str(e).lower()
                # "not found" means the object doesn't exist (e.g. hostapd interface not up yet)
                # No need to retry via subprocess — just return empty
                if "not found" in err_msg:
                    return {}
                global _ubus_connected
                _ubus_connected = False
                print(f"[openwrt] ubus native {path}.{method} failed ({e}), trying subprocess")
    # Subprocess fallback (also used when native call fails)
    try:
        cmd = ["ubus", "call", path, method]
        if args:
            cmd.append(json.dumps(args))
        output = subprocess.check_output(cmd, timeout=5, text=True)
        return json.loads(output) if output.strip() else {}
    except Exception:
        return {}


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
        return result.returncode == 0
    except Exception:
        return False


def uci_commit(config):
    """
    Commit a UCI config section (e.g. 'wireless').
    Returns True on success.
    """
    try:
        result = subprocess.run(
            ["uci", "commit", config],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5
        )
        return result.returncode == 0
    except Exception:
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
