"""
Captive-portal gateway control (OpenNDS).

The device can act as a captive gateway: clients on its WiFi/LAN must pass a
click-to-continue splash before reaching the internet. OpenNDS (the maintained
nodogsplash successor) does the firewall/redirect/session work; here we only
expose a simple enable/disable + status so the manager UI can toggle it.

OpenNDS is configured in UCI (config opennds) and managed by its init script.
The captive logic itself (interception, splash, sessions) is owned by OpenNDS.
"""

import json
import os
import subprocess
import threading
import time

from pcat_util.openwrt import uci_get, uci_set, uci_commit
from pcat_util import pcat_logger

_SECTION = "opennds.@opennds[0]"
_INIT = "/etc/init.d/opennds"

# Serializes enable/disable so overlapping toggles can't race each other's
# UCI writes and init-script calls (procd start/stop is not atomic).
_lock = threading.Lock()

# How long to wait for opennds to actually reach the requested running state
# before giving up and reporting the mismatch instead of a false "ok".
_CONVERGE_TIMEOUT = 20
_CONVERGE_POLL_INTERVAL = 1


def is_installed():
    """True when the OpenNDS package is present on this firmware."""
    return os.path.exists("/usr/bin/opennds") and os.path.exists(_INIT)


def is_running():
    """Liveness via ndsctl (authoritative): if the control socket answers, the
    daemon is up. pgrep is unreliable here because opennds runs under procd and
    forks a web-server child."""
    try:
        r = subprocess.run(["ndsctl", "status"], capture_output=True,
                           text=True, timeout=8)
        return r.returncode == 0 and "openNDS" in r.stdout
    except Exception:
        return False


def is_enabled():
    """User intent from UCI. The stock config ships with the option absent and
    the daemon running, so an absent value is treated as enabled."""
    if not is_installed():
        return False
    return uci_get("{}.enabled".format(_SECTION)) != "0"


def _client_count():
    try:
        out = subprocess.run(["ndsctl", "json"], capture_output=True,
                             text=True, timeout=8).stdout
        data = json.loads(out) if out.strip() else {}
        return int(data.get("client_list_length", 0))
    except Exception:
        return 0


def get_state():
    installed = is_installed()
    running = installed and is_running()
    return {
        "installed": installed,
        "enabled": installed and is_enabled(),
        "running": running,
        "clients": _client_count() if running else 0,
    }


def _init_action(action):
    """Run an opennds init-script action. Returns True on success (rc 0),
    False otherwise, so callers can surface a failed start/stop instead of
    silently reporting success."""
    try:
        r = subprocess.run([_INIT, action], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        if r.returncode != 0:
            pcat_logger.logger.warning(
                "captive: opennds %s exited %s: %s", action, r.returncode,
                r.stderr.decode("utf-8", "replace").strip())
            return False
        return True
    except Exception as e:
        pcat_logger.logger.warning("captive: opennds {} failed: {}".format(action, e))
        return False


def _ensure_portal_config():
    """Harden the OpenNDS config against a known accept-flow failure: the
    default gatewayfqdn (status.client) relies on a dnsmasq record whose
    install is racy at daemon startup ("Dnsmasq reload failed!" in logread).
    When it loses, clients cannot resolve the splash URL and can never accept
    the agreement. gatewayfqdn=disable makes OpenNDS build all portal URLs
    from the gateway IP, removing the DNS dependency entirely."""
    if uci_get("{}.gatewayfqdn".format(_SECTION)) != "disable":
        uci_set("{}.gatewayfqdn=disable".format(_SECTION))
        uci_commit("opennds")


def _wait_until_running(target):
    """Poll ndsctl until is_running() matches `target` or we time out. Returns
    True on convergence, False if opennds never reached the requested state."""
    deadline = time.monotonic() + _CONVERGE_TIMEOUT
    while time.monotonic() < deadline:
        if is_running() == target:
            return True
        time.sleep(_CONVERGE_POLL_INTERVAL)
    return is_running() == target


def set_enabled(enabled):
    """Enable/disable the captive portal and apply it immediately, blocking
    until opennds actually converges to the requested running state (or the
    convergence timeout elapses). Returns (state_dict, ok) where `ok` is False
    when OpenNDS isn't installed, or when the daemon failed to converge -
    callers should surface that as an error rather than a silent success."""
    if not is_installed():
        return None, False
    with _lock:
        # procd-managed service: use start/stop (not restart, which trips
        # procd's "service delete: Not found" path) and let procd own the
        # lifecycle.
        if enabled:
            _ensure_portal_config()
            uci_set("{}.enabled={}".format(_SECTION, "1"))
            uci_commit("opennds")
            # "enable" persists the procd autostart symlink; if it fails, the
            # portal may run now but won't survive a reboot, diverging from
            # the UCI 'enabled' option we just committed - treat that as a
            # failure too rather than only checking the immediate "start".
            enabled_ok = _init_action("enable")
            started = _init_action("start")
            converged = enabled_ok and started and _wait_until_running(True)
        else:
            stopped = _init_action("stop")
            uci_set("{}.enabled={}".format(_SECTION, "0"))
            uci_commit("opennds")
            disabled_ok = _init_action("disable")
            converged = stopped and disabled_ok and _wait_until_running(False)

        if converged:
            pcat_logger.logger.info("captive: portal %s", "enabled" if enabled else "disabled")
        else:
            pcat_logger.logger.warning(
                "captive: portal failed to reach %s state within %ss",
                "running" if enabled else "stopped", _CONVERGE_TIMEOUT)
        return get_state(), converged
