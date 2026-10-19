"""Bluetooth adapter state and scanning for the /dev diagnostics page.

The photonicat2 carries an AICSemi AIC8800D80 combo radio whose Bluetooth
half sits on USB (a69c:8d81, interface class e0/01/01). Whether that half is
usable depends entirely on how the vendor driver was built, which is why this
module reports a *diagnosis* rather than a bare yes/no.

The vendor `aic_btusb` driver has two mutually exclusive personalities,
selected at compile time by CONFIG_BLUEDROID in aic_btusb.h:

    0 (BlueZ)     hci_register_dev() resolves to the kernel's, an hciN adapter
                  appears under /sys/class/bluetooth, and bluetoothctl works.
    1 (bluedroid) the driver compiles its OWN private hci_register_dev() stub
                  and exposes an Android-style char device instead. No adapter
                  ever appears, no matter what userspace does.

Firmware built in bluedroid mode cannot be recovered at runtime -- there is no
adapter to unblock, bring up, or restart a daemon against. Saying "scan
failed" there would be true and useless; the actionable fact is that the
kernel module is the wrong build. detect_bluedroid_build() is what separates
that case from a merely-disabled radio, so the page can say which it is.
"""

import os
import re
import subprocess

from pcat_util import pcat_logger

SYS_BLUETOOTH = "/sys/class/bluetooth"

# The BT half of the combo chip. Presence here means the hardware is fitted
# and enumerated, which is what makes a missing adapter a defect rather than
# a board without the option fitted.
AIC_USB_ID = "a69c:8d81"
AIC_BT_MODULE = "aic_btusb"

_SCAN_SECONDS = 10
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def _run(cmd, timeout=15):
    """Run a command, returning (rc, stdout+stderr). Never raises."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, ""
    except Exception as exc:
        pcat_logger.logger.debug("bluetooth: %s failed: %s", cmd[0], exc)
        return -1, ""


def _have(tool):
    return _run(["which", tool], timeout=5)[0] == 0


def adapters():
    """hciN names the kernel has registered with BlueZ. Empty is meaningful."""
    try:
        return sorted(n for n in os.listdir(SYS_BLUETOOTH) if n.startswith("hci"))
    except OSError:
        return []


def chip_present():
    """True when the AIC8800 Bluetooth USB function is enumerated.

    Read from lsusb, then from the driver's sysfs binding as a fallback: a
    board with lsusb absent still tells the truth through /sys/bus/usb/drivers.
    """
    rc, out = _run(["lsusb"], timeout=10)
    if rc == 0 and AIC_USB_ID in out.lower():
        return True
    return bool(_driver_bindings())


def _driver_bindings():
    """USB interfaces currently bound to the aic_btusb driver."""
    path = "/sys/bus/usb/drivers/%s" % AIC_BT_MODULE
    try:
        return sorted(n for n in os.listdir(path) if ":" in n)
    except OSError:
        return []


def _module_loaded(name):
    try:
        with open("/proc/modules", "r") as handle:
            return any(line.startswith(name + " ") for line in handle)
    except OSError:
        return False


def detect_bluedroid_build():
    """True when aic_btusb was compiled in bluedroid mode (no BlueZ adapter).

    The tell is the module's dependency list. A BlueZ-mode HCI driver must
    link against the `bluetooth` module to reach hci_register_dev(); a
    bluedroid build defines that symbol itself and therefore depends on
    nothing.

    That list comes from modinfo. /proc/modules looks like it would do, but
    its fourth column is used_by -- the modules depending on THIS one -- which
    is empty for a leaf driver either way; reading it as the dependency list
    reports a healthy BlueZ build as bluedroid.

    Returns None when aic_btusb is not loaded at all -- unknown, not false.
    """
    if not _module_loaded(AIC_BT_MODULE):
        return None
    rc, out = _run(["modinfo", AIC_BT_MODULE], timeout=15)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("depends:"):
            return "bluetooth" not in line.split(":", 1)[1]
    return None


def rfkill_state():
    """Soft/hard block state for every bluetooth rfkill switch."""
    rc, out = _run(["rfkill", "list", "bluetooth"], timeout=10)
    if rc != 0:
        return []
    entries, current = [], None
    for line in out.splitlines():
        head = re.match(r"^\s*(\d+):\s*(\S+):", line)
        if head:
            current = {"index": int(head.group(1)), "name": head.group(2),
                       "soft_blocked": False, "hard_blocked": False}
            entries.append(current)
        elif current is not None:
            if "Soft blocked" in line:
                current["soft_blocked"] = line.strip().endswith("yes")
            elif "Hard blocked" in line:
                current["hard_blocked"] = line.strip().endswith("yes")
    return entries


def daemon_running():
    return _run(["pgrep", "-x", "bluetoothd"], timeout=5)[0] == 0


def status():
    """Everything the page needs to explain the radio in one request."""
    found = adapters()
    blocks = rfkill_state()
    bluedroid = detect_bluedroid_build()

    state = {
        "adapters": found,
        "adapter_count": len(found),
        "chip_present": chip_present(),
        "driver_bound": _driver_bindings(),
        "module_loaded": _module_loaded(AIC_BT_MODULE),
        "bluedroid_build": bluedroid,
        "daemon_running": daemon_running(),
        "rfkill": blocks,
        "soft_blocked": any(b["soft_blocked"] for b in blocks),
        "hard_blocked": any(b["hard_blocked"] for b in blocks),
        "tools": {t: _have(t) for t in ("bluetoothctl", "hciconfig", "rfkill")},
    }
    state["usable"] = bool(found)
    state["diagnosis"], state["recoverable"] = _diagnose(state)
    return state


def _diagnose(state):
    """(human-readable cause, whether runtime recovery could possibly help).

    Ordered most-specific first: a bluedroid build explains an absent adapter
    completely, and reporting "radio is soft-blocked" underneath it would send
    someone chasing an rfkill that is not the problem.
    """
    if state["adapters"]:
        return "", False
    if not state["chip_present"] and not state["module_loaded"]:
        return "no Bluetooth hardware detected on this device", False
    if state["bluedroid_build"] is True:
        return ("aic_btusb is built in bluedroid mode (CONFIG_BLUEDROID=1), so "
                "it registers an Android char device instead of a BlueZ "
                "adapter -- no hciN can appear until the kernel module is "
                "rebuilt with CONFIG_BLUEDROID=0"), False
    if state["hard_blocked"]:
        return "Bluetooth is hard-blocked by rfkill (hardware switch)", False
    if state["soft_blocked"]:
        return "Bluetooth is soft-blocked by rfkill", True
    if not state["module_loaded"]:
        return "the %s kernel module is not loaded" % AIC_BT_MODULE, True
    return ("the driver is loaded but registered no HCI adapter with BlueZ"), True


def recover():
    """Best-effort attempt to make an adapter appear. Returns (ok, steps).

    Deliberately refuses to act when the diagnosis says runtime recovery
    cannot work: retrying rfkill against a bluedroid build produces a
    convincing sequence of successful commands and no adapter, which reads as
    a flaky radio instead of the build problem it is.
    """
    before = status()
    steps = []
    if before["adapters"]:
        return True, [{"step": "already up", "ok": True,
                       "detail": ", ".join(before["adapters"])}]
    if not before["recoverable"]:
        return False, [{"step": "recovery skipped", "ok": False,
                        "detail": before["diagnosis"]}]

    def record(step, rc, detail=""):
        steps.append({"step": step, "ok": rc == 0, "detail": detail})

    if before["soft_blocked"]:
        rc, out = _run(["rfkill", "unblock", "bluetooth"], timeout=10)
        record("rfkill unblock bluetooth", rc, out.strip()[:200])

    if not before["module_loaded"]:
        rc, out = _run(["modprobe", AIC_BT_MODULE], timeout=30)
        record("modprobe %s" % AIC_BT_MODULE, rc, out.strip()[:200])

    for name in adapters():
        rc, out = _run(["hciconfig", name, "up"], timeout=15)
        record("hciconfig %s up" % name, rc, out.strip()[:200])

    if not adapters() and os.path.exists("/etc/init.d/bluetoothd"):
        rc, out = _run(["/etc/init.d/bluetoothd", "restart"], timeout=30)
        record("restart bluetoothd", rc, out.strip()[:200])

    now = adapters()
    steps.append({"step": "adapter present", "ok": bool(now),
                  "detail": ", ".join(now) or "still none"})
    return bool(now), steps


def scan(seconds=_SCAN_SECONDS):
    """Discover nearby devices. Returns a dict shaped like the wifi scan API.

    status is "ok" only when a scan actually ran. An adapter that is missing
    is reported as its own status with the diagnosis attached, so the caller
    never has to guess whether an empty list means "nothing nearby" or
    "nothing was ever scanned" -- the same absence-is-not-a-negative rule the
    HIL harness applies.
    """
    state = status()
    if not state["adapters"]:
        return {"status": "unavailable", "reason": state["diagnosis"],
                "devices": [], "adapter": "", "state": state}
    if not state["tools"].get("bluetoothctl"):
        return {"status": "unavailable", "reason": "bluetoothctl is not installed",
                "devices": [], "adapter": state["adapters"][0], "state": state}

    seconds = max(3, min(int(seconds or _SCAN_SECONDS), 30))
    # --timeout makes bluetoothctl exit by itself; without it the command is
    # interactive and would hang until the subprocess timeout fires.
    rc, out = _run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
                   timeout=seconds + 20)
    devices = _parse_devices()
    if not devices and rc != 0:
        return {"status": "error", "reason": out.strip()[:300] or "scan failed",
                "devices": [], "adapter": state["adapters"][0], "state": state}
    return {"status": "ok", "devices": devices, "adapter": state["adapters"][0],
            "scanned_seconds": seconds, "state": state}


def _parse_devices():
    """`bluetoothctl devices` output -> [{address, name}].

    A device whose name has not resolved yet is echoed by bluetoothctl with
    its MAC in the name column; that is reported as an empty name rather than
    a fake one, so the UI can show "(unnamed)" instead of a duplicated MAC.
    """
    rc, out = _run(["bluetoothctl", "devices"], timeout=15)
    if rc != 0:
        return []
    devices = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or parts[0] != "Device":
            continue
        address = parts[1]
        if not _MAC_RE.match(address):
            continue
        name = parts[2].strip() if len(parts) > 2 else ""
        if name.replace("-", ":").upper() == address.upper():
            name = ""
        devices.append({"address": address, "name": name})
    return devices
