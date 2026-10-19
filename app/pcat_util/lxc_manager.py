"""
LXC container manager: setup automation, container lifecycle, migration.

Backs the HIDDEN /lxc page (reachable by URL only, no navbar entry). The
storage-side peers are nas_manager/nas_jobs; this module follows the same
idiom: module functions behind an RLock where needed, subprocess helpers that
never raise, and background work through nas_jobs so the UI has one job shape.

Why everything goes through ubus: the firmware ships liblxc + rpcd-mod-lxc
(the `lxc` ubus object: list/info/start/stop/shutdown/freeze/destroy/rename)
but NOT the lxc-start/lxc-stop/lxc-ls CLIs — only lxc-attach, lxc-console and
lxc-create exist. So lifecycle is `ubus call lxc ...` (also exactly what
luci-app-lxc uses).

Creating a container does NOT use lxc-create. Its lxc-download template
fetches with `wget -q ... >/dev/null 2>&1`, which hides both the byte count
and the failure: on these links the image CDN routinely stalls mid-file, and
because that wget neither resumes nor checks Content-Length, a truncated
tarball is treated as success. The result was a container with an empty
rootfs whose every start died as a bare "Invalid argument". create_container
therefore resolves the image off the index itself, downloads it with resume
plus a size check, verifies the archive (xz -t) before unpacking, writes the
config from /etc/lxc/default.conf, and removes the half-made directory on any
failure — reporting bytes/percent into the job the whole way.

Setup automation ("auto do the stuff a fresh device is missing"), each piece
detected by preflight() and applied by its fix_*():

- Bridge: containers reference lxcbr0 (per /etc/lxc/default.conf) but nothing
  on a stock image creates it, so every container start dies in netns setup.
  The fix writes the bridge as UCI network state — a `device` section with
  bridge_empty (a portless bridge is DOWN otherwise), a static-IP `interface`,
  a dnsmasq DHCP pool so containers get addresses, and membership in the lan
  firewall zone for forwarding — then reloads netifd/dnsmasq/firewall. UCI
  ownership means netifd recreates it on every boot; no init script of ours.
  A container start that finds its bridge missing also runs this fix itself
  (_ensure_start_env), because rpcd reports the resulting netns failure as a
  bare "Invalid argument" and users on a fresh device hit exactly that:
  create works, start fails, and the toast explains nothing.

- cgroup v2: procd natively mounts cgroup2 on /sys/fs/cgroup at boot, but the
  image ships cgroupfs-mount (START=01) which UNMOUNTS that and installs the
  v1 controller forest — and systemd >= 256 (Debian 13, Ubuntu 24.04+)
  refuses to boot as container PID 1 without the unified hierarchy. Nothing
  else on the image needs v1 (verified: openclash only greps /proc/1/cgroup
  for self-detection).

  The durable fix is an installed init script, pcat-cgroup2 at START=02: it
  runs straight after the shim and swaps the forest back to cgroup2 before
  anything has populated it. Two measurements shaped that. A live swap after
  boot does not work — 110s in, with no sub-cgroups at all, umount of the cpu
  controller already returns EBUSY — so the correction has to happen in that
  early window. And `cgroupfs-mount disable` alone does not survive a firmware
  upgrade: sysupgrade -c carries the overlay across as a tar of files, so an
  added file comes back but a deleted rc.d symlink does not, which is why a
  device fixed before an upgrade came up broken after one. An added script
  survives. startup() installs it at boot so this needs no user action; the
  Setup tab's Fix does the same thing plus a best-effort live swap.

- Storage: lxcpath comes from /etc/lxc/lxc.conf. The move is a job: rsync to
  the new location, rewrite lxc.rootfs.path in every container config (they
  embed absolute paths), point lxc.conf at it, restart rpcd (it caches the
  path), and only then delete the source. Refused while anything is running.

- Autostart: an installed /etc/init.d/pcat-lxc (START=99) starts containers
  flagged in /etc/config/pcat_lxc, our own config file.

Migration is Proxmox-style offline push over SSH with a dedicated ed25519
key (/root/.ssh/pcat_lxc_ed25519, never the interactive key): stop if
running, stream `tar | ssh tar -x` with byte-counted progress (no remote
rsync dependency — tar is the one thing every target has), rewrite the
rootfs path for the remote lxcpath, optionally start remotely (ubus first,
lxc-start fallback), and for "move" destroy the source only after the
transfer pipeline exits clean. Key install onto the target drives `ssh`
through a pty once with the user's password (there is no sshpass on the
image); after that everything is BatchMode.
"""

import ipaddress
import json
import os
import pty
import re
import select
import shlex
import socket
import struct
import subprocess
import threading
import time

from pcat_util import nas_jobs
from pcat_util import pcat_logger
from pcat_util.openwrt import (
    uci_get,
    uci_set,
    uci_add_list,
    uci_commit,
    uci_delete,
    uci_show_dict,
)

_lock = threading.RLock()

LXC_CONF = "/etc/lxc/lxc.conf"
DEFAULT_CONF = "/etc/lxc/default.conf"
DEFAULT_LXCPATH = "/srv/lxc"
BRIDGE = "lxcbr0"
BRIDGE_ADDR = "10.0.3.1"
BRIDGE_NETMASK = "255.255.255.0"
UCI_IFACE = "lxc"                      # network.lxc / dhcp.lxc section name
CGROUPFS_INIT = "/etc/init.d/cgroupfs-mount"
CGROUP2_INIT = "/etc/init.d/pcat-cgroup2"
AUTOSTART_INIT = "/etc/init.d/pcat-lxc"
AUTOSTART_UCI = "pcat_lxc"             # /etc/config/pcat_lxc
SSH_DIR = "/root/.ssh"
SSH_KEY = os.path.join(SSH_DIR, "pcat_lxc_ed25519")

# Container names: liblxc allows more, but everything here interpolates the
# name into shell-adjacent contexts (remote tar/sed), so keep it strict.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,127}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# Quick-pick catalog for the Create tab (images.linuxcontainers.org names).
# Free-text dist/release is also accepted — this is UI sugar, not a gate.
TEMPLATE_CATALOG = [
    {"dist": "debian", "release": "trixie", "label": "Debian 13"},
    {"dist": "debian", "release": "bookworm", "label": "Debian 12"},
    {"dist": "ubuntu", "release": "noble", "label": "Ubuntu 24.04"},
    {"dist": "ubuntu", "release": "jammy", "label": "Ubuntu 22.04"},
    {"dist": "alpine", "release": "3.22", "label": "Alpine 3.22"},
    {"dist": "alpine", "release": "3.21", "label": "Alpine 3.21"},
    {"dist": "archlinux", "release": "current", "label": "Arch Linux"},
    {"dist": "rockylinux", "release": "9", "label": "Rocky Linux 9"},
]

_size_cache = {}   # name -> {"time": ts, "bytes": n}
_SIZE_TTL = 60.0


def _run(argv, timeout=15, stdin_data=None):
    """Subprocess helper in the nas_manager idiom: never raises, returns
    (returncode, stdout+stderr-merged-stdout, stderr) with rc=-1 on
    infrastructure failure."""
    try:
        proc = subprocess.run(
            argv, input=stdin_data, capture_output=True, text=True,
            timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        pcat_logger.logger.warning(
            "lxc_manager: {} failed: {}".format(argv[0], e))
        return -1, "", str(e)


def _which(binary):
    for prefix in ("/usr/sbin", "/usr/bin", "/sbin", "/bin"):
        p = os.path.join(prefix, binary)
        if os.path.exists(p):
            return p
    return None


# ── lxc.conf / lxcpath ───────────────────────────────────────────────────────

def _read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def parse_lxcpath(conf_text):
    """lxc.lxcpath from an lxc.conf body; default when absent/blank."""
    for line in conf_text.splitlines():
        line = line.strip()
        if line.startswith("lxc.lxcpath"):
            _, _, val = line.partition("=")
            val = val.strip()
            if val:
                return val
    return DEFAULT_LXCPATH


def lxcpath():
    return parse_lxcpath(_read_file(LXC_CONF))


def rewrite_lxcpath(conf_text, new_path):
    """Return conf_text with lxc.lxcpath set to new_path (replaced in place,
    or appended if the key never existed). Pure function for tests."""
    lines = conf_text.splitlines()
    out, done = [], False
    for line in lines:
        if line.strip().startswith("lxc.lxcpath") and not done:
            out.append("lxc.lxcpath = {}".format(new_path))
            done = True
        else:
            out.append(line)
    if not done:
        out.append("lxc.lxcpath = {}".format(new_path))
    return "\n".join(out) + "\n"


def rewrite_rootfs_paths(config_text, old_base, new_base):
    """Rewrite absolute container-dir references in a container config when
    its base directory moves (rootfs.path, mount entries...). Plain textual
    replace of the old base prefix — configs embed 'dir:/srv/lxc/<n>/rootfs'
    and hook paths the same way. Pure function for tests."""
    return config_text.replace(old_base.rstrip("/") + "/",
                               new_base.rstrip("/") + "/")


# ── ubus lifecycle ───────────────────────────────────────────────────────────

def _ubus(method, params=None, timeout=30):
    """`ubus call lxc <method>` -> (ok, parsed-json-or-{}). rpcd's lxc plugin
    returns empty output for verbs and JSON for list/info."""
    argv = ["ubus", "-t", str(timeout), "call", "lxc", method,
            json.dumps(params or {})]
    rc, out, err = _run(argv, timeout=timeout + 5)
    if rc != 0:
        return False, {"error": (err or out).strip()}
    if not out.strip():
        return True, {}
    try:
        return True, json.loads(out)
    except ValueError:
        return True, {"raw": out.strip()}


def ubus_available():
    ok, _ = _ubus("list", timeout=5)
    return ok


def container_states():
    """{name: 'RUNNING'|'STOPPED'|'FROZEN'} from ubus."""
    ok, data = _ubus("list", timeout=10)
    if not ok or not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _container_config_path(name):
    return os.path.join(lxcpath(), name, "config")


def _uci_container_sections():
    """{section: opts} for /etc/config/pcat_lxc 'container' sections that
    carry a name. Shared by autostart, vmid and rename bookkeeping."""
    cfg = uci_show_dict(AUTOSTART_UCI, max_age=0)
    sections = {}
    for key, val in cfg.items():
        parts = key.split(".")
        if len(parts) < 2:
            continue
        sec = parts[1]
        if len(parts) == 2 and val == "container":
            sections.setdefault(sec, {})
        elif len(parts) == 3:
            sections.setdefault(sec, {})[parts[2]] = val
    return {s: o for s, o in sections.items() if "name" in o}


def _autostart_map():
    """{name: bool} from /etc/config/pcat_lxc."""
    return {o["name"]: o.get("autostart", "0") == "1"
            for o in _uci_container_sections().values()}


def _ensure_uci_file():
    path = "/etc/config/" + AUTOSTART_UCI
    if not os.path.exists(path):
        try:
            open(path, "a").close()
        except OSError:
            return False
    return True


VMID_START = 100


def vmid_in_use(vmid):
    """True when a container already holds this id."""
    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        return False
    return vmid in {int(o["vmid"]) for o in _uci_container_sections().values()
                    if o.get("vmid", "").isdigit()}


def next_free_vmid():
    """The id ensure_vmids() would hand out next — what the create form shows
    as its default."""
    used = {int(o["vmid"]) for o in _uci_container_sections().values()
            if o.get("vmid", "").isdigit()}
    vmid = VMID_START
    while vmid in used:
        vmid += 1
    return vmid


def ensure_vmids(names, prefer=None):
    """Proxmox-style numeric CT ids: every container gets a stable vmid
    (>= 100), stored in its pcat_lxc section. Existing ids are never
    changed (they follow the section through renames); new containers get
    the lowest free id, assigned in name order for determinism. Returns
    {name: vmid}.

    `prefer` is {name: vmid} for containers whose id the user picked at
    create time; a preferred id is honoured only if it is free and >=
    VMID_START, otherwise that container falls back to the lowest free id."""
    if not _ensure_uci_file():
        return {}
    secs = _uci_container_sections()
    by_name = {o["name"]: (sec, o) for sec, o in secs.items()}
    used = {int(o["vmid"]) for o in secs.values()
            if o.get("vmid", "").isdigit()}
    prefer = prefer or {}
    out, changed = {}, False
    for name in sorted(names):
        if name in by_name and by_name[name][1].get("vmid", "").isdigit():
            out[name] = int(by_name[name][1]["vmid"])
            continue
        if name in by_name:
            sec = by_name[name][0]
        else:
            rc, new_sec, _ = _run(["uci", "add", AUTOSTART_UCI,
                                   "container"])
            if rc != 0:
                continue
            sec = new_sec.strip()
            uci_set("{}.{}.name={}".format(AUTOSTART_UCI, sec, name))
        vmid = VMID_START
        want = prefer.get(name)
        try:
            want = int(want)
        except (TypeError, ValueError):
            want = None
        if want is not None and want >= VMID_START and want not in used:
            vmid = want
        while vmid in used:
            vmid += 1
        used.add(vmid)
        uci_set("{}.{}.vmid={}".format(AUTOSTART_UCI, sec, vmid))
        out[name] = vmid
        changed = True
    if changed:
        uci_commit(AUTOSTART_UCI)
    return out


def _rootfs_size(name, path):
    now = time.time()
    ent = _size_cache.get(name)
    if ent and now - ent["time"] < _SIZE_TTL:
        return ent["bytes"]
    rc, out, _ = _run(["du", "-sk", path], timeout=60)
    size = None
    if rc == 0 and out.split():
        try:
            size = int(out.split()[0]) * 1024
        except ValueError:
            size = None
    _size_cache[name] = {"time": now, "bytes": size}
    return size


def list_containers(with_sizes=False):
    """Rows for the UI: state + ips from ubus, dist/release from the config
    comment lxc-download leaves, autostart from our uci file."""
    states = container_states()
    base = lxcpath()
    auto = _autostart_map()
    # Containers on disk that ubus doesn't list (e.g. broken config) still
    # show up, flagged.
    on_disk = []
    try:
        # Dot-directories are ours (image pool, download scratch), never
        # containers: NAME_RE demands a leading alphanumeric, so anything
        # starting with '.' could not be acted on even if it were listed —
        # it would just show up as a BROKEN row whose every button fails
        # name validation.
        on_disk = [d for d in sorted(os.listdir(base))
                   if os.path.isdir(os.path.join(base, d))
                   and NAME_RE.match(d)]
    except OSError:
        pass
    names = sorted(set(states) | set(on_disk))
    vmids = ensure_vmids(names)
    rows = []
    for name in names:
        state = states.get(name, "BROKEN")
        row = {
            "name": name,
            "vmid": vmids.get(name),
            "state": state,
            "ips": [],
            "autostart": auto.get(name, False),
            "dist": "",
            "release": "",
            "size_bytes": None,
            "storage": os.path.dirname(
                os.path.realpath(os.path.join(base, name))),
        }
        if state == "RUNNING":
            ok, info = _ubus("info", {"name": name}, timeout=10)
            if ok:
                row["ips"] = [ip for ip in info.get("ips", []) if ip]
        cfg = _read_file(os.path.join(base, name, "config"))
        m = re.search(r"--dist\s+(\S+)\s+--release\s+(\S+)", cfg)
        if m:
            row["dist"], row["release"] = m.group(1), m.group(2)
        row["network"] = parse_network_mode(cfg)
        if with_sizes:
            row["size_bytes"] = _rootfs_size(
                name, os.path.realpath(os.path.join(base, name)))
        else:
            # The 5s poll must not blank out a size the sizes=1 load already
            # paid du for — reuse the cache at any age.
            ent = _size_cache.get(name)
            row["size_bytes"] = ent["bytes"] if ent else None
        rows.append(row)
    # Proxmox orders by numeric id.
    rows.sort(key=lambda r: (r["vmid"] is None, r["vmid"], r["name"]))
    return rows


def _link_up(link):
    return os.path.isdir("/sys/class/net/" + link)


def _bridge_link(name):
    """The bridge the container's first NIC joins, '' when the config has no
    net section."""
    mode = parse_network_mode(_read_file(_container_config_path(name)))
    if mode == "nat":
        return BRIDGE
    if mode == "bridge":
        return LAN_BRIDGE
    if mode.startswith("custom:"):
        return mode[7:]
    return ""


def _ensure_start_env(name):
    """Pre-start self-heal. Every fresh container references lxcbr0, but a
    stock image has no bridge until fix_bridge() has run, so the first start
    dies in netns setup and all the user sees is rpcd's bare "Invalid
    argument". The intent to run containers could not be clearer than a
    Start click, so bring our own bridges up right here (fix_bridge for
    lxcbr0, a netifd reload for UCI-owned custom ones) instead of sending
    the user to the Setup tab. Returns an error dict when the bridge still
    doesn't exist afterwards, None when the start can proceed."""
    link = _bridge_link(name)
    if not link or _link_up(link):
        return None
    if link == BRIDGE:
        fix_bridge()
    elif any(o.get("name") == link
             for o in _uci_typed_sections("network", "device").values()):
        _run(["/etc/init.d/network", "reload"], timeout=30)
        for _ in range(10):
            if _link_up(link):
                break
            time.sleep(0.5)
    if _link_up(link):
        return None
    return {"code": "bridge_missing"}


def _diagnose_start_failure(name, ubus_err):
    """rpcd's lxc plugin reports every start failure as the same "Invalid
    argument"; turn the two common environmental causes into codes the toast
    renders as something actionable, and keep the raw ubus error only for
    the genuinely unknown remainder."""
    if cgroup_mode() == "legacy":
        # Installing the guard makes the reboot we're about to ask for
        # sufficient (it swaps the hierarchy at START=02 on the way up).
        install_cgroup2_guard()
        return {"code": "cgroup_legacy"}
    link = _bridge_link(name)
    if link and not _link_up(link):
        return {"code": "bridge_missing"}
    return {"code": "ubus_error", "detail": ubus_err}


def container_action(action, name, extra=None):
    """start/shutdown/stop/freeze/unfreeze/destroy/rename via ubus. Returns
    {"code": "ok"} or {"code": ..., "detail": ...}."""
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    extra = extra or {}
    if action == "rename":
        newname = extra.get("newname", "")
        if not NAME_RE.match(newname):
            return {"code": "bad_name"}
        ok, data = _ubus("rename", {"name": name, "newname": newname})
        if ok:
            _rename_autostart(name, newname)
    elif action == "shutdown":
        # Graceful; rpcd blocks until down or timeout.
        ok, data = _ubus("shutdown",
                         {"name": name, "timeout": 30}, timeout=40)
    elif action == "destroy":
        if container_states().get(name) == "RUNNING":
            return {"code": "running"}
        link = os.path.join(lxcpath(), name)
        if os.path.islink(link):
            # Container lives on another storage volume behind a symlink;
            # liblxc's destroy would trip over the link, so remove the real
            # tree and the link ourselves.
            real = os.path.realpath(link)
            ok = True
            rc, _, err = _run(["rm", "-rf", real], timeout=300)
            if rc != 0:
                ok, data = False, {"error": err.strip()}
            else:
                try:
                    os.unlink(link)
                except OSError as e:
                    ok, data = False, {"error": str(e)}
        else:
            ok, data = _ubus("destroy", {"name": name})
        if ok:
            set_autostart(name, False, prune=True)
            _size_cache.pop(name, None)
    elif action in ("start", "stop", "freeze", "unfreeze"):
        if action == "start":
            blocked = _ensure_start_env(name)
            if blocked:
                return blocked
        ok, data = _ubus(action, {"name": name}, timeout=40)
    else:
        return {"code": "bad_action"}
    if not ok:
        if action == "start":
            return _diagnose_start_failure(name, data.get("error", ""))
        return {"code": "ubus_error", "detail": data.get("error", "")}
    if action == "start":
        # ubus start is fire-and-forget; a config error surfaces as an
        # immediate exit. Catch it here so the UI gets a real error instead
        # of a row that flicks back to STOPPED. A systemd >= 256 guest on a
        # not-yet-rebooted (still cgroup v1) host exits exactly this way.
        time.sleep(1.5)
        if container_states().get(name) != "RUNNING":
            if cgroup_mode() == "legacy":
                install_cgroup2_guard()
                return {"code": "cgroup_legacy"}
            # No detail: the toast translates the bare code.
            return {"code": "start_failed"}
    return {"code": "ok"}


# ── per-container network mode (Proxmox-style NAT vs bridged) ───────────────

LAN_BRIDGE = "br-lan"
# nat: our lxcbr0 island (10.0.3.x, DHCP + masquerade through the lan zone).
# bridge: the container joins br-lan directly and is a first-class LAN
# machine (address from the router's own LAN DHCP).
NET_MODES = {"nat": BRIDGE, "bridge": LAN_BRIDGE}


def parse_network_mode(config_text):
    """'nat' / 'bridge' / 'custom:<link>' / '' (no net section) from a
    container config body. Pure function for tests."""
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("lxc.net.0.link"):
            _, _, val = stripped.partition("=")
            link = val.strip()
            if link == BRIDGE:
                return "nat"
            if link == LAN_BRIDGE:
                return "bridge"
            return "custom:" + link if link else ""
    return ""


def rewrite_network_mode(config_text, mode):
    """Return config_text with lxc.net.0.link pointed at the mode's bridge —
    "nat"/"bridge", or "link:<bridge>" for a custom one (line replaced in
    place, or a minimal veth block appended if the config has no net
    section). Pure function for tests."""
    link = mode[5:] if mode.startswith("link:") else NET_MODES[mode]
    lines = config_text.splitlines()
    out, done = [], False
    for line in lines:
        if line.strip().startswith("lxc.net.0.link") and not done:
            out.append("lxc.net.0.link = {}".format(link))
            done = True
        else:
            out.append(line)
    if not done:
        out.extend(["", "# Network configuration",
                    "lxc.net.0.type = veth",
                    "lxc.net.0.link = {}".format(link),
                    "lxc.net.0.flags = up"])
    return "\n".join(out) + "\n"


def set_network(name, mode):
    """Point the container at lxcbr0 (nat), br-lan (bridge), or a custom
    bridge ("link:<bridge>"). Takes effect at the next container start; safe
    to write while running."""
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    if mode in NET_MODES:
        pass
    elif mode.startswith("link:"):
        link = mode[5:]
        if link not in {n["bridge"] for n in list_networks()}:
            return {"code": "bad_mode"}
    else:
        return {"code": "bad_mode"}
    cfg_path = _container_config_path(name)
    body = _read_file(cfg_path)
    if not body:
        return {"code": "not_found"}
    try:
        with open(cfg_path, "w") as f:
            f.write(rewrite_network_mode(body, mode))
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    return {"code": "ok"}


# ── container networks: custom bridges (Setup tab "Networks" card) ───────────

NET_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,9}$")
NETDEV_PREFIX = "lxcnet_"      # UCI network.<sec>=device sections we own
NETIF_PREFIX = "lxcn_"         # interface/dhcp sections for NAT-mode bridges
PORT_RE = re.compile(r"^(eth|lan|wan|sfp)[0-9]*$")


def _uci_typed_sections(cfg, sectype):
    """{section: opts} of every section of `sectype` in a UCI config."""
    data = uci_show_dict(cfg, max_age=0)
    secs = {}
    for key, val in data.items():
        parts = key.split(".")
        if len(parts) == 2 and val == sectype:
            secs.setdefault(parts[1], {})
    for key, val in data.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[1] in secs:
            secs[parts[1]][parts[2]] = val
    return secs


def physical_ports():
    """Ethernet ports a custom bridge could enslave, annotated with their
    current role (bridge membership via the master symlink, and any UCI
    interface bound straight to the device) so the UI can warn instead of
    this code guessing intent."""
    out = []
    base = "/sys/class/net"
    ifaces = _uci_typed_sections("network", "interface")
    iface_by_dev = {}
    for sec, opts in ifaces.items():
        iface_by_dev.setdefault(opts.get("device", ""), []).append(sec)
    try:
        devs = sorted(os.listdir(base))
    except OSError:
        return out
    for d in devs:
        if not PORT_RE.match(d):
            continue
        if os.path.isdir(os.path.join(base, d, "bridge")):
            continue                      # a bridge itself, not a port
        master = ""
        mlink = os.path.join(base, d, "master")
        if os.path.islink(mlink):
            master = os.path.basename(os.readlink(mlink))
        out.append({
            "name": d,
            "master": master,
            "ifaces": sorted(iface_by_dev.get(d, [])),
            "up": _read_file(os.path.join(base, d, "operstate")).strip()
                  == "up",
        })
    return out


def list_networks():
    """Bridges a container can join: the two built-ins plus every custom
    lxcnet_* bridge, with enough detail for the Setup card and the
    per-container Network select."""
    net = uci_show_dict("network", max_age=0)
    out = [
        {"name": "nat", "bridge": BRIDGE, "builtin": True, "mode": "nat",
         "port": "", "addr": BRIDGE_ADDR,
         "up": os.path.isdir("/sys/class/net/" + BRIDGE)},
        {"name": "lan", "bridge": LAN_BRIDGE, "builtin": True, "mode": "lan",
         "port": "", "addr": "",
         "up": os.path.isdir("/sys/class/net/" + LAN_BRIDGE)},
    ]
    for sec, opts in _uci_typed_sections("network", "device").items():
        if not sec.startswith(NETDEV_PREFIX):
            continue
        name = sec[len(NETDEV_PREFIX):]
        bridge = opts.get("name", "")
        ifsec = NETIF_PREFIX + name
        addr = net.get("network.{}.ipaddr".format(ifsec), "")
        out.append({
            "name": name, "bridge": bridge, "builtin": False,
            "mode": "nat-island" if addr else "l2",
            "port": opts.get("ports", ""),
            "moved": opts.get("moved_ifaces", ""),
            "addr": addr,
            "up": os.path.isdir("/sys/class/net/" + bridge),
        })
    return out


def _nets_in_container_configs():
    """{bridge_link: [container, ...]} across every container config."""
    used = {}
    base = lxcpath()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return used
    for n in names:
        cfg = _read_file(os.path.join(base, n, "config"))
        for line in cfg.splitlines():
            stripped = line.strip()
            if stripped.startswith("lxc.net.") and ".link" in stripped:
                link = stripped.partition("=")[2].strip()
                if link:
                    used.setdefault(link, []).append(n)
    return used


def create_network(name, port=None, addressing="l2", subnet=""):
    """Create a container bridge as UCI-owned state (netifd recreates it on
    every boot, mirroring fix_bridge).

    port: optional physical port to enslave. If UCI interfaces ride that
    device directly (e.g. wan/wan6 on the WAN port), they are re-pointed at
    the new bridge — the standard OpenWrt move that lets the router keep its
    uplink while containers join the same L2. The originals are recorded on
    our device section so delete_network can restore them.

    addressing "nat": the bridge also gets a static router IP + dnsmasq pool
    + lan zone membership (an island like lxcbr0, but a separate subnet).
    addressing "l2": no local addressing — containers take DHCP/RA from
    whatever network the uplink port belongs to."""
    if not NET_NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    bridge = "br-" + name
    sec = NETDEV_PREFIX + name
    if bridge in (BRIDGE, LAN_BRIDGE) \
            or os.path.exists("/sys/class/net/" + bridge) \
            or sec in _uci_typed_sections("network", "device"):
        return {"code": "exists"}
    ports = physical_ports()
    moved = []
    if port:
        match = next((p for p in ports if p["name"] == port), None)
        if match is None:
            return {"code": "bad_port"}
        if match["master"]:
            return {"code": "port_in_use", "detail": match["master"]}
        moved = match["ifaces"]
    ipaddr = netmask = ""
    if addressing == "nat":
        try:
            iface = ipaddress.IPv4Interface(subnet or "")
        except ValueError as e:
            return {"code": "bad_subnet", "detail": str(e)}
        ipaddr = str(iface.ip)
        netmask = str(iface.network.netmask)
    elif addressing != "l2":
        return {"code": "bad_addressing"}

    uci_set("network.{}=device".format(sec))
    uci_set("network.{}.name={}".format(sec, bridge))
    uci_set("network.{}.type=bridge".format(sec))
    if port:
        uci_add_list("network.{}.ports={}".format(sec, port))
        for i in moved:
            uci_set("network.{}.device={}".format(i, bridge))
        if moved:
            uci_set("network.{}.moved_ifaces={}".format(sec, " ".join(moved)))
            uci_set("network.{}.orig_device={}".format(sec, port))
    else:
        uci_set("network.{}.bridge_empty=1".format(sec))
    uci_commit("network")

    if addressing == "nat":
        ifsec = NETIF_PREFIX + name
        uci_set("network.{}=interface".format(ifsec))
        uci_set("network.{}.device={}".format(ifsec, bridge))
        uci_set("network.{}.proto=static".format(ifsec))
        uci_set("network.{}.ipaddr={}".format(ifsec, ipaddr))
        uci_set("network.{}.netmask={}".format(ifsec, netmask))
        uci_commit("network")
        uci_set("dhcp.{}=dhcp".format(ifsec))
        uci_set("dhcp.{}.interface={}".format(ifsec, ifsec))
        uci_set("dhcp.{}.start=100".format(ifsec))
        uci_set("dhcp.{}.limit=150".format(ifsec))
        uci_set("dhcp.{}.leasetime=12h".format(ifsec))
        uci_commit("dhcp")
        idx = _lan_zone_index()
        if idx is not None:
            current = uci_get(
                "firewall.@zone[{}].network".format(idx)).split()
            if ifsec not in current:
                uci_add_list(
                    "firewall.@zone[{}].network={}".format(idx, ifsec))
                uci_commit("firewall")

    _run(["/etc/init.d/network", "reload"], timeout=30)
    if addressing == "nat":
        _run(["/etc/init.d/dnsmasq", "reload"], timeout=20)
        _run(["/etc/init.d/firewall", "reload"], timeout=30)
    for _ in range(10):
        if os.path.isdir("/sys/class/net/" + bridge):
            return {"code": "ok"}
        time.sleep(0.5)
    return {"code": "bridge_not_up",
            "detail": "UCI written but {} did not appear".format(bridge)}


def delete_network(name):
    """Remove a custom bridge, restoring any interfaces that were re-pointed
    at it back to the physical port. Refused while a container config still
    references the bridge."""
    if not NET_NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    sec = NETDEV_PREFIX + name
    devs = _uci_typed_sections("network", "device")
    if sec not in devs:
        return {"code": "not_found"}
    bridge = devs[sec].get("name", "br-" + name)
    used = _nets_in_container_configs().get(bridge, [])
    if used:
        return {"code": "in_use", "detail": ", ".join(used)}

    orig = devs[sec].get("orig_device", "")
    for i in devs[sec].get("moved_ifaces", "").split():
        if orig:
            uci_set("network.{}.device={}".format(i, orig))
    uci_delete("network." + sec)
    ifsec = NETIF_PREFIX + name
    if ifsec in _uci_typed_sections("network", "interface"):
        uci_delete("network." + ifsec)
        uci_delete("dhcp." + ifsec)
        idx = _lan_zone_index()
        if idx is not None:
            _run(["uci", "del_list",
                  "firewall.@zone[{}].network={}".format(idx, ifsec)])
            _run(["uci", "commit", "firewall"])
    uci_commit("network")
    uci_commit("dhcp")
    _run(["/etc/init.d/network", "reload"], timeout=30)
    _run(["/etc/init.d/dnsmasq", "reload"], timeout=20)
    _run(["/etc/init.d/firewall", "reload"], timeout=30)
    return {"code": "ok"}


# ── per-container address config (static v4/v6 vs DHCP/SLAAC) ───────────────

def parse_ip_config(config_text):
    """Address-method view of a config: {'v4_mode','v4_addr','v4_gw',
    'v6_mode','v6_addr','v6_gw'}. A family is 'manual' when the config
    carries an lxc.net.0.*.address line; otherwise addressing is left to the
    guest ('dhcp' for v4, 'auto' = SLAAC/DHCPv6 for v6). Pure function for
    tests."""
    out = {"v4_mode": "dhcp", "v4_addr": "", "v4_gw": "",
           "v6_mode": "auto", "v6_addr": "", "v6_gw": ""}
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("lxc.net.0.ipv4.address"):
            out["v4_addr"] = stripped.partition("=")[2].strip()
            out["v4_mode"] = "manual"
        elif stripped.startswith("lxc.net.0.ipv4.gateway"):
            out["v4_gw"] = stripped.partition("=")[2].strip()
        elif stripped.startswith("lxc.net.0.ipv6.address"):
            out["v6_addr"] = stripped.partition("=")[2].strip()
            out["v6_mode"] = "manual"
        elif stripped.startswith("lxc.net.0.ipv6.gateway"):
            out["v6_gw"] = stripped.partition("=")[2].strip()
    return out


def rewrite_ip_config(config_text, ipc):
    """Return config_text with the lxc.net.0 address/gateway lines matching
    `ipc` (shape of parse_ip_config): dropped for dhcp/auto, written for
    manual. New lines land right after lxc.net.0.flags when the config has a
    net section, so the block reads as one unit. Pure function for tests."""
    drop = ("lxc.net.0.ipv4.address", "lxc.net.0.ipv4.gateway",
            "lxc.net.0.ipv6.address", "lxc.net.0.ipv6.gateway")
    lines = [l for l in config_text.splitlines()
             if not l.strip().startswith(drop)]
    add = []
    if ipc.get("v4_mode") == "manual" and ipc.get("v4_addr"):
        add.append("lxc.net.0.ipv4.address = {}".format(ipc["v4_addr"]))
        if ipc.get("v4_gw"):
            add.append("lxc.net.0.ipv4.gateway = {}".format(ipc["v4_gw"]))
    if ipc.get("v6_mode") == "manual" and ipc.get("v6_addr"):
        add.append("lxc.net.0.ipv6.address = {}".format(ipc["v6_addr"]))
        if ipc.get("v6_gw"):
            add.append("lxc.net.0.ipv6.gateway = {}".format(ipc["v6_gw"]))
    if add:
        out, placed = [], False
        for line in lines:
            out.append(line)
            if not placed and line.strip().startswith("lxc.net.0.flags"):
                out.extend(add)
                placed = True
        if not placed:
            out.extend([""] + add)
        lines = out
    return "\n".join(lines) + "\n"


def set_ip_config(name, ipc):
    """Write the address method. Static addresses are applied by liblxc on
    the veth at container start (the guest's own DHCP client, if any, can
    still override them from inside); takes effect at the next start."""
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    clean = {
        "v4_mode": "manual" if ipc.get("v4_mode") == "manual" else "dhcp",
        "v6_mode": "manual" if ipc.get("v6_mode") == "manual" else "auto",
        "v4_addr": (ipc.get("v4_addr") or "").strip(),
        "v4_gw": (ipc.get("v4_gw") or "").strip(),
        "v6_addr": (ipc.get("v6_addr") or "").strip(),
        "v6_gw": (ipc.get("v6_gw") or "").strip(),
    }
    try:
        if clean["v4_mode"] == "manual":
            if "/" not in clean["v4_addr"]:
                return {"code": "bad_address",
                        "detail": "IPv4 needs CIDR form (10.0.3.50/24)"}
            ipaddress.IPv4Interface(clean["v4_addr"])
            if clean["v4_gw"]:
                ipaddress.IPv4Address(clean["v4_gw"])
        if clean["v6_mode"] == "manual":
            if "/" not in clean["v6_addr"]:
                return {"code": "bad_address",
                        "detail": "IPv6 needs CIDR form (fd00::50/64)"}
            ipaddress.IPv6Interface(clean["v6_addr"])
            if clean["v6_gw"]:
                ipaddress.IPv6Address(clean["v6_gw"])
    except ValueError as e:
        return {"code": "bad_address", "detail": str(e)}
    cfg_path = _container_config_path(name)
    body = _read_file(cfg_path)
    if not body:
        return {"code": "not_found"}
    try:
        with open(cfg_path, "w") as f:
            f.write(rewrite_ip_config(body, clean))
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    return {"code": "ok"}


# ── live stats / limits / config editor (Proxmox-style panels) ───────────────

_cpu_samples = {}   # name -> (wall_ts, usage_usec) for cpu% deltas


def _cgroup_dir(name):
    """liblxc puts a running container's payload cgroup here on a unified
    host."""
    d = "/sys/fs/cgroup/lxc.payload.{}".format(name)
    return d if os.path.isdir(d) else None


def _read_int(path):
    val = _read_file(path).strip()
    try:
        return int(val)
    except ValueError:
        return None


def parse_config_limits(config_text):
    """{'memory_mb': int|0, 'cpu_cores': float|0} from lxc.cgroup2 lines
    (0 = unlimited). Pure function for tests."""
    out = {"memory_mb": 0, "cpu_cores": 0.0}
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("lxc.cgroup2.memory.max"):
            _, _, val = stripped.partition("=")
            val = val.strip()
            if val.isdigit():
                out["memory_mb"] = int(val) // (1024 * 1024)
        elif stripped.startswith("lxc.cgroup2.cpu.max"):
            _, _, val = stripped.partition("=")
            parts = val.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                out["cpu_cores"] = round(int(parts[0]) / int(parts[1]), 2)
    return out


def _set_config_line(config_text, key, value):
    """Replace/append/remove (value=None) a `key = value` line. Pure."""
    lines = config_text.splitlines()
    out, done = [], False
    for line in lines:
        if line.strip().startswith(key):
            if value is not None and not done:
                out.append("{} = {}".format(key, value))
                done = True
            # value None (or duplicate): drop the line
        else:
            out.append(line)
    if value is not None and not done:
        out.append("{} = {}".format(key, value))
    return "\n".join(out) + "\n"


def rewrite_limits(config_text, memory_mb, cpu_cores):
    """Persist limits into the config (0/None = unlimited → line removed).
    cpu is expressed as the cgroup2 quota/period pair. Pure for tests."""
    mem_val = str(int(memory_mb) * 1024 * 1024) if memory_mb else None
    cpu_val = "{} 100000".format(int(float(cpu_cores) * 100000)) \
        if cpu_cores else None
    out = _set_config_line(config_text, "lxc.cgroup2.memory.max", mem_val)
    out = _set_config_line(out, "lxc.cgroup2.cpu.max", cpu_val)
    return out


def set_limits(name, memory_mb, cpu_cores):
    """Persist to config + apply live through the payload cgroup when
    running (same split Proxmox does: config for next boot, cgroup now)."""
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    try:
        memory_mb = max(0, int(memory_mb or 0))
        cpu_cores = max(0.0, float(cpu_cores or 0))
    except (TypeError, ValueError):
        return {"code": "bad_value"}
    cfg_path = _container_config_path(name)
    body = _read_file(cfg_path)
    if not body:
        return {"code": "not_found"}
    try:
        with open(cfg_path, "w") as f:
            f.write(rewrite_limits(body, memory_mb, cpu_cores))
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    cg = _cgroup_dir(name)
    if cg:
        try:
            with open(os.path.join(cg, "memory.max"), "w") as f:
                f.write(str(memory_mb * 1024 * 1024) if memory_mb else "max")
            with open(os.path.join(cg, "cpu.max"), "w") as f:
                f.write("{} 100000".format(int(cpu_cores * 100000))
                        if cpu_cores else "max")
        except OSError as e:
            return {"code": "ok", "live": False, "detail": str(e)}
    return {"code": "ok", "live": bool(cg)}


def _gateways_for_pid(pid):
    """(v4_gw, v6_gw) default gateways as seen from a container, read via
    /proc/<pid>/net/* which reflects the container's OWN netns — no attach
    needed. Empty strings when a family has no default route."""
    gw4 = ""
    for line in _read_file("/proc/{}/net/route".format(pid)).splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "00000000" \
                and parts[2] != "00000000":
            try:
                gw4 = socket.inet_ntoa(struct.pack("<I", int(parts[2], 16)))
            except (ValueError, struct.error):
                pass
            break
    gw6 = ""
    for line in _read_file("/proc/{}/net/ipv6_route".format(pid)).splitlines():
        parts = line.split()
        # dst dstlen src srclen nexthop metric refcnt use flags dev
        if len(parts) >= 9 and parts[0] == "0" * 32 and parts[1] == "00" \
                and parts[4] != "0" * 32:
            try:
                gw6 = socket.inet_ntop(socket.AF_INET6,
                                       bytes.fromhex(parts[4]))
            except (ValueError, OSError):
                pass
            break
    return gw4, gw6


def container_stats(name):
    """One poll for Summary/Resources: state, uptime, cpu%, memory, pids,
    plus static network facts (mode, MAC, container DNS)."""
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    cfg = _read_file(_container_config_path(name))
    if not cfg:
        return {"code": "not_found"}
    state = container_states().get(name, "BROKEN")
    hwaddr = ""
    m = re.search(r"lxc\.net\.0\.hwaddr\s*=\s*(\S+)", cfg)
    if m:
        hwaddr = m.group(1)
    dns = []
    resolv = _read_file(os.path.join(lxcpath(), name,
                                     "rootfs", "etc", "resolv.conf"))
    for line in resolv.splitlines()[:20]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            dns.append(parts[1])
    real = os.path.realpath(os.path.join(lxcpath(), name))
    size_ent = _size_cache.get(name)
    out = {
        "code": "ok",
        "state": state,
        "network": parse_network_mode(cfg),
        "hwaddr": hwaddr,
        "dns": dns[:5],
        "limits": parse_config_limits(cfg),
        "ip_config": parse_ip_config(cfg),
        "rootfs_path": real,
        "storage": os.path.dirname(real),
        "size_bytes": size_ent["bytes"] if size_ent else None,
        "gateway4": "", "gateway6": "",
        "host_mem_bytes": None,
        "host_cpus": os.cpu_count() or 1,
        "uptime": None, "ips": [],
        "cpu_pct": None, "mem_bytes": None, "mem_max_bytes": None,
        "pids": None,
    }
    for line in _read_file("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            out["host_mem_bytes"] = int(line.split()[1]) * 1024
            break
    if state != "RUNNING":
        return out
    ok, info = _ubus("info", {"name": name}, timeout=10)
    if ok:
        out["ips"] = [ip for ip in info.get("ips", []) if ip]
        pid = info.get("pid")
        if pid:
            out["gateway4"], out["gateway6"] = _gateways_for_pid(pid)
            try:
                with open("/proc/{}/stat".format(pid)) as f:
                    starttime = int(f.read().split(")")[-1].split()[19])
                host_uptime = float(_read_file("/proc/uptime").split()[0])
                hz = os.sysconf("SC_CLK_TCK") or 100
                out["uptime"] = int(host_uptime - starttime / hz)
            except (OSError, ValueError, IndexError):
                pass
    cg = _cgroup_dir(name)
    if cg:
        out["mem_bytes"] = _read_int(os.path.join(cg, "memory.current"))
        mem_max = _read_file(os.path.join(cg, "memory.max")).strip()
        out["mem_max_bytes"] = int(mem_max) if mem_max.isdigit() else None
        out["pids"] = _read_int(os.path.join(cg, "pids.current"))
        for line in _read_file(os.path.join(cg, "cpu.stat")).splitlines():
            if line.startswith("usage_usec"):
                usage = int(line.split()[1])
                now = time.time()
                prev = _cpu_samples.get(name)
                _cpu_samples[name] = (now, usage)
                if prev and now > prev[0]:
                    pct = (usage - prev[1]) / ((now - prev[0]) * 1e4)
                    out["cpu_pct"] = round(
                        max(0.0, min(100.0, pct / out["host_cpus"])), 1)
                break
    return out


CONFIG_MAX_BYTES = 65536


def get_config(name):
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    body = _read_file(_container_config_path(name))
    if not body:
        return {"code": "not_found"}
    return {"code": "ok", "text": body}


def save_config(name, text):
    """Raw config editor save. Minimal gating: size cap and it must still
    parse as key=value lines; liblxc validates the rest at next start."""
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    if not isinstance(text, str) or len(text) > CONFIG_MAX_BYTES:
        return {"code": "too_big"}
    cfg_path = _container_config_path(name)
    if not os.path.isfile(cfg_path):
        return {"code": "not_found"}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" not in stripped:
            return {"code": "bad_syntax", "detail": stripped[:80]}
    try:
        with open(cfg_path, "w") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    return {"code": "ok"}


# ── autostart (uci + init script) ────────────────────────────────────────────

AUTOSTART_SCRIPT = """#!/bin/sh /etc/rc.common
# Auto-generated by pcat-manager-web (LXC manager). Starts containers
# flagged autostart in /etc/config/pcat_lxc after boot.
START=99

start_one() {
	local cfg="$1" name auto
	config_get name "$cfg" name
	config_get_bool auto "$cfg" autostart 0
	[ "$auto" = "1" ] && [ -n "$name" ] && \\
		ubus -t 60 call lxc start "{\\"name\\":\\"$name\\"}" 2>/dev/null
}

start() {
	config_load pcat_lxc || return 0
	config_foreach start_one container
}

boot() {
	# lxcbr0 (netifd) and rpcd are up well before START=99; small grace
	# for slow eMMC mounts holding lxcpath.
	sleep 2
	start
}
"""


def _autostart_section_for(name):
    cfg = uci_show_dict(AUTOSTART_UCI, max_age=0)
    for key, val in cfg.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[2] == "name" and val == name:
            return parts[1]
    return None


def set_autostart(name, enabled, prune=False):
    if not NAME_RE.match(name or ""):
        return {"code": "bad_name"}
    if not os.path.exists("/etc/config/" + AUTOSTART_UCI):
        try:
            open("/etc/config/" + AUTOSTART_UCI, "a").close()
        except OSError as e:
            return {"code": "io_error", "detail": str(e)}
    sec = _autostart_section_for(name)
    if prune:
        if sec:
            uci_delete("{}.{}".format(AUTOSTART_UCI, sec))
            uci_commit(AUTOSTART_UCI)
        # Drops the container from its volume's manifest too, so a destroyed
        # container is not resurrected by the next boot's adoption scan.
        _sync_quiet()
        return {"code": "ok"}
    if sec is None:
        rc, out, _ = _run(["uci", "add", AUTOSTART_UCI, "container"])
        if rc != 0:
            return {"code": "uci_error"}
        sec = out.strip()
        uci_set("{}.{}.name={}".format(AUTOSTART_UCI, sec, name))
    uci_set("{}.{}.autostart={}".format(AUTOSTART_UCI, sec,
                                        "1" if enabled else "0"))
    uci_commit(AUTOSTART_UCI)
    _sync_quiet()
    return {"code": "ok"}


def _rename_autostart(old, new):
    sec = _autostart_section_for(old)
    if sec:
        uci_set("{}.{}.name={}".format(AUTOSTART_UCI, sec, new))
        uci_commit(AUTOSTART_UCI)
    _sync_quiet()


def fix_autostart():
    try:
        with open(AUTOSTART_INIT, "w") as f:
            f.write(AUTOSTART_SCRIPT)
        os.chmod(AUTOSTART_INIT, 0o755)
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    if not os.path.exists("/etc/config/" + AUTOSTART_UCI):
        try:
            open("/etc/config/" + AUTOSTART_UCI, "a").close()
        except OSError:
            pass
    _run([AUTOSTART_INIT, "enable"])
    return {"code": "ok"}


# ── bridge setup ─────────────────────────────────────────────────────────────

def _bridge_runtime_up():
    return os.path.isdir("/sys/class/net/" + BRIDGE)


def _bridge_uci_present():
    net = uci_show_dict("network", max_age=0)
    dev_ok = any(
        val == BRIDGE and key.endswith(".name") for key, val in net.items())
    if_ok = "network.{}".format(UCI_IFACE) in net
    return dev_ok and if_ok


def _lan_zone_index():
    for i in range(10):
        name = uci_get("firewall.@zone[{}].name".format(i))
        if not name:
            return None
        if name == "lan":
            return i
    return None


def fix_bridge():
    """Persist lxcbr0 as UCI-owned network state and bring it live via the
    normal reload path (netifd owns the device from here on, so it exists on
    every future boot with no init script of ours)."""
    # device section: named section 'lxcbr0dev' to be greppable; portless
    # bridges need bridge_empty or netifd tears them down.
    uci_set("network.lxcbr0dev=device")
    uci_set("network.lxcbr0dev.name={}".format(BRIDGE))
    uci_set("network.lxcbr0dev.type=bridge")
    uci_set("network.lxcbr0dev.bridge_empty=1")
    uci_set("network.{}=interface".format(UCI_IFACE))
    uci_set("network.{}.device={}".format(UCI_IFACE, BRIDGE))
    uci_set("network.{}.proto=static".format(UCI_IFACE))
    uci_set("network.{}.ipaddr={}".format(UCI_IFACE, BRIDGE_ADDR))
    uci_set("network.{}.netmask={}".format(UCI_IFACE, BRIDGE_NETMASK))
    uci_commit("network")

    # DHCP pool so containers just get addresses (dnsmasq serves any
    # interface with a dhcp section).
    uci_set("dhcp.{}=dhcp".format(UCI_IFACE))
    uci_set("dhcp.{}.interface={}".format(UCI_IFACE, UCI_IFACE))
    uci_set("dhcp.{}.start=100".format(UCI_IFACE))
    uci_set("dhcp.{}.limit=150".format(UCI_IFACE))
    uci_set("dhcp.{}.leasetime=12h".format(UCI_IFACE))
    uci_commit("dhcp")

    # lan zone membership: containers get forwarding (lan->wan is already
    # allowed) and are reachable from the LAN — the pragmatic default.
    idx = _lan_zone_index()
    if idx is not None:
        current = uci_get(
            "firewall.@zone[{}].network".format(idx)).split()
        if UCI_IFACE not in current:
            uci_add_list(
                "firewall.@zone[{}].network={}".format(idx, UCI_IFACE))
            uci_commit("firewall")

    _run(["/etc/init.d/network", "reload"], timeout=30)
    _run(["/etc/init.d/dnsmasq", "reload"], timeout=20)
    _run(["/etc/init.d/firewall", "reload"], timeout=30)

    for _ in range(10):
        if _bridge_runtime_up():
            return {"code": "ok"}
        time.sleep(0.5)
    return {"code": "bridge_not_up",
            "detail": "UCI written but {} did not appear".format(BRIDGE)}


# ── cgroup v2 ────────────────────────────────────────────────────────────────

def cgroup_mode():
    """'unified' when /sys/fs/cgroup is cgroup2, else 'legacy'."""
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return "unified"
    return "legacy"


def _cgroupfs_mount_enabled():
    if not os.path.exists(CGROUPFS_INIT):
        return False
    try:
        return any(n.endswith("cgroupfs-mount")
                   for n in os.listdir("/etc/rc.d"))
    except OSError:
        return False


# Runs at START=02 -- immediately after cgroupfs-mount (START=01) has replaced
# procd's cgroup2 with the v1 forest, and before anything else has a chance to
# populate it. That window is the whole point: measured on a photonicat2, by
# the time the console is up (110s, 246 processes) `umount /sys/fs/cgroup/cpu`
# already fails with EBUSY even though the controller has no sub-cgroups, so a
# live swap after boot cannot be won. At START=02 it succeeds.
#
# This exists as an installed init script rather than as `cgroupfs-mount
# disable` because a *deletion* does not survive a firmware upgrade. sysupgrade
# -c backs up the overlay as a tar of files, so an added file in /etc comes
# back after a flash but a removed rc.d symlink does not -- which is exactly
# why LXC broke again after upgrading to r7879 despite having been fixed
# before. An added script is preserved, so this holds across upgrades.
CGROUP2_INIT_SCRIPT = """#!/bin/sh /etc/rc.common

# Installed by pcat-manager-web: guarantee the unified cgroup hierarchy.
# systemd >= 256 (Debian 13, Ubuntu 24.04+) refuses to boot as container PID 1
# without it, so LXC containers cannot start on the v1 forest that
# cgroupfs-mount (START=01) installs.

START=02

boot() {
\t# procd's own cgroup2 mount survived; nothing to undo.
\t[ -e /sys/fs/cgroup/cgroup.controllers ] && return 0

\t# Deepest first: the controllers, then the tmpfs root they sit on.
\tfor m in $(awk '$2 ~ "^/sys/fs/cgroup" { print $2 }' /proc/mounts | sort -r); do
\t\tumount "$m" 2>/dev/null
\tdone
\tmount -t cgroup2 cgroup2 /sys/fs/cgroup 2>/dev/null
}
"""


def cgroup2_guard_installed():
    """True when the START=02 guard is installed *and* enabled."""
    if not os.path.exists(CGROUP2_INIT):
        return False
    try:
        return any(n.endswith("pcat-cgroup2") for n in os.listdir("/etc/rc.d"))
    except OSError:
        return False


def install_cgroup2_guard():
    """Write and enable the boot-time guard. Idempotent."""
    try:
        with open(CGROUP2_INIT, "w") as f:
            f.write(CGROUP2_INIT_SCRIPT)
        os.chmod(CGROUP2_INIT, 0o755)
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    _run([CGROUP2_INIT, "enable"])
    return {"code": "ok"}


def fix_cgroup():
    """Make the unified hierarchy the state of this device, now and after every
    future boot -- including boots that follow a firmware upgrade.

    Three things, in order of how durable they are. The guard is installed
    first because it is the only one that survives a flash; disabling the shim
    is belt-and-braces for the case where the guard is later removed; the live
    swap is a best-effort attempt to spare the user a reboot, and usually fails
    (see CGROUP2_INIT_SCRIPT -- the controller is EBUSY within seconds of
    boot), which is why it reports reboot_required rather than pretending."""
    install_cgroup2_guard()
    if os.path.exists(CGROUPFS_INIT):
        _run([CGROUPFS_INIT, "disable"])
    if cgroup_mode() == "unified":
        return {"code": "ok"}

    mounts = []
    for line in _read_file("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("/sys/fs/cgroup"):
            mounts.append((parts[1], parts[2]))
    # Children (controllers) before the tmpfs root.
    for target, _fstype in sorted(mounts, key=lambda m: -len(m[0])):
        rc, _, err = _run(["umount", target])
        if rc != 0:
            return {"code": "reboot_required",
                    "detail": "{} busy: {}".format(target, err.strip())}
    rc, _, err = _run(
        ["mount", "-t", "cgroup2", "cgroup2", "/sys/fs/cgroup"])
    if rc != 0 or cgroup_mode() != "unified":
        return {"code": "reboot_required", "detail": err.strip()}
    return {"code": "ok"}


def startup():
    """Boot-time hardening, called once from the app's deferred startup.

    Two things a firmware upgrade takes away, both restored here so a device
    comes up working without anyone visiting the Setup tab:

    * lxcpath. `sysupgrade -c` keeps only /etc, so /srv/lxc and every symlink
      in it is erased — and with them every container that lived on another
      disk, whose rootfs is still sitting there untouched. recover_containers()
      rebuilds the links from the records that did survive.
    * the unified cgroup hierarchy. The image ships a v1 shim that takes it
      away on every boot, including the first boot after a flash, which is
      what silently un-fixes it; the guard undoes that (CGROUP2_INIT_SCRIPT).

    Gated on the runtime actually being installed: the guard changes the
    cgroup layout for the whole system, and there is no reason to do that on
    an image that cannot run containers. Nothing else on the image needs v1
    (openclash, the one candidate, only greps /proc/1/cgroup to detect whether
    it is itself containerised).

    Returns a short string for the log, or None when it did nothing.
    """
    if not _which("lxc-create"):
        return None
    notes = []

    # Recovery first, and never allowed to take the cgroup guard down with it:
    # a device whose disks are in an odd state must still come up able to run
    # the containers it does have.
    try:
        rec = recover_containers()
    except Exception as e:
        rec = {"error": repr(e)}
    if rec.get("error"):
        notes.append("storage recovery failed: {}".format(rec["error"]))
    for key in ("relinked", "adopted", "missing", "conflicts"):
        if rec.get(key):
            notes.append("{}: {}".format(key, ",".join(rec[key])))

    if not cgroup2_guard_installed():
        result = install_cgroup2_guard()
        if result.get("code") != "ok":
            notes.append("cgroup2 guard install failed: {}".format(result))
        else:
            # Deliberately no live swap and no reboot here. The swap cannot
            # succeed this late (EBUSY), and rebooting a device because
            # someone opened the web UI would be its own bug. The guard takes
            # effect on the next boot, which for the post-flash case is the
            # boot the user is about to do anyway.
            notes.append(
                "cgroup2 guard installed (effective next boot); mode now: "
                "{}".format(cgroup_mode()))

    return "; ".join(notes) if notes else None


# ── ssh key (migration identity) ─────────────────────────────────────────────

def ssh_key_ready():
    return os.path.exists(SSH_KEY) and os.path.exists(SSH_KEY + ".pub")


def ssh_pubkey():
    return _read_file(SSH_KEY + ".pub").strip()


def fix_sshkey():
    if ssh_key_ready():
        return {"code": "ok", "pubkey": ssh_pubkey()}
    if not _which("ssh-keygen"):
        return {"code": "no_keygen"}
    try:
        os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    rc, out, err = _run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C",
         "pcat-lxc-migrate", "-f", SSH_KEY], timeout=30)
    if rc != 0:
        return {"code": "keygen_failed", "detail": err.strip()}
    return {"code": "ok", "pubkey": ssh_pubkey()}


# ── preflight / health ───────────────────────────────────────────────────────

# Filesystems a Linux rootfs can actually live on. exFAT/VFAT/NTFS volumes
# cannot hold symlinks, ownership or special modes — a container unpacked
# there is silently broken, so they are offered greyed-out, never used.
POSIX_FS = ("ext2", "ext3", "ext4", "btrfs", "f2fs", "xfs", "jfs",
            "ubifs", "overlay", "overlayfs")


def _fstype_for(path):
    """Filesystem type of the mount holding `path` (longest-prefix match
    over /proc/mounts)."""
    best_target, best_type = "", ""
    for line in _read_file("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        target, fstype = parts[1], parts[2]
        if (path == target or target == "/"
                or path.startswith(target.rstrip("/") + "/")):
            if len(target) >= len(best_target):
                best_target, best_type = target, fstype
    return best_type


def fs_can_host_containers(path):
    return _fstype_for(path) in POSIX_FS


def storage_candidates():
    """Places lxcpath could live: the current path's fs plus every mounted
    /mnt volume with meaningful space. Non-POSIX volumes are included but
    flagged so the UI can grey them out."""
    out = []
    seen = set()

    def add(path, label):
        try:
            st = os.statvfs(path)
        except OSError:
            return
        if path in seen:
            return
        seen.add(path)
        fstype = _fstype_for(path)
        out.append({
            "path": path, "label": label,
            "fstype": fstype,
            "posix": fstype in POSIX_FS,
            "free_bytes": st.f_bavail * st.f_frsize,
            "total_bytes": st.f_blocks * st.f_frsize,
        })

    add("/srv", "internal overlay")
    for line in _read_file("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith(MOUNT_ROOT):
            add(parts[1], parts[0])
    return out


# ── surviving a firmware upgrade ─────────────────────────────────────────────
#
# The firmware page flashes with `sysupgrade -c`, which exports
# SAVE_OVERLAY_PATH=/etc and then tars `find .$SAVE_OVERLAY_PATH \( -type f -o
# -type l \)` out of /overlay/upper. Everything outside /etc is destroyed by
# the flash — including lxcpath, which defaults to /srv/lxc.
#
# For a container on an external volume that is not the data loss it looks
# like. The rootfs is on a disk sysupgrade never touches; what died is the
# symlink /srv/lxc/<name> that made it visible. ubus and list_containers()
# read lxcpath and nothing else, so a perfectly intact container becomes
# invisible and the device looks wiped.
#
# The fix is to make the volume self-describing: next to the containers we
# keep a manifest of the bookkeeping that lives in /etc/config/pcat_lxc (ct
# id, autostart), and at boot we rebuild whatever the flash took away. /etc
# becomes a cache rather than the source of truth, which also survives an
# upgrade that keeps nothing at all (sysupgrade -n) and makes a disk portable
# to another device.
#
# What none of this can do is save a container whose rootfs was on the
# overlay: those bytes go with the flash. preflight() reports them separately
# so the UI can say so before the upgrade instead of after.

MANIFEST_NAME = ".pcat-lxc.json"

# Directory names a volume keeps containers under: lxc-storage is what the
# per-container disk move writes, lxc is what a whole-lxcpath move creates.
STORAGE_DIRNAMES = ("lxc-storage", "lxc")

# Where mounted volumes appear. Named rather than inlined so the recovery
# tests can point the scan at a fixture tree.
MOUNT_ROOT = "/mnt/"

# The root overlay, i.e. the filesystem a sysupgrade erases.
OVERLAY_FS = ("overlay", "overlayfs")


def on_overlay(path):
    """True when `path` sits on the root overlay and so cannot survive a
    firmware upgrade. Works for paths that do not exist yet — it is a lookup
    over /proc/mounts, not a stat."""
    return _fstype_for(path) in OVERLAY_FS


def default_storage():
    """Where containers should live so an upgrade cannot erase them: the
    roomiest mounted POSIX volume that is not the overlay.

    None when the device has nothing but internal storage. That is the case
    the UI has to warn about rather than fix — there is no safe location to
    offer, because the only writable filesystem is the one being replaced.
    """
    best = None
    for cand in storage_candidates():
        if not cand["posix"] or on_overlay(cand["path"]):
            continue
        if best is None or cand["free_bytes"] > best["free_bytes"]:
            best = cand
    return os.path.join(best["path"], "lxc") if best else None


def _manifest_path(base):
    return os.path.join(base, MANIFEST_NAME)


def _read_manifest(base):
    """{name: {"vmid": int|None, "autostart": bool}} from a volume's manifest.

    {} when it has none, or it is unreadable, or its JSON is damaged: a
    manifest is a convenience and never a precondition. The containers sitting
    next to it are the real data, and they are adoptable without it.
    """
    try:
        with open(_manifest_path(base)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for name, ent in (data.get("containers") or {}).items():
        if not NAME_RE.match(name or "") or not isinstance(ent, dict):
            continue
        try:
            vmid = int(ent.get("vmid"))
        except (TypeError, ValueError):
            vmid = None
        out[name] = {"vmid": vmid, "autostart": bool(ent.get("autostart"))}
    return out


def _write_manifest(base, entries):
    """Rewrite a volume's manifest atomically. Best effort: a full or
    read-only volume must not turn into a failed container operation."""
    body = {"version": 1, "containers": {
        name: {"vmid": ent.get("vmid"), "autostart": bool(ent.get("autostart"))}
        for name, ent in sorted(entries.items())}}
    tmp = _manifest_path(base) + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(body, f, indent=1, sort_keys=True)
        os.rename(tmp, _manifest_path(base))
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _real_dirs():
    """{name: real container directory} for everything in lxcpath, resolving
    the symlinks that point at other volumes."""
    base = lxcpath()
    out = {}
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        if not NAME_RE.match(name):
            continue
        link = os.path.join(base, name)
        if os.path.isfile(os.path.join(link, "config")):
            out[name] = os.path.realpath(link)
    return out


def volatile_containers():
    """Containers whose rootfs is on the overlay — the ones a firmware
    upgrade destroys, as opposed to merely unlinking."""
    return sorted(n for n, d in _real_dirs().items() if on_overlay(d))


def sync_records():
    """Write down where each container actually lives, in both places that can
    outlive a flash: `option storage` in /etc/config/pcat_lxc (kept by
    sysupgrade -c) and a manifest on the volume itself (kept by anything at
    all, including -n, and portable to another device).

    Idempotent and cheap — no uci write happens unless a value changed — so it
    is safe to call after every operation that moves a container or touches
    its bookkeeping.
    """
    dirs = _real_dirs()
    if not _ensure_uci_file():
        return {}
    secs = _uci_container_sections()
    base = os.path.realpath(lxcpath())
    changed = False
    for sec, opts in secs.items():
        real = dirs.get(opts["name"])
        if real is None:
            continue
        # Only an off-lxcpath container needs a record; one that lives in
        # lxcpath itself is found by looking, and a stale record left behind
        # by a move would send recovery to the old location.
        want = real if os.path.dirname(real) != base else ""
        have = opts.get("storage", "")
        if want == have:
            continue
        if want:
            uci_set("{}.{}.storage={}".format(AUTOSTART_UCI, sec, want))
        else:
            uci_delete("{}.{}.storage".format(AUTOSTART_UCI, sec))
        changed = True
    if changed:
        uci_commit(AUTOSTART_UCI)

    auto = _autostart_map()
    vmids = {o["name"]: int(o["vmid"]) for o in secs.values()
             if o.get("vmid", "").isdigit()}
    by_base = {}
    for name, real in dirs.items():
        parent = os.path.dirname(real)
        if on_overlay(parent):
            continue          # a manifest there dies with the containers
        by_base.setdefault(parent, {})[name] = {
            "vmid": vmids.get(name), "autostart": auto.get(name, False)}
    for parent, entries in by_base.items():
        _write_manifest(parent, entries)
    return by_base


def _sync_quiet():
    """sync_records() for call sites whose own result must not depend on it."""
    try:
        sync_records()
    except Exception:
        pass


def _scan_volumes():
    """{name: (dir, manifest entry)} for every container sitting on a mounted
    volume, wherever this or an earlier install put it.

    This is what re-adopts containers after a flash removed their lxcpath
    entry, and what picks up a disk carried over from another device.
    """
    found = {}
    for line in _read_file("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].startswith(MOUNT_ROOT):
            continue
        if not fs_can_host_containers(parts[1]):
            continue
        for dirname in STORAGE_DIRNAMES:
            parent = os.path.join(parts[1], dirname)
            try:
                names = sorted(os.listdir(parent))
            except OSError:
                continue
            manifest = _read_manifest(parent)
            for name in names:
                # Dot-directories are ours (image pool, backups) and NAME_RE
                # rejects them; a container is a directory with a config.
                if not NAME_RE.match(name) or name in found:
                    continue
                cdir = os.path.join(parent, name)
                if os.path.isfile(os.path.join(cdir, "config")):
                    found[name] = (cdir, manifest.get(name, {}))
    return found


def recover_containers():
    """Rebuild lxcpath from whatever survived, and adopt containers found on
    mounted volumes. Idempotent, and safe to run on every boot.

    Three sources, in descending order of trust:

    1. `option storage` in /etc/config/pcat_lxc — this device's own record of
       a container it put on another volume, kept across `sysupgrade -c`;
    2. the manifest on the volume — the same record written where a flash
       cannot reach it, which is also what makes a disk portable;
    3. the container directory itself — a config next to a rootfs is a
       container whatever any bookkeeping says.

    A name already present in lxcpath is never touched: what is there wins
    over anything a scan turns up, so this can never replace a live container
    with a stale copy from a disk.
    """
    base = lxcpath()
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as e:
        return {"error": str(e)}

    relinked, adopted, missing, conflicts = [], [], [], []

    def link_in(name, cdir):
        try:
            os.symlink(cdir, os.path.join(base, name))
            return True
        except OSError:
            conflicts.append(name)
            return False

    for opts in _uci_container_sections().values():
        name, cdir = opts["name"], opts.get("storage", "")
        if not cdir or os.path.lexists(os.path.join(base, name)):
            continue
        if os.path.isfile(os.path.join(cdir, "config")):
            if link_in(name, cdir):
                relinked.append(name)
        else:
            # Recorded, but its volume is not mounted (yet). Reported rather
            # than forgotten: the record is the only thing that knows this
            # container exists, and dropping it would strand the data.
            missing.append(name)

    restore = {}
    for name, (cdir, ent) in _scan_volumes().items():
        if os.path.lexists(os.path.join(base, name)):
            continue
        if link_in(name, cdir):
            adopted.append(name)
            restore[name] = ent

    if restore:
        # Re-adopting must not renumber a container the user knows by its ct
        # id — but must not steal an id another container already holds
        # either, so a colliding id falls back to the lowest free one.
        prefer = {n: e["vmid"] for n, e in restore.items()
                  if e.get("vmid") and not vmid_in_use(e["vmid"])}
        ensure_vmids(sorted(restore), prefer=prefer)
        for name, ent in restore.items():
            if ent.get("autostart"):
                set_autostart(name, True)

    _sync_quiet()
    return {"relinked": sorted(relinked), "adopted": sorted(adopted),
            "missing": sorted(missing), "conflicts": sorted(conflicts)}


def fix_storage():
    """Setup-tab Fix: rescan the disks and rebuild lxcpath. Cannot conjure
    back a rootfs the flash erased — that is what the volatile list warns
    about — but recovers anything whose bytes still exist somewhere."""
    rec = recover_containers()
    if rec.get("error"):
        return {"code": "io_error", "detail": rec["error"]}
    return {"code": "ok", "result": rec}


def preflight():
    """One status blob for the Setup tab: every check with ok + fixability."""
    path = lxcpath()
    path_exists = os.path.isdir(path)
    try:
        st = os.statvfs(path if path_exists else "/")
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = None
    states = container_states()
    checks = {
        "runtime": {
            "ok": bool(_which("lxc-create"))
                  and os.path.exists("/usr/lib/rpcd/lxc.so"),
            "fixable": False,
        },
        "ubus": {"ok": ubus_available(), "fixable": False},
        "bridge": {
            "ok": _bridge_runtime_up() and _bridge_uci_present(),
            "runtime": _bridge_runtime_up(),
            "persistent": _bridge_uci_present(),
            "fixable": True,
        },
        # What decides this is the hierarchy containers actually get: unified,
        # now, plus something that will keep it unified next boot. The shim
        # being enabled is not itself a failure once the START=02 guard is
        # installed -- the guard runs after it and undoes it -- and reporting
        # it as one left the page saying "not set up" about a device whose
        # cgroup2 was mounted and working.
        "cgroup": {
            "ok": cgroup_mode() == "unified"
                  and (cgroup2_guard_installed() or not _cgroupfs_mount_enabled()),
            "mode": cgroup_mode(),
            "shim_enabled": _cgroupfs_mount_enabled(),
            "guard_installed": cgroup2_guard_installed(),
            "fixable": True,
        },
        "autostart": {
            "ok": os.path.exists(AUTOSTART_INIT),
            "fixable": True,
        },
        "sshkey": {
            "ok": ssh_key_ready(),
            "fixable": bool(_which("ssh-keygen")),
        },
    }
    # Storage durability. `ok` is about what Fix can act on — containers this
    # device knows about whose lxcpath entry is missing — NOT about volatile
    # storage, which no button can repair and which would otherwise leave a
    # permanently red check on every device without an external disk.
    dirs = _real_dirs()
    checks["storage"] = {
        "ok": not any(o.get("storage") and o["name"] not in dirs
                      for o in _uci_container_sections().values()),
        "fixable": True,
        # Rootfs on the overlay: erased by the next firmware upgrade.
        "volatile": sorted(n for n, d in dirs.items() if on_overlay(d)),
        # Recorded on a volume that is not mounted right now.
        "orphans": sorted(o["name"] for o in _uci_container_sections().values()
                          if o.get("storage") and o["name"] not in dirs),
        "recommended": default_storage(),
    }
    return {
        "checks": checks,
        "ready": all(c["ok"] for k, c in checks.items()
                     if k in ("runtime", "ubus", "bridge", "cgroup")),
        "lxcpath": path,
        "lxcpath_exists": path_exists,
        "free_bytes": free,
        "running": sum(1 for s in states.values() if s == "RUNNING"),
        "total": len(states),
        "templates": TEMPLATE_CATALOG,
        "arch": _default_arch(),
    }


def apply_fix(what):
    """Dispatch for POST fix.json; 'all' runs everything applicable."""
    fixers = {
        "bridge": fix_bridge,
        "cgroup": fix_cgroup,
        "autostart": fix_autostart,
        "sshkey": fix_sshkey,
        "storage": fix_storage,
    }
    if what == "all":
        results = {}
        for key, fn in fixers.items():
            results[key] = fn()
        bad = {k: v for k, v in results.items() if v.get("code") not in
               ("ok",)}
        return {"code": "ok" if not bad else "partial", "results": results}
    if what not in fixers:
        return {"code": "bad_fix"}
    return fixers[what]()


# ── create (job) ─────────────────────────────────────────────────────────────

def _default_arch():
    rc, out, _ = _run(["uname", "-m"], timeout=5)
    machine = out.strip()
    return {"aarch64": "arm64", "x86_64": "amd64",
            "armv7l": "armhf"}.get(machine, machine or "arm64")


def _write_file(path, text):
    with open(path, "w") as f:
        f.write(text)


def _mac_for(name):
    """Deterministic locally-administered MAC, same derivation the clone path
    uses so two containers never collide on the bridge."""
    digest = 0
    for ch in name:
        digest = (digest * 131 + ord(ch)) & 0xFFFFFF
    return "00:16:3e:{:02x}:{:02x}:{:02x}".format(
        (digest >> 16) & 0xFF, (digest >> 8) & 0xFF, digest & 0xFF)


def _default_config_for(name):
    """Container config from /etc/lxc/default.conf, with the rootfs path and
    a real MAC filled in (the shipped file carries an 'xx:xx:xx' placeholder
    that lxc-create would otherwise have substituted)."""
    text = _read_file(DEFAULT_CONF)
    lines = []
    seen_hwaddr = False
    for line in text.splitlines():
        if line.strip().startswith("lxc.net.0.hwaddr"):
            lines.append("lxc.net.0.hwaddr = {}".format(_mac_for(name)))
            seen_hwaddr = True
        elif line.strip().startswith("lxc.rootfs.path"):
            continue
        else:
            lines.append(line)
    if not lines:
        lines = ["lxc.net.0.type = veth",
                 "lxc.net.0.link = {}".format(BRIDGE),
                 "lxc.net.0.flags = up"]
    if not seen_hwaddr:
        lines.append("lxc.net.0.hwaddr = {}".format(_mac_for(name)))
    lines.append("lxc.uts.name = {}".format(name))
    lines.append("lxc.rootfs.path = dir:{}".format(
        os.path.join(lxcpath(), name, "rootfs")))
    return "\n".join(lines) + "\n"


# Images ship these files containing the literal string LXC_NAME, which
# lxc-download's template would have rewritten (its `sed -i s#LXC_NAME#...`
# pass, driven by a file list in meta.tar.xz). We fetch only rootfs.tar.xz,
# so the substitution is ours to do — without it every container comes up
# calling itself "LXC_NAME".
_PLACEHOLDER_FILES = ("etc/hostname", "etc/hosts")


def _substitute_rootfs_placeholders(rootfs, name):
    """Replace LXC_NAME in the handful of files images template on."""
    for rel in _PLACEHOLDER_FILES:
        full = os.path.join(rootfs, rel)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        try:
            with open(full, "r", errors="surrogateescape") as f:
                body = f.read()
            if "LXC_NAME" not in body:
                continue
            with open(full, "w", errors="surrogateescape") as f:
                f.write(body.replace("LXC_NAME", name))
        except OSError as e:
            pcat_logger.logger.warning(
                "lxc_manager: placeholder subst {}: {}".format(rel, e))


def _image_index(server, timeout=30):
    """Fetch and parse the image index. Returns a list of
    (dist, release, arch, variant, build, path) tuples, or [] on failure."""
    url = "https://{}/meta/1.0/index-system".format(server)
    rc, out, _ = _run(["wget", "-q", "-T", str(timeout), "-O", "-", url],
                      timeout=timeout + 15)
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(";")
        if len(parts) >= 6 and parts[5].startswith("/"):
            rows.append((parts[0], parts[1], parts[2], parts[3],
                         parts[4], parts[5]))
    return rows


def _resolve_image(server, dist, release, arch, variant="default"):
    """Path of the newest build for dist/release/arch/variant, or None.
    Picked by the build stamp (field 5, 'YYYYmmdd_HH:MM' so it sorts
    lexically) rather than by position — the index is not ordered."""
    best = None
    for row in _image_index(server):
        if (row[0] == dist and row[1] == release and row[2] == arch
                and row[3] == variant):
            if best is None or row[4] > best[4]:
                best = row
    return best[5] if best else None


# A stalled transfer is the normal failure here, not the exception: the image
# CDN routinely drops mid-file on these links (measured: 704K/736K/1.5M of a
# 3.3M rootfs). lxc-download cannot cope with that — its download_file runs
# `wget -q ... >/dev/null 2>&1`, so the bytes are invisible AND a truncated
# file still looks like success. The container is then created with an empty
# rootfs, and `ubus lxc start` reports only a bare "Invalid argument".
# So we fetch the tarball ourselves: resume where we left off (-c), verify the
# result against Content-Length, and report bytes as they land.
_DL_ATTEMPTS = 8


def _remote_size(url, timeout=30):
    """Content-Length via a spider request, or 0 when the server won't say."""
    rc, out, err = _run(["wget", "--spider", "-S", "-T", str(timeout), url],
                        timeout=timeout + 15)
    size = 0
    for line in (out + "\n" + err).splitlines():
        m = re.match(r"\s*[Cc]ontent-[Ll]ength:\s*(\d+)", line)
        if m:
            size = int(m.group(1))   # last wins: follow redirects to the file
    return size


# ── image pool ───────────────────────────────────────────────────────────────
#
# Downloaded rootfs tarballs are kept, keyed by dist/release/arch + the build
# stamp from the index, so creating a second container from the same image is
# a local unpack instead of another multi-minute download. A pooled file is
# only reused once `xz -t` has passed and a .ok marker was written next to it
# — the marker is what distinguishes "complete and verified" from "a partial
# transfer that happens to be sitting there", which is exactly the confusion
# that produced the empty-rootfs containers in the first place.

POOL_DIRNAME = ".pcat-image-pool"
_pool_locks = {}
_pool_locks_guard = threading.Lock()


def pool_dir():
    return os.path.join(lxcpath(), POOL_DIRNAME)


def _pool_key(dist, release, arch, path):
    """Stable filename for an image. `path` carries the build stamp, so a
    newer build lands beside the old one instead of silently reusing it."""
    build = ""
    for part in (path or "").strip("/").split("/"):
        if re.match(r"^\d{8}_\d{2}:\d{2}$", part):
            build = part.replace(":", "")
            break
    stem = "{}_{}_{}".format(dist, release, arch)
    if build:
        stem += "_" + build
    return re.sub(r"[^A-Za-z0-9._-]", "_", stem) + ".tar.xz"


def _pool_path(dist, release, arch, path):
    return os.path.join(pool_dir(), _pool_key(dist, release, arch, path))


def _pool_lock_for(tarball):
    """One lock per pooled image: two creates of the same image must not
    download onto the same file, but different images may run concurrently."""
    with _pool_locks_guard:
        lock = _pool_locks.get(tarball)
        if lock is None:
            lock = threading.Lock()
            _pool_locks[tarball] = lock
    return lock


def _pool_hit(tarball):
    """True only for a fully downloaded, verified image."""
    if not os.path.exists(tarball) or not os.path.exists(tarball + ".ok"):
        return False
    if os.path.exists(tarball + ".aria2"):
        return False        # aria2 control file => transfer still incomplete
    try:
        return os.path.getsize(tarball) > 0
    except OSError:
        return False


def _pool_mark_ok(tarball):
    try:
        _write_file(tarball + ".ok", "verified\n")
    except OSError as e:
        pcat_logger.logger.warning("lxc_manager: pool marker: {}".format(e))


def pool_list():
    """Pooled images for the UI: name, size, mtime."""
    rows = []
    directory = pool_dir()
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return rows
    for entry in entries:
        if not entry.endswith(".tar.xz"):
            continue
        full = os.path.join(directory, entry)
        try:
            st = os.stat(full)
        except OSError:
            continue
        rows.append({
            "name": entry,
            "size_bytes": st.st_size,
            "mtime": int(st.st_mtime),
            "verified": os.path.exists(full + ".ok"),
        })
    return rows


def pool_delete(entry):
    """Drop one pooled image (and its markers)."""
    if not entry or "/" in entry or ".." in entry:
        return {"code": "bad_name"}
    full = os.path.join(pool_dir(), entry)
    if not os.path.exists(full):
        return {"code": "not_found"}
    for path in (full, full + ".ok", full + ".aria2"):
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"code": "ok"}


def _fmt_rate(bps):
    """Human download rate for the job message/log."""
    if bps >= 1024 * 1024:
        return "{:.1f} MiB/s".format(bps / (1024.0 * 1024.0))
    if bps >= 1024:
        return "{:.0f} KiB/s".format(bps / 1024.0)
    return "{} B/s".format(int(bps))


# aria2's summary line, e.g.
#   [#77b7d1 224KiB/3.2MiB(6%) CN:1 DL:77KiB ETA:39s]
# Sizes are human units, so DL: is parsed via _parse_unit rather than int().
_ARIA_DL_RE = re.compile(r"DL:\s*([0-9.]+)\s*([KMG]?i?B)")
# The leading "done/total" of that same line. This is the ONLY trustworthy
# byte count for aria2: it preallocates the destination, so the file reports
# its full final size from the first second and os.path.getsize() would read
# as 100% complete while almost nothing has transferred.
_ARIA_DONE_RE = re.compile(
    r"\[#\w+\s+([0-9.]+)\s*([KMG]?i?B)\s*/\s*([0-9.]+)\s*([KMG]?i?B)")
_ARIA_ETA_RE = re.compile(r"ETA:\s*(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
_UNIT_MULT = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
              "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}


def _parse_unit(value, unit):
    try:
        return int(float(value) * _UNIT_MULT.get(unit, 1))
    except (TypeError, ValueError):
        return 0


def _parse_aria_eta(line):
    m = _ARIA_ETA_RE.search(line)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def _download_with_progress(handle, url, dest, label, timeout=60):
    """Resumable fetch reporting bytes AND transfer rate into the job. Raises
    RuntimeError if the file cannot be completed; returns the byte count.

    aria2c is preferred: it resumes (-c), splits the file across connections
    (-x/-s, which on this link turned a ~15-minute wget crawl into ~30s), and
    prints a summary line we can parse for speed/ETA. wget is kept as the
    fallback for images without it — same resume loop, no rate to report."""
    total = _remote_size(url, timeout=timeout)
    handle.set_progress(message=label, bytes_total=total, bytes_done=0,
                        pct=0 if total else None, speed_bps=0)

    def _have():
        try:
            return os.path.getsize(dest)
        except OSError:
            return 0

    if _which("aria2c"):
        try:
            return _download_aria2(handle, url, dest, label, total, _have,
                                   timeout)
        except nas_jobs.JobCancelled:
            raise
        except RuntimeError as e:
            # Never let an aria2-specific problem (an option this build does
            # not know, a broken accelerator) cost the user the create — wget
            # still finishes, just slower.
            handle.log("aria2 failed ({}), falling back to wget".format(e))
            handle.set_progress(message="{} — retrying with wget".format(label))
    return _download_wget(handle, url, dest, label, total, _have, timeout)


def _download_aria2(handle, url, dest, label, total, have, timeout):
    """aria2 leg. Byte counts come from the summary line, never from the file
    on disk (aria2 preallocates, so the file is full-size immediately), and
    completion is judged by the .aria2 control file disappearing."""
    directory, filename = os.path.split(dest)
    control = dest + ".aria2"
    last_done = -1
    progressed = False

    def _complete():
        # aria2 removes the control file only when every piece has landed.
        return os.path.exists(dest) and not os.path.exists(control)

    for attempt in range(_DL_ATTEMPTS):
        handle.check_cancel()
        argv = ["aria2c", "--summary-interval=1", "--console-log-level=warn",
                "--auto-file-renaming=false", "--continue=true",
                "--allow-overwrite=true",
                "--max-connection-per-server=4", "--split=4",
                "--min-split-size=1M", "--max-tries=3",
                "--retry-wait=2", "--timeout={}".format(timeout),
                "--connect-timeout=30",
                "-d", directory, "-o", filename, url]

        seen = {"done": 0}

        def on_line(line, _seen=seen):
            if "DL:" not in line:
                return
            speed_m = _ARIA_DL_RE.search(line)
            speed = _parse_unit(speed_m.group(1), speed_m.group(2)) \
                if speed_m else 0
            done_m = _ARIA_DONE_RE.search(line)
            if done_m:
                _seen["done"] = _parse_unit(done_m.group(1), done_m.group(2))
            done = _seen["done"]
            handle.set_progress(
                bytes_done=done, speed_bps=speed,
                eta_sec=_parse_aria_eta(line),
                message="{} — {}".format(label, _fmt_rate(speed)),
                pct=min(99, done * 100 // total) if total else None)

        # watch_subprocess registers the pid for cancellation and drains
        # stdout for us; the summary lines arrive through line_cb.
        rc = handle.watch_subprocess(argv, line_cb=on_line)
        handle.check_cancel()

        done = seen["done"]
        if _complete():
            size = have()
            handle.set_progress(bytes_done=size, speed_bps=0, eta_sec=0)
            return size
        handle.set_progress(bytes_done=done, speed_bps=0,
                            pct=min(99, done * 100 // total) if total else None)
        if done > 0:
            progressed = True
        elif not progressed and attempt >= 1:
            # Two runs, not one byte: this aria2 cannot do the job at all
            # (unknown option, no TLS, blocked). Bail out now so the caller
            # can fall back to wget instead of burning every retry.
            raise RuntimeError(
                "aria2 transferred nothing (rc={})".format(rc))
        # The .aria2 control file is deliberately left in place — that is
        # what lets the next attempt resume instead of restarting.
        if done == last_done and attempt >= 2:
            break
        last_done = done
        handle.log("{}: stalled at {} of {} bytes, retrying ({}/{})".format(
            label, done, total or "?", attempt + 1, _DL_ATTEMPTS))
        handle.set_progress(message="{} — retrying ({}/{})".format(
            label, attempt + 1, _DL_ATTEMPTS))
    raise RuntimeError(
        "download incomplete: got {} of {} bytes from {}".format(
            seen["done"], total or "?", url))


def _download_wget(handle, url, dest, label, total, have, timeout):
    last_size = -1
    for attempt in range(_DL_ATTEMPTS):
        handle.check_cancel()
        # -c resumes; without it every retry restarts from zero and a link
        # that dies at 1.5M can never finish no matter how many retries.
        proc = subprocess.Popen(
            ["wget", "-c", "-q", "-T", str(timeout), "-O", dest, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        prev, prev_t = have(), time.time()
        try:
            while proc.poll() is None:
                if handle.cancelled:
                    proc.terminate()
                    raise nas_jobs.JobCancelled()
                time.sleep(1.0)
                done, now = have(), time.time()
                speed = int((done - prev) / max(0.001, now - prev_t))
                prev, prev_t = done, now
                handle.set_progress(
                    bytes_done=done, speed_bps=max(0, speed),
                    message="{} — {}".format(label, _fmt_rate(max(0, speed))),
                    pct=min(99, done * 100 // total) if total else None)
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait()
        done = have()
        handle.set_progress(
            bytes_done=done, speed_bps=0,
            pct=min(99, done * 100 // total) if total else None)
        if total and done >= total:
            return done
        if not total and proc.returncode == 0 and done > 0:
            return done      # server withheld the length; trust wget's rc
        if done == last_size:
            # A retry that transferred nothing new means resume isn't working
            # (no ranges, or the link is dead) — more attempts won't help.
            if attempt >= 2:
                break
        last_size = done
        handle.log("{}: stalled at {} of {} bytes, retrying ({}/{})".format(
            label, done, total or "?", attempt + 1, _DL_ATTEMPTS))
        handle.set_progress(message="{} — retrying ({}/{})".format(
            label, attempt + 1, _DL_ATTEMPTS))
    raise RuntimeError(
        "download incomplete: got {} of {} bytes from {}".format(
            have(), total or "?", url))


def create_container(name, dist, release, arch=None, storage=None, vmid=None):
    """Download-template create as a background job (network-bound, minutes).
    `storage` picks where the container lives (Proxmox-style): when it isn't
    the global lxcpath, the container is created there and symlinked into
    lxcpath so the ubus runtime still sees it. `vmid` is the CT id the user
    picked; omitted means the lowest free one. Returns (job_id, None) or
    (None, {"code": ...})."""
    if not NAME_RE.match(name or ""):
        return None, {"code": "bad_name"}
    if vmid not in (None, ""):
        try:
            vmid = int(vmid)
        except (TypeError, ValueError):
            return None, {"code": "bad_vmid"}
        if vmid < VMID_START or vmid > 999999:
            return None, {"code": "bad_vmid"}
        if vmid_in_use(vmid):
            return None, {"code": "vmid_in_use", "detail": str(vmid)}
    else:
        vmid = None
    for field in (dist, release):
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$", field or ""):
            return None, {"code": "bad_template"}
    arch = arch or _default_arch()
    if not re.match(r"^[a-z0-9]{2,12}$", arch):
        return None, {"code": "bad_template"}
    base = lxcpath()
    target = base
    if storage:
        if not storage.startswith("/") or ".." in storage:
            return None, {"code": "bad_path"}
        target = os.path.normpath(storage)
    if target != base and not fs_can_host_containers(target):
        return None, {"code": "bad_fs", "detail": _fstype_for(target)}
    if os.path.lexists(os.path.join(base, name)) or (
            target != base and os.path.exists(os.path.join(target, name))):
        return None, {"code": "exists"}
    # Not lxc-create: we fetch and unpack the image ourselves (see module
    # docstring), so these are the binaries that actually have to be present.
    for binary in ("wget", "xz", "tar"):
        if not _which(binary):
            return None, {"code": "no_tool", "detail": binary}
    server = uci_get("lxc.lxc.url") or "images.linuxcontainers.org"

    def runner(handle):
        os.makedirs(target, exist_ok=True)
        cdir = os.path.join(target, name)

        def _cleanup_failed():
            # Never leave the half-made dir behind: it has an empty rootfs, so
            # a later start would fail with an unexplained "Invalid argument",
            # and its presence makes a retry fail with "exists".
            if os.path.isdir(cdir) and not os.path.exists(
                    os.path.join(cdir, "rootfs", "etc")):
                _run(["rm", "-rf", cdir], timeout=120)
                if target != base and os.path.islink(
                        os.path.join(base, name)):
                    try:
                        os.unlink(os.path.join(base, name))
                    except OSError:
                        pass

        try:
            handle.set_progress(message="looking up {} {} ({})".format(
                dist, release, arch))
            path = _resolve_image(server, dist, release, arch)
            if not path:
                raise RuntimeError(
                    "no image for {} {} {} on {}".format(
                        dist, release, arch, server))

            desc = "{} {} ({})".format(dist, release, arch)
            tarball = _pool_path(dist, release, arch, path)
            os.makedirs(os.path.dirname(tarball), exist_ok=True)
            with _pool_lock_for(tarball):
                if _pool_hit(tarball):
                    handle.log("image pool: reusing {}".format(tarball))
                    handle.set_progress(
                        message="using pooled image {}".format(desc),
                        bytes_done=os.path.getsize(tarball),
                        bytes_total=os.path.getsize(tarball), pct=99)
                else:
                    _download_with_progress(
                        handle,
                        "https://{}{}rootfs.tar.xz".format(server, path),
                        tarball, "downloading {}".format(desc))
                    # Verify before unpacking: this is the check whose absence
                    # let a truncated tarball become a container with an empty
                    # rootfs. A verified file is what makes the pool safe to
                    # reuse, so only mark it good once xz -t passes.
                    handle.set_progress(message="verifying image")
                    rc, _, err = _run(["xz", "-t", tarball], timeout=300)
                    if rc != 0:
                        for junk in (tarball, tarball + ".aria2"):
                            try:
                                os.unlink(junk)   # poisoned; don't resume onto it
                            except OSError:
                                pass
                        raise RuntimeError(
                            "downloaded image is corrupt: {}".format(
                                err.strip()))
                    _pool_mark_ok(tarball)

            handle.set_progress(message="unpacking rootfs", pct=None)
            rootfs = os.path.join(cdir, "rootfs")
            os.makedirs(rootfs, exist_ok=True)
            rc = handle.watch_subprocess(
                ["tar", "--numeric-owner", "-xpJf", tarball, "-C", rootfs])
            if rc != 0:
                raise RuntimeError("unpacking rootfs failed (tar {})".format(rc))
            if not os.path.exists(os.path.join(rootfs, "etc")):
                raise RuntimeError("unpacked rootfs has no /etc")

            handle.set_progress(message="writing config")
            _substitute_rootfs_placeholders(rootfs, name)
            _write_file(os.path.join(cdir, "config"),
                        _default_config_for(name))
            if target != base:
                os.symlink(cdir, os.path.join(base, name))
            # Claim the requested CT id now that the container exists. Done
            # here rather than up front so a failed create does not burn an
            # id; if it was taken in the meantime, ensure_vmids falls back to
            # the lowest free one instead of failing the finished container.
            ensure_vmids([name], prefer={name: vmid} if vmid else None)
            # Record where it landed, in /etc and on the volume, so a
            # firmware upgrade cannot lose track of it.
            _sync_quiet()
            # The tarball deliberately stays in the pool for the next create.
            handle.set_progress(pct=100, message="created")
        except BaseException:
            _cleanup_failed()
            raise

    return nas_jobs.submit(
        "lxc_create",
        "LXC create: {} ({} {})".format(name, dist, release),
        "LXC 创建：{}（{} {}）".format(name, dist, release),
        runner, meta={"container": name},
        exclusive_key="lxc:" + name)


# ── clone (job) ──────────────────────────────────────────────────────────────

def rewrite_clone_config(config_text, base, src, dst):
    """Config surgery for a clone: container-dir paths, uts name, and a
    fresh locally-administered MAC so both containers can share the bridge.
    Pure function for tests (MAC derived from the dst name, not random, so
    the result is deterministic)."""
    out = rewrite_rootfs_paths(
        config_text, os.path.join(base, src), os.path.join(base, dst))
    lines = []
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("lxc.uts.name"):
            lines.append("lxc.uts.name = {}".format(dst))
        elif stripped.startswith("lxc.net.0.hwaddr"):
            digest = 0
            for ch in dst:
                digest = (digest * 131 + ord(ch)) & 0xFFFFFF
            lines.append(
                "lxc.net.0.hwaddr = 00:16:3e:{:02x}:{:02x}:{:02x}".format(
                    (digest >> 16) & 0xFF, (digest >> 8) & 0xFF,
                    digest & 0xFF))
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def clone_container(src, dst):
    """Local clone as a job: rsync the container dir, then rewrite the
    config (paths, hostname, MAC). Source must be stopped."""
    for n in (src, dst):
        if not NAME_RE.match(n or ""):
            return None, {"code": "bad_name"}
    if src == dst:
        return None, {"code": "bad_name"}
    base = lxcpath()
    if not os.path.isdir(os.path.join(base, src)):
        return None, {"code": "not_found"}
    if os.path.exists(os.path.join(base, dst)):
        return None, {"code": "exists"}
    if container_states().get(src) == "RUNNING":
        return None, {"code": "running"}
    if not _which("rsync"):
        return None, {"code": "no_rsync"}

    def runner(handle):
        # Clone lands next to the source's REAL directory (the source may
        # live on another storage volume behind a symlink); the clone gets
        # its own symlink into lxcpath when that's elsewhere.
        src_real = os.path.realpath(os.path.join(base, src))
        real_parent = os.path.dirname(src_real)
        dst_dir = os.path.join(real_parent, dst)
        rc, out, _ = _run(["du", "-sk", src_real], timeout=300)
        total = int(out.split()[0]) * 1024 if rc == 0 and out.split() else 0
        handle.set_progress(message="copying rootfs", bytes_total=total)
        rc = handle.watch_subprocess(
            ["rsync", "-a", "--numeric-ids", src_real + "/", dst_dir + "/"])
        if rc != 0:
            _run(["rm", "-rf", dst_dir], timeout=300)
            raise RuntimeError("rsync exited {}".format(rc))
        cfg_path = os.path.join(dst_dir, "config")
        body = _read_file(cfg_path)
        if body:
            with open(cfg_path, "w") as f:
                f.write(rewrite_clone_config(body, real_parent, src, dst))
        if real_parent != os.path.realpath(base):
            os.symlink(dst_dir, os.path.join(base, dst))
        handle.set_progress(pct=100, message="cloned")

    return nas_jobs.submit(
        "lxc_clone",
        "LXC clone: {} → {}".format(src, dst),
        "LXC 克隆：{} → {}".format(src, dst),
        runner, meta={"container": src, "clone": dst},
        exclusive_key="lxc:" + src)


# ── backup / restore (jobs) ──────────────────────────────────────────────────
#
# Proxmox-style vzdump-lite: one .tar.gz per backup holding the container
# directory (config + rootfs), written to a backup dir under lxcpath. Offline
# only — snapshotting a live rootfs would capture a torn filesystem, and this
# firmware has no LVM/btrfs snapshot to borrow, so requiring STOPPED is the
# honest option rather than producing a backup that silently will not restore.

BACKUP_DIRNAME = ".pcat-backups"
# vzdump-ish and, more importantly, parseable back into (name, stamp).
_BACKUP_RE = re.compile(r"^vzdump-lxc-([A-Za-z0-9][A-Za-z0-9_.-]{0,63})-"
                        r"(\d{8}_\d{6})\.tar\.gz$")


def backup_dir():
    """Where new container archives are written.

    Deliberately NOT under lxcpath when lxcpath is on the overlay: an archive
    that the firmware upgrade erases along with the container it was meant to
    protect is not a backup. Falls back to lxcpath only on a device with no
    durable volume at all, where there is nowhere better to put it.
    """
    base = lxcpath()
    if on_overlay(base):
        durable = default_storage()
        if durable:
            return os.path.join(durable, BACKUP_DIRNAME)
    return os.path.join(base, BACKUP_DIRNAME)


def _backup_dirs():
    """Every directory a backup may be sitting in: the durable one first,
    then the legacy lxcpath location, so archives written before backup_dir()
    moved still list, restore and delete."""
    out = []
    for directory in (backup_dir(), os.path.join(lxcpath(), BACKUP_DIRNAME)):
        if directory not in out:
            out.append(directory)
    return out


def _find_backup(entry):
    """Full path of a backup by file name, wherever it lives. None if gone."""
    for directory in _backup_dirs():
        full = os.path.join(directory, entry)
        if os.path.isfile(full):
            return full
    return None


def _backup_name(name, stamp):
    return "vzdump-lxc-{}-{}.tar.gz".format(name, stamp)


def backup_list():
    """Backups on disk, newest first, from every location they may live in."""
    rows, seen = [], set()
    for directory in _backup_dirs():
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            m = _BACKUP_RE.match(entry)
            if not m or entry in seen:
                continue
            full = os.path.join(directory, entry)
            try:
                st = os.stat(full)
            except OSError:
                continue
            seen.add(entry)
            rows.append({
                "file": entry,
                "container": m.group(1),
                "stamp": m.group(2),
                "size_bytes": st.st_size,
                "mtime": int(st.st_mtime),
            })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def backup_delete(entry):
    if not entry or not _BACKUP_RE.match(entry):
        return {"code": "bad_name"}
    full = _find_backup(entry)
    if not full:
        return {"code": "not_found"}
    try:
        os.unlink(full)
    except OSError as e:
        return {"code": "io_error", "detail": str(e)}
    return {"code": "ok"}


def backup_container(name, stamp):
    """Archive a stopped container to backup_dir() as a job. `stamp` is the
    caller's timestamp (YYYYmmdd_HHMMSS) — this module has no clock of its
    own in tests. Returns (job_id, None) or (None, {"code": ...})."""
    if not NAME_RE.match(name or ""):
        return None, {"code": "bad_name"}
    if not re.match(r"^\d{8}_\d{6}$", stamp or ""):
        return None, {"code": "bad_name"}
    base = lxcpath()
    if not os.path.isdir(os.path.join(base, name)):
        return None, {"code": "not_found"}
    if container_states().get(name) == "RUNNING":
        return None, {"code": "running"}
    if not _which("tar"):
        return None, {"code": "no_tool", "detail": "tar"}

    out_name = _backup_name(name, stamp)
    out_path = os.path.join(backup_dir(), out_name)

    def runner(handle):
        os.makedirs(backup_dir(), exist_ok=True)
        real_dir = os.path.realpath(os.path.join(base, name))
        parent = os.path.dirname(real_dir)
        rc, out, _ = _run(["du", "-sk", real_dir], timeout=300)
        total = int(out.split()[0]) * 1024 if rc == 0 and out.split() else 0
        handle.set_progress(message="archiving {}".format(name),
                            bytes_total=total)
        # Write to .part and rename on success: a half-written archive must
        # never be listed as a restorable backup.
        part = out_path + ".part"
        argv = ["tar", "--numeric-owner", "-czf", part, "-C", parent, name]
        rc = handle.watch_subprocess(argv)
        if rc != 0:
            for junk in (part,):
                try:
                    os.unlink(junk)
                except OSError:
                    pass
            raise RuntimeError("tar exited {}".format(rc))
        os.replace(part, out_path)
        handle.set_progress(pct=100, message="backed up",
                            bytes_done=os.path.getsize(out_path))

    return nas_jobs.submit(
        "lxc_backup",
        "LXC backup: {}".format(name),
        "LXC 备份：{}".format(name),
        runner, meta={"container": name, "file": out_name},
        exclusive_key="lxc:" + name)


def restore_backup(entry, newname=None):
    """Restore a backup, optionally under a new name. Never overwrites a
    live container: the target must not exist. Returns (job_id, None) or
    (None, {"code": ...})."""
    if not entry or not _BACKUP_RE.match(entry):
        return None, {"code": "bad_name"}
    src_name = _BACKUP_RE.match(entry).group(1)
    target = newname or src_name
    if not NAME_RE.match(target):
        return None, {"code": "bad_name"}
    archive = _find_backup(entry)
    if not archive:
        return None, {"code": "not_found"}
    base = lxcpath()
    if os.path.lexists(os.path.join(base, target)):
        return None, {"code": "exists"}
    if not _which("tar"):
        return None, {"code": "no_tool", "detail": "tar"}

    def runner(handle):
        handle.set_progress(message="restoring {}".format(target),
                            bytes_total=os.path.getsize(archive))
        dest = os.path.join(base, target)
        # Extract straight to the destination, with --strip-components=1 to
        # drop the archive's original top-level directory. The obvious
        # alternative — unpack to a scratch dir and rename the tree in — fails
        # here: GNU tar restores directory mtimes at the end and aborts with
        # "Directory renamed before its status could be extracted" if anything
        # touches the tree meanwhile, and the container list polls lxcpath
        # every few seconds. Extracting in place avoids the race entirely.
        ok = False
        try:
            os.makedirs(dest, exist_ok=True)
            rc = handle.watch_subprocess(
                ["tar", "--numeric-owner", "-xpzf", archive,
                 "--strip-components=1", "-C", dest])
            if rc != 0:
                raise RuntimeError("tar exited {}".format(rc))
            if not os.path.isdir(os.path.join(dest, "rootfs")):
                raise RuntimeError("archive has no rootfs")
            # Restoring under a new name is the same config surgery as a
            # clone: paths, uts name and a fresh MAC so the restored copy can
            # run beside the original.
            if target != src_name:
                cfg_path = os.path.join(dest, "config")
                body = _read_file(cfg_path)
                if body:
                    _write_file(cfg_path, rewrite_clone_config(
                        body, base, src_name, target))
                rootfs = os.path.join(dest, "rootfs")
                if os.path.isdir(rootfs):
                    _substitute_rootfs_placeholders(rootfs, target)
            ok = True
        finally:
            # A half-extracted container would show up as BROKEN and block
            # the retry with "exists".
            if not ok:
                _run(["rm", "-rf", dest], timeout=300)
        ensure_vmids([target])
        handle.set_progress(pct=100, message="restored")

    return nas_jobs.submit(
        "lxc_restore",
        "LXC restore: {} → {}".format(entry, target),
        "LXC 恢复：{} → {}".format(entry, target),
        runner, meta={"container": target, "file": entry},
        exclusive_key="lxc:" + target)


# ── per-container disk move (job) ────────────────────────────────────────────

def move_container(name, dest_base):
    """Move ONE container's disk to another storage volume (Proxmox's "move
    volume"). Offline only. The container keeps its name; its lxcpath entry
    becomes a symlink to the new location — or a real directory again when
    the destination IS the default storage. Returns (job_id, None) or
    (None, {"code": ...})."""
    if not NAME_RE.match(name or ""):
        return None, {"code": "bad_name"}
    if not dest_base or not dest_base.startswith("/") or ".." in dest_base:
        return None, {"code": "bad_path"}
    dest_base = os.path.normpath(dest_base)
    base = lxcpath()
    link = os.path.join(base, name)
    if not os.path.isdir(link):
        return None, {"code": "not_found"}
    if container_states().get(name) == "RUNNING":
        return None, {"code": "running"}
    src_real = os.path.realpath(link)
    if os.path.dirname(src_real) == dest_base:
        return None, {"code": "same_path"}
    to_default = dest_base == os.path.realpath(base)
    if not to_default and not fs_can_host_containers(dest_base):
        return None, {"code": "bad_fs", "detail": _fstype_for(dest_base)}
    dst_final = os.path.join(dest_base, name)
    if dst_final != link and os.path.lexists(dst_final):
        return None, {"code": "exists"}
    if not _which("rsync"):
        return None, {"code": "no_rsync"}

    def runner(handle):
        os.makedirs(dest_base, exist_ok=True)
        dst_tmp = dst_final + ".moving"
        if os.path.lexists(dst_tmp):
            _run(["rm", "-rf", dst_tmp], timeout=300)
        rc, out, _ = _run(["du", "-sk", src_real], timeout=300)
        total = int(out.split()[0]) * 1024 if rc == 0 and out.split() else 0
        handle.set_progress(message="copying rootfs", bytes_total=total)
        rc = handle.watch_subprocess(
            ["rsync", "-a", "--numeric-ids", src_real + "/", dst_tmp + "/"])
        if rc != 0:
            _run(["rm", "-rf", dst_tmp], timeout=300)
            raise RuntimeError("rsync exited {}".format(rc))
        cfg_path = os.path.join(dst_tmp, "config")
        body = _read_file(cfg_path)
        if body:
            fixed = rewrite_rootfs_paths(body, src_real, dst_final)
            if fixed != body:
                with open(cfg_path, "w") as f:
                    f.write(fixed)
        handle.set_progress(message="swapping in new copy")
        if os.path.islink(link):
            os.unlink(link)
        os.rename(dst_tmp, dst_final)
        if src_real != dst_final and os.path.isdir(src_real):
            handle.set_progress(message="removing old copy")
            rc = handle.watch_subprocess(["rm", "-rf", src_real])
            if rc != 0:
                raise RuntimeError("cleanup of {} failed".format(src_real))
        if dst_final != link and not os.path.lexists(link):
            os.symlink(dst_final, link)
        _size_cache.pop(name, None)
        # The container's location just changed: refresh both records before
        # the job reports done, or a flash before the next boot would send
        # recovery to the old path.
        _sync_quiet()
        handle.set_progress(pct=100, message="disk moved")

    return nas_jobs.submit(
        "lxc_move",
        "LXC disk move: {} → {}".format(name, dest_base),
        "LXC 磁盘迁移：{} → {}".format(name, dest_base),
        runner, meta={"container": name, "to": dest_base},
        exclusive_key="lxc:" + name)


# ── storage move (job) ───────────────────────────────────────────────────────

def move_storage(new_path, keep_old=False):
    """Move lxcpath (and every container) to new_path. Offline only."""
    if not new_path or not new_path.startswith("/") or ".." in new_path:
        return None, {"code": "bad_path"}
    new_path = os.path.normpath(new_path)
    old_path = lxcpath()
    if new_path == old_path:
        return None, {"code": "same_path"}
    if not fs_can_host_containers(new_path):
        return None, {"code": "bad_fs", "detail": _fstype_for(new_path)}
    if any(s == "RUNNING" for s in container_states().values()):
        return None, {"code": "running"}
    if not _which("rsync"):
        return None, {"code": "no_rsync"}

    def runner(handle):
        os.makedirs(new_path, exist_ok=True)
        if os.path.isdir(old_path) and os.listdir(old_path):
            rc, out, _ = _run(["du", "-sk", old_path], timeout=300)
            total = int(out.split()[0]) * 1024 if rc == 0 and out.split() \
                else 0
            handle.set_progress(message="copying containers",
                                bytes_total=total)
            rc = handle.watch_subprocess(
                ["rsync", "-a", "--numeric-ids",
                 old_path.rstrip("/") + "/", new_path.rstrip("/") + "/"])
            if rc != 0:
                raise RuntimeError("rsync exited {}".format(rc))
        handle.set_progress(message="rewriting configs")
        for entry in sorted(os.listdir(new_path)) \
                if os.path.isdir(new_path) else []:
            cfg = os.path.join(new_path, entry, "config")
            if os.path.isfile(cfg):
                body = _read_file(cfg)
                fixed = rewrite_rootfs_paths(body, old_path, new_path)
                if fixed != body:
                    with open(cfg, "w") as f:
                        f.write(fixed)
        with open(LXC_CONF, "w") as f:
            f.write(rewrite_lxcpath(_read_file(LXC_CONF), new_path))
        # rpcd's lxc plugin resolves lxcpath at load; restart to pick it up.
        _run(["/etc/init.d/rpcd", "restart"], timeout=30)
        if not keep_old and os.path.isdir(old_path):
            handle.set_progress(message="removing old copy")
            rc = handle.watch_subprocess(["rm", "-rf", old_path])
            if rc != 0:
                raise RuntimeError("cleanup of {} failed".format(old_path))
        # Containers that were real directories under the old lxcpath are
        # real directories under the new one, so their per-container records
        # have to be cleared and the new volume given its manifest.
        _sync_quiet()
        handle.set_progress(pct=100, message="storage moved")

    return nas_jobs.submit(
        "lxc_storage",
        "LXC storage move: {} → {}".format(old_path, new_path),
        "LXC 存储迁移：{} → {}".format(old_path, new_path),
        runner, meta={"from": old_path, "to": new_path},
        exclusive_key="lxc-storage", cancellable=False)


# ── migration (Proxmox-style offline push) ───────────────────────────────────

def _ssh_base(host, port, user):
    return ["ssh", "-i", SSH_KEY, "-p", str(port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            "{}@{}".format(user, host)]


def _valid_target(host, port, user):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not HOST_RE.match(host or "") or not USER_RE.match(user or "") \
            or not 0 < port < 65536:
        return None
    return host, port, user


def test_target(host, port, user):
    """Probe a migration target over the dedicated key: reachability, tar,
    remote lxcpath, whether ubus/lxc-start exist to start after migrate."""
    tgt = _valid_target(host, port, user)
    if tgt is None:
        return {"code": "bad_target"}
    if not ssh_key_ready():
        return {"code": "no_key"}
    host, port, user = tgt
    probe = ("echo PCAT_OK; command -v tar >/dev/null && echo HAS_TAR; "
             "ubus list lxc >/dev/null 2>&1 && echo HAS_UBUS_LXC; "
             "command -v lxc-start >/dev/null && echo HAS_LXC_START; "
             "grep -s lxc.lxcpath /etc/lxc/lxc.conf")
    rc, out, err = _run(_ssh_base(host, port, user) + [probe], timeout=25)
    if rc != 0 or "PCAT_OK" not in out:
        return {"code": "unreachable", "detail": (err or out).strip()[-300:]}
    remote_path = DEFAULT_LXCPATH
    m = re.search(r"lxc\.lxcpath\s*=\s*(\S+)", out)
    if m:
        remote_path = m.group(1)
    return {
        "code": "ok",
        "tar": "HAS_TAR" in out,
        "ubus_lxc": "HAS_UBUS_LXC" in out,
        "lxc_start": "HAS_LXC_START" in out,
        "remote_lxcpath": remote_path,
    }


def push_key(host, port, user, password):
    """One-time key install driving ssh through a pty with the user's
    password (no sshpass on the image). Appends to both authorized_keys
    locations so OpenWrt (dropbear) and stock-linux targets both work."""
    tgt = _valid_target(host, port, user)
    if tgt is None:
        return {"code": "bad_target"}
    if not ssh_key_ready():
        keyres = fix_sshkey()
        if keyres.get("code") != "ok":
            return keyres
    host, port, user = tgt
    pub = ssh_pubkey()
    remote = ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
              "grep -qsF {k} ~/.ssh/authorized_keys || "
              "echo {k} >> ~/.ssh/authorized_keys; "
              "if [ -d /etc/dropbear ]; then "
              "grep -qsF {k} /etc/dropbear/authorized_keys 2>/dev/null || "
              "echo {k} >> /etc/dropbear/authorized_keys; fi; "
              "echo PCAT_KEY_OK").format(k=shlex.quote(pub))
    argv = ["ssh", "-p", str(port),
            "-o", "ConnectTimeout=10",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "StrictHostKeyChecking=accept-new",
            "{}@{}".format(user, host), remote]

    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)

    output = b""
    sent = False
    deadline = time.time() + 30
    try:
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            low = output.lower()
            if not sent and (b"password:" in low or b"password for" in low):
                os.write(fd, (password + "\n").encode())
                sent = True
            if b"PCAT_KEY_OK" in output:
                break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            _, status = os.waitpid(pid, 0)
        except ChildProcessError:
            status = -1

    if b"PCAT_KEY_OK" not in output:
        tail = output.decode("utf-8", "replace").strip()[-300:]
        if not sent:
            return {"code": "no_password_prompt", "detail": tail}
        return {"code": "auth_failed", "detail": tail}
    # Verify the key path end to end.
    check = test_target(host, port, user)
    if check.get("code") != "ok":
        return {"code": "key_not_working", "detail": check.get("detail", "")}
    return {"code": "ok"}


def migrate(name, host, port, user, remote_path=None, mode="copy",
            start_after=False):
    """Offline migration job: stop → tar|ssh stream with byte progress →
    rewrite remote rootfs path → optional remote start → (move) destroy
    source. Source is only destroyed after the pipeline exits clean."""
    if not NAME_RE.match(name or ""):
        return None, {"code": "bad_name"}
    tgt = _valid_target(host, port, user)
    if tgt is None:
        return None, {"code": "bad_target"}
    host, port, user = tgt
    if mode not in ("copy", "move"):
        return None, {"code": "bad_mode"}
    if not ssh_key_ready():
        return None, {"code": "no_key"}
    base = lxcpath()
    cdir = os.path.join(base, name)
    if not os.path.isdir(cdir):
        return None, {"code": "not_found"}
    rpath = remote_path or DEFAULT_LXCPATH
    if not rpath.startswith("/") or ".." in rpath:
        return None, {"code": "bad_path"}
    rpath = os.path.normpath(rpath)

    ssh_argv = _ssh_base(host, port, user)

    def rsh(cmd, timeout=30):
        return _run(ssh_argv + [cmd], timeout=timeout)

    def runner(handle):
        # 1. stop if running (graceful, then hard)
        if container_states().get(name) == "RUNNING":
            handle.set_progress(message="stopping container")
            _ubus("shutdown", {"name": name, "timeout": 30}, timeout=40)
            if container_states().get(name) == "RUNNING":
                _ubus("stop", {"name": name}, timeout=30)
            if container_states().get(name) == "RUNNING":
                raise RuntimeError("could not stop container")
        handle.check_cancel()

        # 2. remote sanity: not already there, dir ready
        rc, out, err = rsh(
            "test -e {} && echo EXISTS; mkdir -p {} && echo DIR_OK".format(
                shlex.quote(rpath + "/" + name), shlex.quote(rpath)))
        if "EXISTS" in out:
            raise RuntimeError("target already has a container named " + name)
        if rc != 0 or "DIR_OK" not in out:
            raise RuntimeError("cannot prepare remote dir: " +
                               (err or out).strip()[-200:])

        # 3. size for progress (resolve the storage symlink if any — tar
        # must archive the real tree, not a one-line link)
        real_dir = os.path.realpath(cdir)
        real_parent = os.path.dirname(real_dir)
        rc, out, _ = _run(["du", "-sk", real_dir], timeout=300)
        total = int(out.split()[0]) * 1024 if rc == 0 and out.split() else 0
        handle.set_progress(message="transferring", bytes_total=total)

        # 4. tar | count | ssh tar -x
        tar_create = ["tar", "-C", real_parent, "-cf", "-", name]
        rc_probe, ver, _ = _run(["tar", "--version"], timeout=5)
        if "GNU tar" in ver:
            tar_create = ["tar", "-C", real_parent, "--numeric-owner",
                          "-cf", "-", name]
        remote_cmd = "tar -C {} -xpf -".format(shlex.quote(rpath))
        # stderr to a spooled file, not a pipe: an undrained pipe would
        # deadlock the transfer if the remote got chatty.
        import tempfile
        err_file = tempfile.TemporaryFile()
        p_tar = subprocess.Popen(tar_create, stdout=subprocess.PIPE)
        p_ssh = subprocess.Popen(ssh_argv + [remote_cmd],
                                 stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL,
                                 stderr=err_file)
        done = 0
        try:
            while True:
                if handle.cancelled:
                    p_tar.terminate()
                    p_ssh.terminate()
                    raise nas_jobs.JobCancelled()
                chunk = p_tar.stdout.read(1024 * 512)
                if not chunk:
                    break
                p_ssh.stdin.write(chunk)
                done += len(chunk)
                handle.set_progress(
                    bytes_done=done,
                    pct=min(99, done * 100 // total) if total else None)
            p_ssh.stdin.close()
            rc_tar = p_tar.wait(timeout=120)
            rc_ssh = p_ssh.wait(timeout=600)
        except (BrokenPipeError, OSError) as e:
            p_tar.kill()
            p_ssh.kill()
            raise RuntimeError("transfer pipe broke: {}".format(e))
        if rc_tar != 0 or rc_ssh != 0:
            err_file.seek(0)
            err_tail = err_file.read().decode(
                "utf-8", "replace").strip()[-200:]
            err_file.close()
            raise RuntimeError("transfer failed (tar={}, ssh={}) {}".format(
                rc_tar, rc_ssh, err_tail))
        err_file.close()

        # 5. rewrite absolute paths in the remote config (the config embeds
        # the REAL local directory, which is what must be replaced)
        if rpath != real_parent:
            handle.set_progress(message="fixing remote config")
            sed = "sed -i 's|{}|{}|g' {}".format(
                real_parent.rstrip("/") + "/" + name,
                rpath.rstrip("/") + "/" + name,
                shlex.quote(rpath + "/" + name + "/config"))
            rc, out, err = rsh(sed)
            if rc != 0:
                raise RuntimeError("remote config rewrite failed: " +
                                   (err or out).strip()[-200:])

        # 6. optional remote start (ubus first, lxc-start fallback)
        if start_after:
            handle.set_progress(message="starting on target")
            start_cmd = (
                "if ubus list lxc >/dev/null 2>&1; then "
                "ubus -t 60 call lxc start '{{\"name\":\"{n}\"}}'; "
                "elif command -v lxc-start >/dev/null; then "
                "lxc-start -n {n}; else echo NO_STARTER; fi; sleep 2; "
                "if ubus list lxc >/dev/null 2>&1; then "
                "ubus call lxc list; fi").format(n=name)
            rc, out, err = rsh(start_cmd, timeout=90)
            if "NO_STARTER" in out:
                handle.log("target has no way to start containers")
            elif '"{}"'.format(name) in out and "RUNNING" not in out:
                handle.log("warning: container did not reach RUNNING "
                           "on target")

        # 7. move: destroy source only now
        if mode == "move":
            handle.set_progress(message="removing source copy")
            res = container_action("destroy", name)
            if res["code"] != "ok":
                raise RuntimeError(
                    "transferred OK but source destroy failed: " +
                    res.get("detail", res["code"]))
        handle.set_progress(pct=100, message="migration complete")

    return nas_jobs.submit(
        "lxc_migrate",
        "LXC migrate: {} → {} ({})".format(name, host, mode),
        "LXC 迁移：{} → {}（{}）".format(
            name, host, "移动" if mode == "move" else "复制"),
        runner, meta={"container": name, "host": host, "mode": mode},
        exclusive_key="lxc:" + name)
