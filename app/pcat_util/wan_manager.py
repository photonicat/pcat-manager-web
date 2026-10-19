"""
Metric-based multi-WAN priority manager (network_mode == "priority").

Replaces both the fixed network modes (auto/eth_only/cell_only/smart_wan) and
mwan3 with one priority table of upstream slots in a fixed failover order
(1 WAN > 2 WiFi > 3 Cellular); the user only enables/disables each slot:

    wan       WAN NIC   (eth0)          network.wan / network.wan6
    wifi      WiFi upstream STA          network.wan_wifi / wan_wifi_v6
    lan       LAN NIC as upstream (eth1) network.wan_lan / wan_lan_v6
    cellular  5G/LTE modem               network.wwan_5g / wwan_lte / wwan_fm350

Selection is done entirely by route metrics: slot position p in the table maps
to metric 10*(p+1) (v6 = +1, extra family members +2 each), and the kernel
routes out the lowest-metric default route.  No userspace policy engine sits
in the datapath, so a dead health-checker can never blackhole egress the way
mwan3's fwmark rules do when its only tracked interface goes down — which is
why mwan3 is stopped and disabled outright (see disable_mwan3()).

The metric ladder is not ours alone to write: older fm350-mm builds rewrote
network.wwan_fm350.metric=14 on every data call (stealing egress from WiFi
at metric 20). Current dialers preserve the ladder metric, but reassert_metrics()
still corrects any out-of-band drift (legacy binaries, other dialers) on the
fast link pass (~5s) as well as the 30s probe keeper.

Health checking generalizes the proven wifi_wan probe/demote pattern: each
enabled slot with a live interface is probed for real internet (bound to its
netdev, so the probe tests that uplink regardless of the default route); after
two consecutive bad rounds the slot's metrics are demoted by +1000 so egress
falls to the next slot, and restored as soon as the probe succeeds again.  The
wifi slot keeps its richer handling (captive-portal login hook, scan/rejoin)
in wifi_wan.reconcile(); this module only reports its state.

DNS failover is made seamless by dnsmasq's all_servers option: every
upstream's resolvers are queried in parallel and the first answer wins, so
resolvers learned from a dead upstream time out silently instead of stalling
lookups (see ensure_dns_resilience()).
"""

import json
import os
import subprocess
import threading
import time

from pcat_util.openwrt import (
    ubus_call,
    ubus_call_strict,
    uci_get,
    uci_set,
    uci_commit,
    uci_add_list,
    uci_delete,
    uci_show_dict,
)
from pcat_util import pcat_logger

SLOTS = ("wan", "wifi", "cellular")

# UCI logical interfaces per slot as (ipv4, ipv6) pairs; multi-modem slots list
# every family member (only the one matching the fitted modem ever comes up).
SLOT_IFACES = {
    "wan": (("wan", "wan6"),),
    "wifi": (("wan_wifi", "wan_wifi_v6"),),
    "cellular": (("wwan_5g", "wwan_5g_v6"),
                 ("wwan_lte", "wwan_lte_v6"),
                 ("wwan_fm350", "wwan_fm350_v6")),
}

# Netdevs that indicate the slot's hardware is present.
SLOT_NETDEVS = {
    "wan": ("eth0",),
    "cellular": ("usb0", "wwan0", "eth2"),
}

METRIC_STEP = 10          # per-position spacing; family members fit inside
METRIC_DEMOTED = 1000     # added to a slot's metrics while its uplink is dead

# Metrics the legacy (non-priority) modes use for the wifi slot; kept so
# upgrading devices behave exactly as before until the user saves the table.
LEGACY_WIFI_METRICS = (5, 6, 50)

_HEALTH_FLAP_THRESHOLD = 2  # consecutive bad probe rounds before demotion

# A healthy slot that is not the active egress carries no traffic, so the 30s
# HTTP health probe over its netdev (for cellular, a modem-radio wake) only
# refreshes a status badge. reconcile() probes the active slot and any demoted
# slot every pass (failover and restore ride on those), but backs a healthy
# standby off to this interval. Its metrics stay at preferred, so if the active
# uplink dies egress moves onto it instantly and it is then probed every pass.
_STANDBY_PROBE_INTERVAL = 120  # seconds

# slot -> {"result": online/portal/offline/down, "bad_streak": n,
#          "demoted": bool, "time": ts, "portal_url": str}
_health = {}

# Guards every read-modify-write of _health and of the per-slot route metrics.
# apply() (Flask save/boot), reconcile() (background keeper), the fast carrier
# pass reconcile_link_state() (App tick), reassert_metrics() and status() all
# touch this shared state from different threads. Reentrant so the nested
# helpers (_set_slot_demoted, _mark_slot_down, reassert_metrics) can be called
# by a holder. reconcile() deliberately RELEASES this around its blocking HTTP
# probe so the fast pass and status() are never stalled behind a probe round.
_lock = threading.RLock()

# Single-flight for reconcile(): a round can take tens of seconds (one blocking
# probe per slot), so a second overlapping call would double-probe and race.
# Acquired non-blocking; an overlapping call returns "busy" immediately.
_reconcile_active = threading.Lock()

_LAN_PORTS_BACKUP_KEY = "wan_lan_ports_backup"

_mwan3_handled = False


# ── priority table (stored in the user config) ───────────────────────────────

# Slots enabled on a fresh install. The two always-present uplinks; the WiFi
# upstream is opt-in (joining an upstream network turns its slot on).
_DEFAULT_ENABLED = ("wan", "cellular")


def default_priority():
    """Fresh table, in the fixed SLOTS order.

    Derived from SLOTS rather than restating it: the failover order now lives
    in exactly one place. priority_from_mode() returns this without going
    through _normalize(), so a hand-written order here could silently disagree
    with the order _normalize() produces for every other caller."""
    return [{"id": slot, "enabled": slot in _DEFAULT_ENABLED} for slot in SLOTS]


def _normalize(items):
    """Sanitize a priority list. The slot order is fixed (1 WAN > 2 WiFi >
    3 Cellular, i.e. SLOTS); only each slot's enabled flag is taken from the
    input, so a stored/posted list in any order snaps back to the fixed
    sequence. Slots missing from the input come out disabled."""
    enabled = {}
    for item in items or []:
        slot = (item or {}).get("id")
        if slot in SLOTS and slot not in enabled:
            enabled[slot] = bool(item.get("enabled"))
    return [{"id": slot, "enabled": enabled.get(slot, False)} for slot in SLOTS]


def priority_from_mode(mode):
    """Initial table for a device migrating from a legacy network mode.
    'smart_wan' is the retired mode that priority mode replaces; a device
    upgrading from it seeds a table with the wifi slot enabled (the equivalent
    eth -> wifi -> cellular chain it used to hardcode). The slot order is
    always the fixed sequence; a mode only picks which slots are enabled."""
    order = default_priority()
    by_id = {e["id"]: e for e in order}
    if mode == "eth_only":
        by_id["cellular"]["enabled"] = False
    elif mode == "cell_only":
        by_id["wan"]["enabled"] = False
    elif mode == "smart_wan":
        by_id["wifi"]["enabled"] = True
    elif mode == "all_off":
        for entry in order:
            entry["enabled"] = False
    return order


def get_priority():
    import pcat_config
    stored = pcat_config.get_user_config_key("wan_priority", None)
    if isinstance(stored, list) and stored:
        return _normalize(stored)
    # Seed from the *raw* stored mode so a legacy 'smart_wan' device (which
    # get_network_mode() now reports as 'priority') still gets its wifi slot
    # enabled on first read.
    raw_mode = pcat_config.get_user_config_key("network_mode", "auto")
    return priority_from_mode(raw_mode)


def is_slot_enabled(slot):
    return any(e["id"] == slot and e["enabled"] for e in get_priority())


def migrate_legacy_mode():
    """Fold a legacy fixed network mode onto the priority table, exactly once.

    The fixed modes ('auto', 'eth_only', 'cell_only', 'all_off' and the retired
    'smart_wan') are gone; priority_from_mode() maps each to the table that
    routes identically, so a box upgrading from any of them keeps the behaviour
    it had and gains the new logic -- metric ladder, health keeper, seamless
    DNS across failover.

    This used to be gated on the mode already reading as 'priority', which only
    ever caught the smart_wan boxes: every device on the default 'auto' (i.e.
    most of the fleet) stayed on the legacy code path forever, with no ladder,
    no failover keeper, and whatever stale metrics were last written -- exactly
    the state a device is found in with reconcile() reporting 'not_priority'.

    The stored table is its own marker (apply() writes it), so this is a no-op
    on every later boot and can never overwrite a table the user has edited.
    Returns the applied order string, or None when there was nothing to do.
    """
    import pcat_config
    if pcat_config.get_user_config_key("wan_priority", None) is not None:
        return None
    mode = pcat_config.get_user_config_key("network_mode", "auto")
    order = apply(get_priority())
    pcat_logger.logger.info(
        "wan_manager: migrated legacy mode '{}' onto the priority table "
        "({})".format(mode, order))
    return order


# ── metric scheme ────────────────────────────────────────────────────────────

def _slot_base(position):
    return METRIC_STEP * (position + 1)


def slot_metrics(slot, priority, demoted=False):
    """{uci_iface_name: metric} for one slot at its position in the table.
    demoted=True returns the +METRIC_DEMOTED variant used while the uplink
    is dead."""
    pos = next((i for i, e in enumerate(priority) if e["id"] == slot), None)
    if pos is None:
        return {}
    base = _slot_base(pos)
    offset = METRIC_DEMOTED if demoted else 0
    metrics = {}
    for i, (v4, v6) in enumerate(_slot_iface_pairs(slot)):
        metrics[v4] = base + 2 * i + offset
        metrics[v6] = base + 2 * i + 1 + offset
    return metrics


def metrics_for(priority):
    """{uci_iface_name: metric} for every slot interface, from table order."""
    metrics = {}
    for entry in priority:
        metrics.update(slot_metrics(entry["id"], priority))
    return metrics


def wifi_metrics():
    """(v4, v6, demoted_v4) metrics for the wifi slot. wifi_wan uses these so
    the STA plumbing follows the table in priority mode and keeps the historic
    5/6/50 values in the legacy modes."""
    import pcat_config
    if pcat_config.get_network_mode() != "priority":
        return LEGACY_WIFI_METRICS
    priority = get_priority()
    pos = next((i for i, e in enumerate(priority) if e["id"] == "wifi"), 1)
    base = _slot_base(pos)
    return (base, base + 1, base + METRIC_DEMOTED)


# ── mwan3 removal ────────────────────────────────────────────────────────────

def disable_mwan3():
    """Stop mwan3 and remove it from startup. Its stock config only tracks
    'wan' (eth0); when that link dies every policy resolves to 'unreachable'
    and its fwmark ip-rules blackhole ALL egress, defeating metric failover.
    Stopping it removes the fwmark rules from the datapath for good."""
    global _mwan3_handled
    if _mwan3_handled:
        return False
    _mwan3_handled = True
    if not os.path.exists("/etc/init.d/mwan3"):
        return False
    try:
        subprocess.run(["/etc/init.d/mwan3", "stop"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        subprocess.run(["/etc/init.d/mwan3", "disable"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        pcat_logger.logger.info("wan_manager: mwan3 stopped and disabled")
        return True
    except Exception as e:
        pcat_logger.logger.warning("wan_manager: mwan3 disable failed: {}".format(e))
        return False


# ── seamless DNS across failover ─────────────────────────────────────────────

def ensure_dns_resilience():
    """Set dnsmasq all_servers so every upstream's resolvers are queried in
    parallel and the first answer wins. Without it dnsmasq sticks to one
    upstream resolver, and after a failover that resolver (learned from the
    dead uplink) stalls every lookup until dnsmasq retries its way past it."""
    if uci_get("dhcp.@dnsmasq[0]") != "dnsmasq":
        return False
    if uci_get("dhcp.@dnsmasq[0].all_servers") == "1":
        return False
    uci_set("dhcp.@dnsmasq[0].all_servers=1")
    uci_commit("dhcp")
    try:
        subprocess.run(["/etc/init.d/dnsmasq", "reload"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception as e:
        pcat_logger.logger.warning("wan_manager: dnsmasq reload failed: {}".format(e))
    pcat_logger.logger.info("wan_manager: dnsmasq all_servers enabled")
    return True


# ── UCI plumbing ─────────────────────────────────────────────────────────────

def _iface_exists(iface):
    return uci_get("network.{}".format(iface)) == "interface"


# Protos that mean the user (or another tool) dials the modem through netifd
# instead of the vendor pcat-manager -> quectel-cm/fm350-mm chain. The stock
# config only ever uses dhcp/static on the cellular families, so an mbim/qmi
# proto there is a deliberate user override. Forcing such an interface up (or
# clearing its disabled flag) starts a second dialer on the SAME /dev/cdc-wdmN
# the vendor dialer owns -- on an RM500Q that fight presents as endless "mbim
# message timeout / PIN_FAILED / retry in 15s" and a cellular session that
# takes minutes to establish, if it ever does (forum: r7897 蜂窝启动时序报告).
# Such a family is fully hands-off: no disabled rewrite, no up/down nudge, no
# metric management. The user keeps netifd's own auto/metric semantics.
_USER_DIALER_PROTOS = ("mbim", "qmi")

# proto reads shell out to uci; cache the filtered list briefly so the 5s link
# pass does not pay 6 subprocess calls per tick. A proto edit takes effect on
# the next refresh (<=30s), which is faster than the user can retrigger dialing.
_DIALER_CACHE_TTL = 30
_dialer_cache = {"time": 0.0, "pairs": None}
_handsoff_logged = set()


def _cellular_iface_pairs():
    """SLOT_IFACES['cellular'] minus families the user re-pointed at a netifd
    dialer proto (see _USER_DIALER_PROTOS). Logged once per family per boot so
    a hands-off family shows up in the log instead of silently unmanaged."""
    now = time.time()
    if _dialer_cache["pairs"] is not None and \
            now - _dialer_cache["time"] < _DIALER_CACHE_TTL:
        return _dialer_cache["pairs"]
    pairs = []
    for v4, v6 in SLOT_IFACES["cellular"]:
        protos = (uci_get("network.{}.proto".format(v4)),
                  uci_get("network.{}.proto".format(v6)))
        if any(p in _USER_DIALER_PROTOS for p in protos):
            if v4 not in _handsoff_logged:
                _handsoff_logged.add(v4)
                pcat_logger.logger.info(
                    "wan_manager: {} uses a netifd dialer proto ({}); leaving "
                    "it to the user -- no enable/up/metric management".format(
                        v4, "/".join(p for p in protos if p)))
            continue
        pairs.append((v4, v6))
    _dialer_cache["time"] = now
    _dialer_cache["pairs"] = pairs
    return pairs


def _slot_iface_pairs(slot):
    """SLOT_IFACES[slot], with user-dialed cellular families filtered out.
    Use this in every path that WRITES interface state (disabled, metric,
    up/down); the read-only status paths keep the full list so a user-dialed
    interface still shows up in the panel."""
    if slot == "cellular":
        return _cellular_iface_pairs()
    return SLOT_IFACES[slot]


def _set_if_changed(key, value):
    if uci_get(key) == str(value):
        return False
    uci_set("{}={}".format(key, value))
    return True


def _set_iface_disabled(iface, disabled):
    if not _iface_exists(iface):
        return False
    return _set_if_changed("network.{}.disabled".format(iface),
                           "1" if disabled else "0")


def _network_reload():
    """Ask netifd to reload config; only interfaces whose config changed are
    restarted (a metric change restarts that interface briefly). If ubus can't
    be reached, fall back to the init script -- ubus_call_strict returns None
    only on an infrastructure failure (a successful reload returns {}), so this
    fallback actually runs now instead of being dead code."""
    result = ubus_call_strict("network", "reload")
    if result is None:
        try:
            subprocess.run(["/etc/init.d/network", "reload"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except Exception as e:
            pcat_logger.logger.warning("wan_manager: network reload failed: {}".format(e))


def _ifup(iface):
    try:
        subprocess.run(["ifup", iface], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=10)
    except Exception:
        pass


def _wan_zone_index():
    for i in range(20):
        name = uci_get("firewall.@zone[{}].name".format(i))
        if not name:
            return None
        if name == "wan":
            return i
    return None


def _ensure_firewall_wan_zone(nets):
    idx = _wan_zone_index()
    if idx is None:
        pcat_logger.logger.error("wan_manager: firewall wan zone not found")
        return False
    current = uci_get("firewall.@zone[{}].network".format(idx)).split()
    changed = False
    for net in nets:
        if net not in current:
            uci_add_list("firewall.@zone[{}].network={}".format(idx, net))
            changed = True
    if changed:
        uci_commit("firewall")
        try:
            subprocess.run(["/etc/init.d/firewall", "reload"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except Exception as e:
            pcat_logger.logger.warning("wan_manager: firewall reload failed: {}".format(e))
    return True


# Don't hammer the firewall if a reload doesn't cure the drift (broken zone
# config, fw4 erroring): one attempt, then back off and keep reporting.
_ZONE_RULESET_RELOAD_COOLDOWN = 300  # seconds
_zone_ruleset_last_reload = 0.0


def _zone_ruleset_missing():
    """Zone interfaces (per UCI) whose live L3 device is absent from the
    loaded fw4 ruleset, as ["iface(dev)", ...].

    fw4 resolves a zone's networks to concrete device names when it LOADS. A
    device that does not exist at that moment (a Wi-Fi STA that has not
    associated yet, or that re-associated after the AP bounced) leaves its
    interface in no zone at all: LAN egress hits the forward chain's default
    drop and is never masqueraded. Clients keep getting DNS answers (dnsmasq
    answers locally) while nothing routes, which reads as "internet broken,
    DNS fine". No netifd event marks the moment either -- a roam happens at
    layer 2 with the netdev up throughout, so ifup hooks never fire; the
    check has to be polled.

    UCI is deliberately the reference here, not `fw4 -q network`: the loaded
    ruleset is the thing under suspicion, so asking it whether an interface
    has a zone answers "no" in exactly the case this exists to repair."""
    try:
        proc = subprocess.run(["nft", "list", "table", "inet", "fw4"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10)
        ruleset = proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        return []  # no nftables here (dev box) -- nothing to check
    # Empty output means fw4 is not loaded at all (stopped, or mid-reload);
    # "repairing" on top of that would race the real load.
    if proc.returncode != 0 or not ruleset.strip():
        return []

    missing = []
    for i in range(20):
        if not uci_get("firewall.@zone[{}].name".format(i)):
            break
        for iface in uci_get("firewall.@zone[{}].network".format(i)).split():
            status = ubus_call_strict("network.interface.{}".format(iface),
                                      "status")
            if status is None or not status.get("up"):
                continue  # can't tell / down: no device that could be missing
            dev = status.get("l3_device") or status.get("device")
            if dev and '"{}"'.format(dev) not in ruleset:
                missing.append("{}({})".format(iface, dev))
    return missing


def check_zone_ruleset():
    """Reload the firewall when an up zone interface's device is missing from
    the running ruleset (see _zone_ruleset_missing for how it gets that way).
    Called from the reconcile keeper; firmware >= e6909b7e94 also runs an
    equivalent cron check on-device, but boxes that only take pcat-manager-web
    updates have no other repair path. Returns the drift found on entry."""
    global _zone_ruleset_last_reload
    missing = _zone_ruleset_missing()
    if not missing:
        return []
    now = time.time()
    if now - _zone_ruleset_last_reload < _ZONE_RULESET_RELOAD_COOLDOWN:
        return missing
    _zone_ruleset_last_reload = now
    pcat_logger.logger.info(
        "wan_manager: {} missing from running firewall ruleset, reloading".format(
            " ".join(missing)))
    try:
        subprocess.run(["/etc/init.d/firewall", "reload"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception as e:
        pcat_logger.logger.warning("wan_manager: firewall reload failed: {}".format(e))
        return missing
    still = _zone_ruleset_missing()
    if still:
        # The cause is upstream of us (zone config, fw4 error); the cooldown
        # keeps this from turning into a reload loop.
        pcat_logger.logger.warning(
            "wan_manager: still missing after firewall reload: {}".format(
                " ".join(still)))
    return missing


# ── legacy eth1-as-upstream cleanup ──────────────────────────────────────────

def _br_lan_device_index():
    for i in range(10):
        if uci_get("network.@device[{}]".format(i)) == "":
            return None
        if uci_get("network.@device[{}].name".format(i)) == "br-lan":
            return i
    return None


def _cleanup_lan_upstream():
    """The LAN NIC is no longer offered as an internet uplink; remove any
    wan_lan config an earlier build created and give eth1 back to br-lan."""
    import pcat_config
    changed = False
    had_ifaces = False
    for iface in ("wan_lan", "wan_lan_v6"):
        if uci_get("network.{}".format(iface)):
            uci_delete("network.{}".format(iface))
            had_ifaces = changed = True
    if had_ifaces:
        idx = _wan_zone_index()
        if idx is not None:
            for net in ("wan_lan", "wan_lan_v6"):
                try:
                    subprocess.run(
                        ["uci", "del_list",
                         "firewall.@zone[{}].network={}".format(idx, net)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                except Exception:
                    pass
            uci_commit("firewall")
    backup = pcat_config.get_user_config_key(_LAN_PORTS_BACKUP_KEY)
    if backup:
        idx = _br_lan_device_index()
        if idx is not None:
            ports_key = "network.@device[{}].ports".format(idx)
            if uci_get(ports_key) != backup:
                uci_set("{}={}".format(ports_key, backup))
                changed = True
        pcat_config.set_user_config_key(_LAN_PORTS_BACKUP_KEY, None)
    return changed


# ── apply the table ──────────────────────────────────────────────────────────

def apply(priority=None, wifi_radio=None):
    """Persist and enact the priority table: metric ladder, per-slot
    enable/disable, WiFi STA relay, LAN-as-upstream bridge move, mwan3 off,
    resilient DNS. Idempotent — netifd only restarts interfaces whose config
    actually changed. Returns a short summary string for logging."""
    import pcat_config
    from pcat_util import wifi_wan

    # Serialize the whole enact against the reconcile keeper and the fast
    # carrier pass: they mutate the same _health verdicts and route metrics.
    # Without this, a save that pops a slot's verdict could interleave with a
    # reconcile writing a stale verdict back, pinning a healthy uplink demoted
    # below cellular until reboot. reconcile() releases this lock around its
    # blocking probe, so holding it here does not stall failover.
    with _lock:
        # Read the outgoing table before it is overwritten: a slot whose enable
        # flag is unchanged keeps its health verdict (see below).
        prev_enabled = {e["id"]: e["enabled"] for e in get_priority()}

        if priority is not None:
            priority = _normalize(priority)
            pcat_config.set_user_config_key("wan_priority", priority)
        else:
            priority = get_priority()
        pcat_config.set_network_mode("priority")

        disable_mwan3()
        ensure_dns_resilience()

        if wifi_radio:
            try:
                wifi_wan.set_sta_radio(wifi_radio)
            except Exception as e:
                pcat_logger.logger.error("wan_manager: radio switch failed: {}".format(e))

        enabled = {e["id"]: e["enabled"] for e in priority}
        changed = False

        # A login window on a slot the user just switched off has nothing to
        # hold up any more; close it before the metric writes below so it
        # cannot pin a disabled uplink at its preferred metric.
        held = _portal_login["slot"]
        if held and not enabled.get(held):
            end_portal_login("connection switched off")

        # A demotion is a *measurement* (this uplink was probed and found dead),
        # not configuration, so toggling one slot must not forgive every other
        # slot. Clearing all of it here meant flipping, say, the wifi slot handed
        # a known-dead cellular link its preferred metric back and let it carry
        # traffic again until the keeper re-condemned it -- up to ~60s (two probe
        # rounds) for a link that is up but has no internet.
        #
        # Drop the verdict only for slots whose enable flag actually changed:
        # their interface is about to come up or go away, so any prior verdict is
        # stale. The rest keep theirs, and get their demoted metric written below.
        # The wifi slot is not in _health at all -- wifi_wan owns wan_wifi's metric
        # end to end (portal demotion, roam, rejoin) and re-demotes on its own
        # keeper, so duplicating that state machine here would only fight it.
        for slot in list(_health):
            if prev_enabled.get(slot) != enabled.get(slot):
                _health.pop(slot, None)

        metrics = {}
        for entry in priority:
            slot = entry["id"]
            if slot == "wifi":
                # wifi_wan owns wan_wifi's metric end to end: the initial value
                # (via _ensure_network_ifaces) and every portal demotion/restore
                # on its own keeper. Writing it here stomped a live portal
                # demotion on *every* panel save, bouncing egress back onto a
                # captive-portal-locked WiFi for a full probe cycle (~20-30s)
                # until wifi_wan re-demoted it. Leave it alone -- reassert_metrics()
                # already skips the wifi slot for exactly this reason.
                continue
            still_demoted = (entry["enabled"]
                             and bool((_health.get(slot) or {}).get("demoted")))
            metrics.update(slot_metrics(slot, priority, still_demoted))

        # WiFi slot: the STA relay (and AP parking) is wifi_wan's job; it reads
        # our metrics via wifi_metrics(). No-op until an upstream is configured.
        #
        # The relay needs two live radios (one joins upstream, the other keeps
        # serving this device's hotspot; with one radio it would park the only
        # AP). That check must NOT be persisted into the table: at boot apply()
        # runs before the WiFi drivers have registered their phys, and writing
        # the forced 'off' to the user config made the wifi slot forget itself
        # on every reboot. During a radio deficit the STA state is left alone —
        # an already-enabled STA simply associates once its radio appears — and
        # the wifi_wan keeper parks it only if the deficit persists (a really
        # pulled PCIe card), so the user's slot choice survives throughout.
        try:
            if enabled["wifi"] and not relay_radios_ready():
                pcat_logger.logger.info(
                    "wan_manager: wifi upstream waiting for two live radios")
            else:
                wifi_wan.set_sta_enabled(enabled["wifi"])
        except Exception as e:
            pcat_logger.logger.error("wan_manager: wifi slot toggle failed: {}".format(e))

        changed |= _cleanup_lan_upstream()

        for iface, metric in metrics.items():
            if _iface_exists(iface):
                changed |= _set_if_changed("network.{}.metric".format(iface), metric)

        for v4, v6 in SLOT_IFACES["wan"]:
            changed |= _set_iface_disabled(v4, not enabled["wan"])
            changed |= _set_iface_disabled(v6, not enabled["wan"])
        for v4, v6 in _slot_iface_pairs("cellular"):
            changed |= _set_iface_disabled(v4, not enabled["cellular"])
            changed |= _set_iface_disabled(v6, not enabled["cellular"])

        # Surviving verdicts above already had their demoted metric written, so
        # _health and UCI agree here; nothing left to reset.
        if changed:
            uci_commit("network")
            _network_reload()
            # The modem interfaces have auto=0 in the stock config, so a reload
            # won't start them; nudge them explicitly like the legacy toggle did.
            for v4, v6 in _slot_iface_pairs("cellular"):
                action = "up" if enabled["cellular"] else "down"
                ubus_call("network.interface.{}".format(v4), action)
                ubus_call("network.interface.{}".format(v6), action)

        order = ">".join("{}{}".format(e["id"], "" if e["enabled"] else "(off)")
                         for e in priority)
        pcat_logger.logger.info(
            "wan_manager: applied priority {} (changed={})".format(order, changed))
        return order


def set_slot_enabled(slot, enable):
    """Flip one slot's enable flag and re-apply (e.g. joining an upstream
    WiFi from the panel enables the wifi slot)."""
    priority = get_priority()
    for entry in priority:
        if entry["id"] == slot:
            if entry["enabled"] == bool(enable):
                return False
            entry["enabled"] = bool(enable)
    apply(priority)
    return True


_WIFI_GUARD_REPAIR_MARKER = "wifi_slot_guard_repaired"


def _repair_wifi_slot_disable():
    """One-shot heal for tables the old two-radio guard corrupted.

    An earlier apply() force-disabled the wifi slot and PERSISTED it whenever
    fewer than two live radios were visible — which at boot meant "always"
    (the drivers register their phys after this process starts), so every
    reboot silently erased the user's wifi-upstream enable. The wrong value
    is indistinguishable from a deliberate disable, so detection leans on the
    damage signature instead: priority mode, a stored table with the wifi
    slot off, and an upstream STA still configured in wireless UCI (a real
    "forget upstream" deletes that section; the guard never did). Runs once
    ever per device — the marker is set on the first startup regardless of
    outcome — so a slot the user turns off after this boot stays off.
    Returns True when the slot was re-enabled."""
    import pcat_config
    from pcat_util import wifi_wan
    if pcat_config.get_user_config_key(_WIFI_GUARD_REPAIR_MARKER):
        return False
    pcat_config.set_user_config_key(_WIFI_GUARD_REPAIR_MARKER, True)
    if pcat_config.get_network_mode() != "priority":
        return False
    stored = pcat_config.get_user_config_key("wan_priority", None)
    if not isinstance(stored, list) or not stored:
        return False  # nothing persisted -> nothing the guard could have hit
    priority = _normalize(stored)
    wifi = next(e for e in priority if e["id"] == "wifi")
    if wifi["enabled"]:
        return False
    if not wifi_wan.is_configured():
        return False  # no upstream set up (or deliberately forgotten)
    wifi["enabled"] = True
    pcat_config.set_user_config_key("wan_priority", priority)
    pcat_logger.logger.info(
        "wan_manager: wifi slot re-enabled (healing the persisted "
        "two-radio-guard disable from an earlier build)")
    return True


def startup():
    """Boot-time hook (called once from the App thread): always drop mwan3
    and harden DNS; in priority mode heal a table the old two-radio guard
    corrupted, then re-assert it (no-op reload when the persisted UCI state
    already matches)."""
    import pcat_config
    disable_mwan3()
    ensure_dns_resilience()
    try:
        _repair_wifi_slot_disable()
    except Exception as e:
        pcat_logger.logger.error("wan_manager: wifi slot repair failed: {}".format(e))
    if pcat_config.get_network_mode() == "priority":
        try:
            apply()
        except Exception as e:
            pcat_logger.logger.error("wan_manager: startup apply failed: {}".format(e))


# ── health checking / demotion ───────────────────────────────────────────────

def _netdev_carrier(dev):
    """True when /sys reports carrier=1. False on missing iface, no-carrier,
    or unreadable sysfs (common right as a cable is yanked)."""
    try:
        with open("/sys/class/net/{}/carrier".format(dev), "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return False


def _slot_link_up(slot):
    """Physical-link check for slots that have a fixed netdev. Returns True
    when the link is up or when we cannot tell (non-eth slots)."""
    if slot == "wan":
        if not os.path.exists("/sys/class/net/eth0"):
            return False
        return _netdev_carrier("eth0")
    return True


def _slot_l3dev(slot):
    """Live netdev of the slot's first up interface, None if it genuinely has no
    L3, or False if ubus was unreachable so its state is unknown. The caller
    must treat False as "don't know" -- demoting a live uplink because a ubus
    hiccup made every status read fail is exactly the false-down this guards."""
    ubus_ok = False
    for v4, _v6 in SLOT_IFACES[slot]:
        status = ubus_call_strict("network.interface.{}".format(v4), "status")
        if status is None:
            continue  # this interface's state could not be read (ubus hiccup)
        ubus_ok = True
        if status.get("up"):
            dev = status.get("l3_device") or status.get("device")
            if dev:
                return dev
    return None if ubus_ok else False


# ── manual captive-portal login window ───────────────────────────────────────
# A slot that probes 'portal' (hotel / airport / office login page) is demoted
# so egress falls to the next working uplink. Right for traffic, but it also
# means the login page can never be reached: every request a LAN browser makes
# leaves through the *other* uplink, and the portal only answers what arrives
# on its own link. Nobody can log in, so the slot stays portal-locked forever.
#
# The window is the way out. For _PORTAL_LOGIN_WINDOW seconds the chosen slot
# is pinned at its preferred metric — it carries egress again, the portal
# answers, the user signs in — and then it reverts on its own:
#   * the probe turns 'online'  -> the login worked, window closed, slot stays
#   * the user cancels          -> demoted again immediately
#   * the window times out      -> demoted again, back on the working uplink
# Held in memory only, and time-based rather than scheduled, so a reboot, a
# crashed keeper or a wedged thread can never leave a dead uplink pinned as
# primary: past `until` nothing holds any more.
_PORTAL_LOGIN_WINDOW = 900   # 15 minutes
_PORTAL_LOGIN_MIN = 60
_PORTAL_LOGIN_MAX = 3600
_portal_login = {"slot": "", "until": 0.0, "started": 0.0}


def portal_login_hold(slot):
    """True while a manual login window pins `slot` at its preferred metric.

    Deliberately not lock-guarded: it only reads two fields, and it is called
    from the demotion paths that already hold _lock. Expiry is the wall clock
    rather than a scheduled callback, so the hold lapses the moment its time
    is up whether or not anything has run to tidy the window away."""
    return _portal_login["slot"] == slot and time.time() < _portal_login["until"]


def portal_login_state():
    """{slot, remaining, window} for the UI. slot is '' when no window is open;
    remaining counts down in whole seconds."""
    remaining = _portal_login["until"] - time.time()
    if not _portal_login["slot"] or remaining <= 0:
        return {"slot": "", "remaining": 0, "window": _PORTAL_LOGIN_WINDOW}
    return {"slot": _portal_login["slot"],
            "remaining": int(remaining),
            "window": int(_portal_login["until"] - _portal_login["started"])}


def start_portal_login(slot, duration=None):
    """Open a login window on `slot`: restore its preferred metrics now (so it
    takes over egress and its portal becomes reachable) and hold them there for
    `duration` seconds. Only one window at a time — starting a second one
    closes the first. Returns portal_login_state(); raises ValueError for a
    slot that is unknown, disabled or absent."""
    if slot not in SLOTS:
        raise ValueError("Unknown connection. / 未知连接。")
    priority = get_priority()
    if not any(e["id"] == slot and e["enabled"] for e in priority):
        raise ValueError("That connection is switched off. / 该连接已关闭。")
    if not _slot_exists(slot):
        raise ValueError("That connection is not present. / 未检测到该连接。")
    try:
        duration = int(duration) if duration else _PORTAL_LOGIN_WINDOW
    except (TypeError, ValueError):
        duration = _PORTAL_LOGIN_WINDOW
    duration = max(_PORTAL_LOGIN_MIN, min(duration, _PORTAL_LOGIN_MAX))

    with _lock:
        if _portal_login["slot"] and _portal_login["slot"] != slot:
            end_portal_login("superseded by {}".format(slot))
        now = time.time()
        _portal_login.update({"slot": slot, "started": now,
                              "until": now + duration})
        # Undo the health demotion right away rather than waiting for the next
        # keeper pass: the user is standing at their browser waiting for the
        # login page. The wifi slot's metric belongs to wifi_wan, but its
        # preferred pair is exactly slot_metrics()'s (see wifi_metrics), and
        # its keeper honours the hold from the next round on.
        _set_slot_demoted(slot, False, priority)
        state = _health.get(slot)
        if state is not None:
            state["demoted"] = False
        pcat_logger.logger.info(
            "wan_manager: portal login window open on {} for {}s".format(
                slot, duration))
    return portal_login_state()


def end_portal_login(reason=""):
    """Close the window and put the slot back where its health says it belongs:
    a slot that is still not online is demoted again immediately, so cancelling
    hands egress back to the working uplink without waiting for a keeper pass.
    Returns the slot that was held, or ''."""
    with _lock:
        slot = _portal_login["slot"]
        if not slot:
            return ""
        _portal_login.update({"slot": "", "until": 0.0, "started": 0.0})
        if slot == "wifi":
            from pcat_util import wifi_wan
            online = wifi_wan.connectivity_state() == "online"
        else:
            online = (_health.get(slot) or {}).get("result") == "online"
        if not online:
            _set_slot_demoted(slot, True, get_priority())
            state = _health.get(slot)
            if state is not None:
                state["demoted"] = True
        pcat_logger.logger.info(
            "wan_manager: portal login window on {} closed ({}); uplink {}".format(
                slot, reason or "done", "kept" if online else "demoted again"))
        return slot


def _expire_portal_login():
    """Close a window whose time is up. Cheap enough for the 5s link pass,
    which is what guarantees the revert even if the 30s probe keeper is stuck
    behind a dead uplink's timeouts."""
    if _portal_login["slot"] and time.time() >= _portal_login["until"]:
        return end_portal_login("timed out")
    return ""


def _set_slot_demoted(slot, demoted, priority):
    """Shift a slot's metrics by +METRIC_DEMOTED (or back) and re-apply via
    ifup, mirroring the proven wifi demotion path. Lower = preferred."""
    metrics = slot_metrics(slot, priority, demoted)
    if not metrics:
        return False
    changed = False
    for iface, metric in metrics.items():
        if _iface_exists(iface):
            changed |= _set_if_changed("network.{}.metric".format(iface), metric)
    if changed:
        uci_commit("network")
        for v4, _v6 in _slot_iface_pairs(slot):
            if _iface_exists(v4):
                _ifup(v4)
        pcat_logger.logger.info("wan_manager: slot {} {} (metric base {})".format(
            slot, "demoted" if demoted else "restored", min(metrics.values())))
        # Route just changed (cable out demotion / restore): drop the short-TTL
        # active_egress memo so the dashboard does not keep the old green 🟢.
        try:
            import app as _app
            if hasattr(_app, "invalidate_active_egress_cache"):
                _app.invalidate_active_egress_cache()
            elif hasattr(_app, "_active_egress_cache"):
                _app._active_egress_cache["time"] = 0.0
        except Exception:
            pass
    return changed


def reassert_metrics(priority=None):
    """Re-apply the ladder metric to any slot interface whose metric was
    rewritten behind our back, and bounce that interface so the live route
    follows.

    Defense for dialers that rewrite UCI metrics out of band. Older
    pcat-manager fm350-mm builds did an unconditional
    `uci set network.wwan_fm350.metric=14` on every data call, which lands
    *below* the wifi slot's 20 and silently makes 5G the default route even
    though the table ranks WiFi above cellular. apply() only runs on
    save/boot; this function is called from the 5s link pass and the 30s
    probe keeper so drift is corrected within a few seconds.

    A slot legitimately sits at either its preferred or its demoted metric,
    so only a value that is neither counts as drift; the replacement follows
    our own health opinion. The wifi slot is skipped -- wifi_wan owns that
    metric (portal demotion, roam/rejoin) and would be fought here.

    Returns the list of corrected interfaces."""
    priority = priority or get_priority()
    # Reads _health verdicts and does a UCI metric read-modify-write, so it
    # runs under the shared lock like the rest of the metric mutators.
    with _lock:
        current = uci_show_dict("network")
        fixed = []
        for entry in priority:
            slot = entry["id"]
            if slot == "wifi" or not entry["enabled"]:
                continue
            is_demoted = bool((_health.get(slot) or {}).get("demoted"))
            preferred = slot_metrics(slot, priority, False)
            demoted = slot_metrics(slot, priority, True)
            for iface, metric in preferred.items():
                if current.get("network.{}".format(iface)) != "interface":
                    continue
                live = current.get("network.{}.metric".format(iface))
                if live in (str(metric), str(demoted[iface])):
                    continue
                target = demoted[iface] if is_demoted else metric
                uci_set("network.{}.metric={}".format(iface, target))
                fixed.append((iface, live, target))
        if not fixed:
            return []
        uci_commit("network")
        for iface, _live, _target in fixed:
            _ifup(iface)
        pcat_logger.logger.info("wan_manager: metric drift corrected: {}".format(
            ", ".join("{} {}->{}".format(i, live or "unset", t) for i, live, t in fixed)))
    # Route just changed: drop the short-TTL active_egress memo so the next
    # dashboard / priority poll sees the real default route immediately.
    if fixed:
        try:
            import app as _app
            if hasattr(_app, "invalidate_active_egress_cache"):
                _app.invalidate_active_egress_cache()
            elif hasattr(_app, "_active_egress_cache"):
                _app._active_egress_cache["time"] = 0.0
        except Exception:
            pass
    return [i for i, _live, _t in fixed]


def _probe(l3dev, record=None):
    # wifi_wan owns the probe implementation (HTTPS-verified with captive
    # portal detection); imported lazily to avoid a module cycle. `record`
    # collects the captive portal's login URL for THIS netdev (the STA's own
    # record belongs to the wifi keeper's episode, see _note_portal_url).
    from pcat_util import wifi_wan
    return wifi_wan._probe_connectivity(l3dev, record)


def _handoff_to_wifi(reason):
    """When a higher-priority uplink dies, unstick the wifi slot: if the STA
    still has an address, restore its preferred metric so it beats cellular;
    if it dropped, clear scan backoff so the next reconcile rejoins promptly.

    Without this, a wifi slot that was demoted (+1000) while eth was primary
    (false/stale probe, brief glitch) stays worse than cellular after eth is
    unplugged — the UI still shows the old IP, but egress jumps to 5G."""
    try:
        from pcat_util import wifi_wan
        if not is_slot_enabled("wifi"):
            return
        if wifi_wan.promote_if_connected():
            pcat_logger.logger.info(
                "wan_manager: promoted wifi after {} (prefer over cellular)".format(
                    reason))
        else:
            # Disassociated or no address yet — make the rejoin scan eager.
            wifi_wan.nudge_rejoin()
    except Exception as e:
        pcat_logger.logger.warning(
            "wan_manager: wifi handoff after {} failed: {}".format(reason, e))


def _mark_slot_down(slot, state, priority, reason):
    """Immediate demotion for a dead link. Unlike the probe path we do NOT
    restore preferred metrics while the link is down: a stale eth0 default
    route at metric 10 would blackhole egress after unplug until netifd
    tears it down (which can lag the carrier event by many seconds).

    Returns True only on the down *transition* (was not already demoted/down),
    so callers can kick a one-shot wifi handoff without spamming every tick
    while the cable stays unplugged."""
    # A login window on a link that just died has nothing left to sign in to;
    # close it so the demotion below is not held off (and the user is not left
    # staring at a countdown on an unplugged cable).
    if portal_login_hold(slot):
        end_portal_login("link went down")
    was_down = state.get("result") == "down" and state.get("demoted")
    state.update({"result": "down", "bad_streak": 0, "time": time.time()})
    if not state.get("demoted"):
        _set_slot_demoted(slot, True, priority)
        state["demoted"] = True
    transition = not was_down
    if transition and slot == "wan":
        _handoff_to_wifi(reason)
    return transition


def reconcile_link_state():
    """Cheap, no-HTTP pass: demote eth immediately when the cable is out,
    and reassert the metric ladder against dialer drift.

    Intended to run every few seconds so failover is not gated on the 30s
    probe keeper (two failed probe rounds would otherwise mean ~60s of
    blackhole / stuck-on-5G after unplug). Metric reassert also lives here
    so an FM350 (or other dialer) that rewrites UCI metric=14 on dial only
    steals egress for ~5s instead of up to ~30s.

    Returns a non-idle summary on the eth down *transition* or when metric
    drift was corrected, so the app thread can log / kick wifi rejoin without
    spamming every tick."""
    import pcat_config
    if pcat_config.get_network_mode() != "priority":
        return "not_priority"

    priority = get_priority()
    transition = None
    # Revert an expired captive-portal login window here, on the 5s pass: the
    # 30s probe keeper can sit for tens of seconds inside a dead uplink's
    # timeouts, and the promise the UI made ("switches back on its own after
    # 15 minutes") must not depend on that.
    expired = _expire_portal_login()
    if expired:
        transition = "portal_login_expired:{}".format(expired)
    # Short critical section (sysfs carrier reads + at most one demotion). The
    # keeper releases _lock around its probe, so this pass is no longer starved
    # behind a probe round -- it can demote eth the moment the cable is out.
    with _lock:
        for entry in priority:
            slot = entry["id"]
            if slot != "wan" or not entry["enabled"]:
                continue
            state = _health.setdefault(
                slot, {"result": "unknown", "bad_streak": 0, "demoted": False, "time": 0.0})
            if not _slot_link_up(slot):
                if _mark_slot_down(slot, state, priority, "no_carrier"):
                    transition = "wan:no_carrier"
            # carrier back: leave restore to the full probe reconcile so we do
            # not promote a link that is physically up but has no internet yet.

    # Defense: correct out-of-band metric rewrites (legacy fm350-mm metric=14,
    # other dialers) every few seconds — not only on the 30s probe keeper.
    # reassert_metrics uses the reentrant _lock; safe alongside the eth pass.
    fixed = reassert_metrics(priority)
    if fixed:
        drift = "metric_drift:" + ",".join(fixed)
        return transition + ";" + drift if transition else drift
    return transition or "idle"


def _active_slot():
    """Slot id currently carrying egress, from the kernel default route, or
    None. get_active_egress() is memoized (short TTL) so this is cheap; only
    'wan' and 'cellular' are health-probed here, and a wifi/lan/no-route egress
    maps to None (neither eth nor cellular is the active uplink)."""
    try:
        import app
        return {"wan": "wan", "mobile": "cellular"}.get(app.get_active_egress())
    except Exception:
        return None


def reconcile():
    """Health keeper for the non-wifi slots (the wifi slot has its own richer
    keeper in wifi_wan.reconcile()). Call on a timer from a background thread;
    probes block a few seconds per slot. Only active in priority mode."""
    import pcat_config
    if pcat_config.get_network_mode() != "priority":
        return "not_priority"

    # Single-flight: a round blocks tens of seconds across its probes, so a
    # second overlapping call would double-probe and race on _health. Skip.
    if not _reconcile_active.acquire(blocking=False):
        return "busy"
    try:
        priority = get_priority()
        # Before probing, take back any metric a dialer rewrote out of band (see
        # reassert_metrics): a cellular slot sitting at someone else's metric can
        # outrank WiFi, and the probes below would all pass while egress is on the
        # wrong uplink, so nothing else in this keeper would ever notice.
        reassert_metrics(priority)
        # Metrics only pick the egress; the firewall must also have the winning
        # device in its wan zone, and a Wi-Fi STA roam can silently drop it out
        # (see _zone_ruleset_missing). Probes below would still pass -- they run
        # ON the router -- while every LAN client is blackholed, so this keeper
        # is the right place to notice.
        check_zone_ruleset()
        active = _active_slot()
        summary = []
        for entry in priority:
            slot = entry["id"]
            if slot == "wifi":
                continue

            # Phase 1 — quick assessment under the lock: link/L3 checks and the
            # probe-due decision. The lock is dropped before the slow probe so
            # the fast carrier pass, status() and apply() are never blocked
            # behind a probe round. l3dev survives the block for phase 2.
            with _lock:
                if not entry["enabled"]:
                    _health.pop(slot, None)
                    continue
                state = _health.setdefault(
                    slot, {"result": "unknown", "bad_streak": 0, "demoted": False, "time": 0.0})

                # Cable out / modem netdev gone: demote immediately. Do not wait
                # for HTTP probe rounds and do not restore preferred metrics down.
                if not _slot_link_up(slot):
                    _mark_slot_down(slot, state, priority, "no_carrier")
                    summary.append("{}:down".format(slot))
                    continue

                l3dev = _slot_l3dev(slot)
                if l3dev is False:
                    # ubus was unreachable, so we can't tell this slot's real L3
                    # state. Leave its metrics/verdict untouched and retry next
                    # pass -- demoting on a ubus hiccup would blackhole a live
                    # uplink. (The carrier check above already caught a dead link.)
                    summary.append("{}:ubus_unavailable".format(slot))
                    continue
                if not l3dev:
                    # No L3 yet. For eth (wan) demote immediately — a cable-out
                    # iface can keep a stale default route that blackholes
                    # traffic. For cellular leave preferred metrics in place so
                    # when the modem dials the route is ready without a restore.
                    if slot == "wan":
                        _mark_slot_down(slot, state, priority, "no_l3")
                    else:
                        state.update({"result": "down", "bad_streak": 0, "time": time.time()})
                        if state.get("demoted"):
                            _set_slot_demoted(slot, False, priority)
                            state["demoted"] = False
                    summary.append("{}:down".format(slot))
                    continue

                # Standby-probe backoff (power). Probe the active slot and any
                # demoted slot every pass -- failover of the active uplink and
                # restore of a demoted one both depend on a fresh verdict -- and
                # any slot not yet measured. A healthy, non-active standby (radio
                # idle) is backed off to _STANDBY_PROBE_INTERVAL: it carries no
                # traffic and its metrics are already at preferred, so if the
                # active uplink dies egress fails over to it immediately and it is
                # probed every pass again (demoted within a cycle if it too is
                # dead). Worst case is a ~2min-stale badge; egress never stranded.
                now = time.time()
                probe_due = (slot == active
                             or state["demoted"]
                             or state["result"] == "unknown"
                             # A slot under a login window is held at preferred
                             # metrics, so the "healthy standby" backoff below
                             # would apply to it -- and the window closes on a
                             # fresh 'online' verdict, so backing off would
                             # leave the user signed in but still counting down.
                             or portal_login_hold(slot)
                             or (now - state["time"]) >= _STANDBY_PROBE_INTERVAL)
                if not probe_due:
                    summary.append("{}:{}".format(slot, state["result"]))
                    continue

            # Phase 2 — the slow probe, WITHOUT the lock.
            portal_seen = {"url": ""}
            verdict = _probe(l3dev, portal_seen)

            # Phase 3 — apply the verdict, re-validating against a concurrent
            # apply()/fast-pass that may have run during the probe.
            with _lock:
                state = _health.get(slot)
                if state is None or not is_slot_enabled(slot):
                    # apply() toggled or cleared this slot mid-probe; drop the
                    # now-stale verdict rather than resurrect an orphaned state.
                    summary.append("{}:skipped".format(slot))
                    continue
                if not _slot_link_up(slot):
                    # Carrier dropped mid-probe; the fast pass owns the demotion
                    # now. Do not let a stale "online" verdict restore it.
                    summary.append("{}:down".format(slot))
                    continue
                state["time"] = time.time()
                state["portal_url"] = portal_seen["url"] if verdict == "portal" else ""
                if verdict == "online":
                    state["bad_streak"] = 0
                    state["result"] = "online"
                    if state["demoted"]:
                        _set_slot_demoted(slot, False, priority)
                        state["demoted"] = False
                    if portal_login_hold(slot):
                        # The sign-in worked: the window has done its job, and
                        # the slot keeps the preferred metrics it is already on.
                        end_portal_login("login succeeded")
                else:
                    state["bad_streak"] += 1
                    # Same flap damping as the wifi path: hold the last good
                    # verdict until failures are sustained, so one dropped probe
                    # can't bounce egress to the next slot and back.
                    if state["result"] == "online" and state["bad_streak"] < _HEALTH_FLAP_THRESHOLD:
                        pass
                    else:
                        state["result"] = verdict
                        if portal_login_hold(slot):
                            # The user is signing in on this slot right now.
                            # Demoting it would push their browser back out the
                            # working uplink, where the portal does not answer —
                            # the exact deadlock the window exists to break.
                            # end_portal_login() applies this verdict when the
                            # window closes.
                            pass
                        elif not state["demoted"] and state["bad_streak"] >= _HEALTH_FLAP_THRESHOLD:
                            _set_slot_demoted(slot, True, priority)
                            state["demoted"] = True
                            if slot == "wan":
                                _handoff_to_wifi("wan_offline")
                summary.append("{}:{}".format(slot, state["result"]))
        return " ".join(summary) or "idle"
    finally:
        _reconcile_active.release()


# ── status for the UI ────────────────────────────────────────────────────────

def _slot_exists(slot):
    if slot == "wifi":
        from pcat_util import wifi_wan
        return wifi_wan.is_available()
    return any(os.path.exists("/sys/class/net/{}".format(dev))
               for dev in SLOT_NETDEVS.get(slot, ()))


def _radio_classes():
    """Which physical radio classes exist, from the UCI radio device paths.

    A radio section outlives its hardware: pulling the PCIe card leaves
    wireless.radio1 in /etc/config/wireless, and going on the section alone
    kept offering "PCIe" as an upstream radio on a box that no longer has
    one — picking it moved the STA onto a radio that can never come up.
    Confirm each section still resolves to a live kernel phy."""
    from pcat_util import wifi_wan
    classes = {"onboard": False, "pcie": False}
    # Cached wireless snapshot, not one 'uci get' fork per radio: status() and
    # the reconcile keeper hit this on hot paths, and the radio path is static.
    cfg = uci_show_dict("wireless")
    for radio in ("radio0", "radio1"):
        path = cfg.get("wireless.{}.path".format(radio), "")
        if not path:
            continue
        if wifi_wan._phy_for_radio(radio) is None:
            continue
        classes["pcie" if "pcie" in path else "onboard"] = True
    return classes


def relay_radios_ready():
    """True when two live radios exist — the floor for the WiFi upstream
    relay (one radio joins upstream while the other keeps serving this
    device's own hotspot). False can also mean the drivers simply haven't
    registered their phys yet (early boot), so callers must treat it as a
    transient condition, never persist it."""
    return sum(1 for alive in _radio_classes().values() if alive) >= 2


def _default_route_metrics():
    """{netdev: lowest default-route metric} from `ip -j route show default`.

    Used by status() so the debug panel can show what the kernel is actually
    routing on, not just the UCI ladder values. Empty on parse failure."""
    try:
        out = subprocess.run(
            ["ip", "-j", "route", "show", "default"],
            capture_output=True, text=True, timeout=3).stdout.strip()
        routes = json.loads(out) if out else []
    except Exception:
        return {}
    by_dev = {}
    for r in routes or []:
        dev = r.get("dev") or ""
        if not dev:
            continue
        m = r.get("metric", 0)
        try:
            m = int(m)
        except (TypeError, ValueError):
            m = 0
        if dev not in by_dev or m < by_dev[dev]:
            by_dev[dev] = m
    return by_dev


def _iface_metrics_debug(priority=None):
    """Per-UCI-iface metric snapshot for the WAN priority debug panel.

    For every ladder interface that exists in UCI: intended preferred/demoted
    metrics, live UCI metric, netdev, and the kernel default-route metric on
    that netdev (if any). Lets the UI surface dialer drift (e.g. metric=14)
    without SSHing into the box."""
    priority = priority or get_priority()
    current = uci_show_dict("network") or {}
    route_by_dev = _default_route_metrics()
    rows = []
    for pos, entry in enumerate(priority):
        slot = entry["id"]
        is_demoted = bool((_health.get(slot) or {}).get("demoted"))
        preferred = slot_metrics(slot, priority, False)
        demoted = slot_metrics(slot, priority, True)
        for v4, v6 in SLOT_IFACES.get(slot, ()):
            for iface in (v4, v6):
                if current.get("network.{}".format(iface)) != "interface":
                    continue
                pref = preferred.get(iface)
                dem = demoted.get(iface)
                uci_m = current.get("network.{}.metric".format(iface))
                # Resolve netdev via ubus when possible so route metric matches.
                ifstatus = ubus_call(
                    "network.interface.{}".format(iface), "status") or {}
                dev = (ifstatus.get("l3_device")
                       or ifstatus.get("device")
                       or current.get("network.{}.device".format(iface))
                       or current.get("network.{}.ifname".format(iface))
                       or "")
                # Strip alias form like "@wan_wifi"
                if isinstance(dev, str) and dev.startswith("@"):
                    dev = ""
                route_m = route_by_dev.get(dev) if dev else None
                # Drift = UCI metric is neither preferred nor demoted ladder value
                drifted = False
                if pref is not None and uci_m is not None:
                    drifted = str(uci_m) not in (str(pref), str(dem))
                rows.append({
                    "slot": slot,
                    "iface": iface,
                    "enabled": bool(entry.get("enabled")),
                    "demoted": is_demoted if slot != "wifi" else False,
                    "preferred": pref,
                    "demoted_metric": dem,
                    "uci_metric": int(uci_m) if uci_m is not None and str(uci_m).isdigit()
                                 else uci_m,
                    "device": dev,
                    "up": bool(ifstatus.get("up")),
                    "route_metric": route_m,
                    "drifted": drifted,
                })
    return rows


def status():
    """Table + live per-slot state for the /api/v1/wan_priority.json GET."""
    import pcat_config
    from pcat_util import wifi_wan

    priority = get_priority()
    # Snapshot the shared verdicts under the lock, then build rows from the copy
    # so the (per-slot ubus) status calls below don't hold _lock and stall the
    # keeper / fast pass.
    with _lock:
        health_snapshot = {s: dict(v) for s, v in _health.items()}
    # Live UCI metrics once for all slots (also feeds the debug panel).
    try:
        uci_net = uci_show_dict("network") or {}
    except Exception:
        uci_net = {}
    route_by_dev = _default_route_metrics()
    rows = []
    for pos, entry in enumerate(priority):
        slot = entry["id"]
        intended = _slot_base(pos)
        row = {
            "id": slot,
            "enabled": entry["enabled"],
            "metric": intended,
            "metric_uci": None,
            "metric_route": None,
            "exists": _slot_exists(slot),
            "up": False,
            "device": "",
            "ip": "",
            "health": "unknown",
            "demoted": False,
            # Where this slot's captive portal sends clients, when its last
            # probe caught a redirect. The UI offers it as a login shortcut.
            "portal_url": "",
        }
        for v4, _v6 in SLOT_IFACES[slot]:
            ifstatus = ubus_call("network.interface.{}".format(v4), "status") or {}
            if ifstatus.get("up"):
                row["up"] = True
                row["device"] = ifstatus.get("l3_device") or ifstatus.get("device") or ""
                for addr in ifstatus.get("ipv4-address") or []:
                    row["ip"] = addr.get("address", "")
                    break
                # Live UCI + kernel metrics for the interface that is actually up.
                uci_m = uci_net.get("network.{}.metric".format(v4))
                if uci_m is not None and str(uci_m).isdigit():
                    row["metric_uci"] = int(uci_m)
                elif uci_m is not None:
                    row["metric_uci"] = uci_m
                if row["device"] and row["device"] in route_by_dev:
                    row["metric_route"] = route_by_dev[row["device"]]
                break
        if slot == "wifi":
            # wifi health comes from wifi_wan's keeper (portal-aware)
            if wifi_wan.is_sta_enabled():
                conn = wifi_wan.connectivity_state()
                row["health"] = conn if row["up"] else "down"
                if row["health"] == "portal":
                    row["portal_url"] = wifi_wan.portal_url()
            else:
                row["health"] = "down" if entry["enabled"] else "unknown"
            # wifi_wan may demote via +1000 without going through _health
            if row["metric_uci"] is not None and isinstance(row["metric_uci"], int):
                if row["metric_uci"] >= intended + METRIC_DEMOTED:
                    row["demoted"] = True
                    row["metric"] = intended + METRIC_DEMOTED
        else:
            state = health_snapshot.get(slot)
            if not entry["enabled"]:
                row["health"] = "unknown"
            elif state:
                row["health"] = state["result"]
                row["demoted"] = state["demoted"]
                if row["health"] == "portal":
                    row["portal_url"] = state.get("portal_url", "")
                if state["demoted"]:
                    # Report the metric the kernel is actually routing on, not
                    # the ladder value the slot would have when healthy -- a
                    # demoted slot showing metric 10 next to demoted:true reads
                    # as a contradiction and hides the +1000 from diagnostics.
                    row["metric"] += METRIC_DEMOTED
            else:
                row["health"] = "unknown" if row["up"] else "down"
        rows.append(row)

    # Full per-iface dump for the debug metrics panel (auto-refreshed with the
    # priority table poll). Built outside the slot loop so v6 + standby ifaces
    # that are not "the" active member still appear.
    try:
        iface_metrics = _iface_metrics_debug(priority)
    except Exception as e:
        pcat_logger.logger.debug("iface_metrics_debug failed: {}".format(e))
        iface_metrics = []

    return {
        "mode": pcat_config.get_network_mode(),
        "priority": rows,
        "portal_login": portal_login_state(),
        "wifi_radio": pcat_config.get_wifi_wan_radio(),
        "wifi_radios": _radio_classes(),
        "iface_metrics": iface_metrics,
        "active_route_metrics": route_by_dev,
    }
