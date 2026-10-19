"""Boot-time repair for a Wi-Fi radio the firmware brought up switched off.

A photonicat2 with an add-on PCIe Wi-Fi card comes out of a firmware flash
with that radio unusable, and it takes two reboots plus a manual click in
Luci before it works:

  * first boot -- the card has not finished enumerating by the time OpenWrt
    generates /etc/config/wireless, so there is no wifi-device section for it
    at all and the radio is absent from every UI;
  * second boot -- detection finds it and writes the section, but a freshly
    detected radio is written with `option disabled '1'`. The AP is now
    configured and correctly branded, sitting in the list looking healthy,
    and never goes on the air.

A third way loses the radio outright: park the onboard AP for the upstream
relay, reboot, and OpenWrt prunes that radio's whole `wifi-device` section
because it cannot match the stored path to a phy that has not enumerated yet.
The AP iface is left pointing at nothing, the radio is absent from every UI,
and the section comes back later as factory 'OpenWrt' with no passphrase --
the customer's network name and password gone.

None of this is something a user can be expected to diagnose; the reported
symptom is "my WiFi disappeared". All three are reconciled here.

Timing is the whole difficulty. This does NOT run "long after netifd", which
is what an earlier version of this file assumed: the reconcile has been
observed running with one of the two phys still un-enumerated, correctly
concluding nothing was wrong, and being overtaken moments later by the
firmware regenerating the very radio it was there to protect. So the pass
waits for the hardware to actually appear (see _read_wireless) and then looks
again a few times over the following minutes (see _schedule_recheck), because
the cost of losing that race is a customer with no WiFi.

Not fighting the user is the hard part. A radio that is off because someone
*wanted* it off must stay off, and we cannot tell that from UCI alone: a
deliberately disabled radio and a never-enabled one look identical. So the
intent is tracked separately, in the user config, and the reconcile is a
small state machine over (hardware present, UCI state, remembered intent):

    intent unknown, UCI off  -> enable it, remember "on"
    intent "on",     UCI off -> someone turned it off behind our back since
                                we last enabled it; that is a real choice.
                                Remember "off" and never touch it again.
    intent "off",    UCI off -> leave it alone, forever.
    any intent,      UCI on  -> remember "on".

The worst case for someone who disables a radio outside this UI (ssh, Luci)
is therefore that it comes back on exactly once, and is then respected
permanently. The alternative -- doing nothing -- leaves every flashed device
with a dead radio until its owner finds the checkbox.

"on" is only remembered after re-reading UCI and confirming the change
actually landed. A commit that fails silently leaves the staged change in
/tmp (see openwrt.uci_commit), and recording "on" off the back of one would
make the next boot read a still-disabled radio as a deliberate switch-off and
give up permanently.
"""

import os
import subprocess
import threading
import time

from pcat_util import pcat_logger
from pcat_util.openwrt import (
    invalidate_uci_cache, uci_commit, uci_commit_verified, uci_delete, uci_set,
    uci_show_dict,
)

# {radio name: bool} in the user config -- the last on/off state we have
# reason to believe the user wanted. Written here and by the wireless page's
# enable toggle (app.enable_wifi_interface).
INTENT_KEY = "wifi_radio_intent"

# {radio name: {ssid, encryption, key, hidden, origin: {...}}} in the user
# config -- the hotspot settings last saved through this UI, plus the identity
# that save replaced. Written by the wireless page's save (see
# remember_ap_settings) and read back by restore_saved_aps.
AP_SETTINGS_KEY = "wifi_ap_settings"

# The AP options a save owns and a restore puts back. Band, channel and htmode
# are deliberately not among them: the relay retunes its radio to the
# *upstream's* band for as long as it runs, and a regenerated radio's band is
# already covered from the relay's own snapshot (restore_regenerated_bands).
AP_IDENTITY_OPTS = ("ssid", "encryption", "key", "hidden")

PHY_DIR = "/sys/class/ieee80211"

# The firmware's own AP naming: the onboard radio broadcasts the device name
# bare, the add-on PCIe card the same name with a "-5g" tail --
# photonicat2-46a4d8dd / photonicat2-46a4d8dd-5g.
#
# Naming a new AP therefore has to start from the *stem*. Appending a band to
# whichever radio happened to already be configured is how an onboard AP ended
# up broadcasting "photonicat2-46a4d8dd-5g-5g": the template it inherited from
# was the PCIe radio, whose name already carried the suffix (issue #115).
BAND_SUFFIXES = ("-2.4g", "-2g", "-5g", "-6g")
PCIE_SSID_SUFFIX = "-5g"

# What a factory-generated AP section is called. An AP wearing one of these is
# the firmware's, not the customer's, and may be rewritten freely -- it is also
# an AP that must never be switched on as-is, since the stock section carries
# no passphrase.
#
# Matched whole (modulo a band suffix), never as a prefix: somebody who names
# their network "OpenWrt-home" has chosen that name, and rewriting their SSID
# and passphrase out from under them is precisely the harm this file exists to
# prevent. Same list wifi_wan._PLACEHOLDER_SSIDS tests against.
STOCK_SSIDS = ("OpenWrt", "LEDE")

# How long startup() will wait for the boot to settle /etc/config/wireless.
WIRELESS_WAIT_SECONDS = 60
WIRELESS_POLL_SECONDS = 2

# Seconds after the first pass at which to look again. Empty disables.
#
# The firmware's regeneration of a parked radio has been timed at ~100s after
# boot on this hardware, which no single pass can be scheduled against: land
# before it and there is nothing to see, land after and the customer has
# already lost their network. These offsets bracket it either side and out to
# several minutes, because guessing one moment is what the timeline above says
# does not work. Each pass is a single `uci show` on a healthy device.
RECHECK_SCHEDULE = (60, 180, 420)


def _strip_one_band_suffix(ssid):
    for suffix in BAND_SUFFIXES:
        if ssid.lower().endswith(suffix):
            return ssid[:-len(suffix)]
    return ssid


def ssid_stem(ssid):
    """`ssid` with every trailing band suffix peeled off.

    One suffix is the firmware's own convention; more than one only ever comes
    from the mis-branding above, and peeling all of them is what lets a device
    already stuck with "-5g-5g" be repaired. An SSID that is nothing but a
    suffix is returned untouched -- an empty name is not an improvement.
    """
    while True:
        stripped = _strip_one_band_suffix(ssid)
        if stripped == ssid or not stripped:
            return ssid
        ssid = stripped


def is_pcie_radio(wireless, radio):
    """True when this radio is the add-on PCIe card rather than the onboard
    one, matched on the device path -- the same test command_parser and
    wifi_wan use to label the two radios."""
    return "pcie" in wireless.get("wireless.{}.path".format(radio), "")


def ap_ssid_for(wireless, radio, stem):
    """The firmware's AP name for `radio`: <stem>-5g on the PCIe card,
    <stem> on the onboard radio."""
    if is_pcie_radio(wireless, radio):
        return stem + PCIE_SSID_SUFFIX
    return stem


# ── remembered intent ────────────────────────────────────────────────────────

def get_intents():
    """{radio: bool} of remembered user intent; {} when nothing is stored."""
    try:
        import pcat_config
        stored = pcat_config.get_user_config_key(INTENT_KEY, None)
    except Exception:
        return {}
    if not isinstance(stored, dict):
        return {}
    return {k: bool(v) for k, v in stored.items() if isinstance(k, str)}


def save_intents(intents):
    try:
        import pcat_config
        pcat_config.set_user_config_key(INTENT_KEY, intents)
        return True
    except Exception as e:
        pcat_logger.logger.warning("wifi_radio: could not save intent: %s", e)
        return False


def record_intent(radio, enabled):
    """Remember that the user asked for `radio` to be on/off.

    Called from the wireless page's enable toggle. Without this, turning a
    radio off in this UI would be undone by the next boot's reconcile.
    """
    intents = get_intents()
    if intents.get(radio) is bool(enabled):
        return False
    intents[radio] = bool(enabled)
    return save_intents(intents)


# ── remembered hotspot settings ──────────────────────────────────────────────

def get_ap_settings():
    """{radio: entry} of hotspot settings saved through this UI; {} when none."""
    try:
        import pcat_config
        stored = pcat_config.get_user_config_key(AP_SETTINGS_KEY, None)
    except Exception:
        return {}
    if not isinstance(stored, dict):
        return {}
    return {k: v for k, v in stored.items()
            if isinstance(k, str) and isinstance(v, dict)}


def save_ap_settings(store):
    try:
        import pcat_config
        pcat_config.set_user_config_key(AP_SETTINGS_KEY, store)
        return True
    except Exception as e:
        pcat_logger.logger.warning(
            "wifi_radio: could not save AP settings: %s", e)
        return False


def read_ap_identity(wireless, vif):
    """The AP options this module owns, read out of a `uci show` snapshot."""
    return {opt: wireless.get("wireless.{}.{}".format(vif, opt), "")
            for opt in AP_IDENTITY_OPTS}


def remember_ap_settings(radio, identity, replaced=None):
    """Record the hotspot settings just saved for `radio`.

    This becomes the only copy of the customer's network name and passphrase
    that survives the firmware rewriting /etc/config/wireless, so it is
    written on every save rather than only the first.

    `replaced` is the identity that was live immediately before this save. The
    first one seen is kept as the radio's `origin` and never overwritten: on a
    device configured through this UI that is the factory identity, which is
    what later makes a regenerated section recognisable (restore_saved_aps).
    """
    store = get_ap_settings()
    previous = store.get(radio) or {}
    entry = {opt: identity.get(opt, "") for opt in AP_IDENTITY_OPTS}
    origin = previous.get("origin")
    if isinstance(origin, dict) and origin.get("ssid"):
        entry["origin"] = origin
    elif replaced and replaced.get("ssid"):
        entry["origin"] = {opt: replaced.get(opt, "")
                           for opt in AP_IDENTITY_OPTS}
    store[radio] = entry
    return save_ap_settings(store)


def saved_ap_settings(radio, store=None):
    """The identity last saved for `radio` through this UI, or None."""
    entry = (get_ap_settings() if store is None else store).get(radio)
    if not isinstance(entry, dict) or not entry.get("ssid"):
        return None
    return {opt: entry.get(opt, "") for opt in AP_IDENTITY_OPTS}


def ap_section(wireless, radio):
    """The iface section `radio` serves clients from.

    Resolved, never assumed: `default_<radio>` is only the name OpenWrt gives
    the sections it generates itself, and a `uci set` against a section that is
    not there fails -- silently, when nobody checks the return code. The read
    path has always resolved the real section, so writing to a guessed one is
    how a save could be reported as done and change nothing at all.
    """
    from pcat_util import command_parser
    return command_parser._ap_vif(wireless, radio)


def _apply_sae_pmf(vif, encryption):
    """SAE without these two is WPA3 that clients cannot hold a connection to:
    PMF (ieee80211w=2) is mandatory for SAE, and OCV has to be off for stable
    WPA3 on this hardware. Derived from the encryption at every write rather
    than stored, so falling back to psk2 never leaves PMF=required behind.
    """
    if encryption == "sae":
        ok = uci_set("wireless.{}.ieee80211w=2".format(vif))
        return uci_set("wireless.{}.ocv=0".format(vif)) and ok
    # Already absent is the wanted state, so a failed delete is not an error.
    uci_delete("wireless.{}.ieee80211w".format(vif))
    uci_delete("wireless.{}.ocv".format(vif))
    return True


def save_ap_config(radio, wireless, radio_opts, identity):
    """Write one radio's hotspot settings, and prove they reached flash.

    `identity` carries ssid/encryption/hidden plus `key`, where None means
    "leave the passphrase alone"; `radio_opts` are the wifi-device options
    (band/htmode/channel/country) that go with them. `wireless` is a fresh
    `uci show wireless` snapshot.

    Every save from the wireless page comes through here so that "did it
    actually persist?" has exactly one answer, and so the settings are
    remembered for restore_saved_aps() to put back when the firmware
    regenerates the config.

    Returns (ok, vif).
    """
    vif = ap_section(wireless, radio)
    previous = read_ap_identity(wireless, vif)
    key = identity.get("key")
    effective_key = previous.get("key", "") if key is None else key

    ok = True
    if wireless.get("wireless.{}".format(vif)) != "wifi-iface":
        # The firmware prunes a radio's AP section outright at times; put a
        # usable one back rather than fail the save.
        for kv in ("wireless.{}=wifi-iface".format(vif),
                   "wireless.{}.device={}".format(vif, radio),
                   "wireless.{}.network=lan".format(vif),
                   "wireless.{}.mode=ap".format(vif)):
            ok = uci_set(kv) and ok
    for opt in sorted(radio_opts):
        ok = uci_set("wireless.{}.{}={}".format(
            radio, opt, radio_opts[opt])) and ok
    ok = uci_set("wireless.{}.ssid={}".format(
        vif, identity.get("ssid", ""))) and ok
    ok = uci_set("wireless.{}.encryption={}".format(
        vif, identity.get("encryption", "none"))) and ok
    ok = _apply_sae_pmf(vif, identity.get("encryption", "none")) and ok
    if key is not None:
        ok = uci_set("wireless.{}.key={}".format(vif, key)) and ok
    if identity.get("hidden"):
        ok = uci_set("wireless.{}.hidden=1".format(vif)) and ok
    else:
        # Already absent is the wanted state, so a failed delete is not one.
        uci_delete("wireless.{}.hidden".format(vif))

    # Verified: a commit that leaves the change in the /tmp/.uci staging delta
    # works for the rest of the session and is gone at the next reboot -- which
    # is exactly the "the WiFi name and password I set go back to the defaults
    # when I restart" report. Better to tell the user the save failed than to
    # let it evaporate overnight.
    committed = uci_commit_verified(
        "wireless", expect=[identity.get("ssid", ""), effective_key])
    if not (ok and committed):
        pcat_logger.logger.error(
            "wifi_radio: %s hotspot save did not persist "
            "(writes ok=%s, commit ok=%s)", radio, ok, committed)
        return False, vif
    remember_ap_settings(radio, dict(identity, key=effective_key),
                         replaced=previous)
    return True, vif


def _same_network(live, other):
    """Same name and passphrase -- i.e. the same network as a client sees it."""
    if not other or not other.get("ssid"):
        return False
    return (live.get("ssid", "") == other.get("ssid", "")
            and live.get("key", "") == other.get("key", ""))


def factory_ssid_stem():
    """The name the firmware brands a fresh AP with: the device's hostname.

    A regenerated section comes back as <hostname> on the onboard radio and
    <hostname>-5g on the PCIe card (see ap_ssid_for) -- photonicat2-4a99abb9 /
    photonicat2-4a99abb9-5g. Recognising that shape is what lets a hotspot be
    repaired for someone who never used this page at all: their own name is
    remembered by restore_saved_aps just from seeing it configured, and a
    section wearing the device's own factory name instead is the firmware's,
    not theirs.
    """
    from pcat_util.openwrt import uci_get
    return uci_get("system.@system[0].hostname").strip()


def _looks_factory(wireless, vif, origin, factory_stem):
    """Is this AP section the firmware's own rather than someone's choice?

    Three ways to tell, in descending order of certainty: it is an OpenWrt
    placeholder; it is exactly the identity this radio carried before the
    customer first changed it here; or it wears the device's own factory name.
    Anything else is treated as a deliberate choice and never overwritten.
    """
    if is_stock_ap(wireless, vif):
        return True
    live = read_ap_identity(wireless, vif)
    if _same_network(live, origin):
        return True
    return bool(factory_stem) and ssid_stem(live.get("ssid", "")) == factory_stem


def restore_saved_aps(wireless, radios):
    """Put the customer's own hotspot settings back on a regenerated AP.

    The firmware rewrites /etc/config/wireless when it pleases -- during boot,
    and on some units the whole file reverts on every boot -- and what comes
    back is the *factory* config: the device's original network name and
    password, not the customer's. That is not the OpenWrt-stock shape
    restore_stock_aps repairs. It looks exactly like a properly configured AP,
    so nothing here used to touch it, and the reported symptom was the plain
    "the WiFi name and password I set go back to the defaults when I restart".

    So the hotspot identity is remembered -- from a save on this page, and
    otherwise from simply seeing a configured one here, so that somebody who
    set their WiFi up in Luci is covered too -- and written back whenever the
    live section has reverted to something recognisably the firmware's
    (_looks_factory).

    An identity we do not recognise is left strictly alone and adopted as the
    new remembered one, so a hotspot edited outside this UI is never reverted
    at the next boot; it becomes what a later restore puts back.

    Returns [(radio, vif, secured)] for the sections written, the same shape as
    restore_stock_aps. The caller commits.
    """
    store = get_ap_settings()
    factory_stem = factory_ssid_stem()
    restored = []
    learned = {}
    for radio in radios:
        # Only the section this page manages, not every AP on the radio: a
        # second one (a guest network added in Luci) is somebody else's, and
        # taking its name for this radio's remembered hotspot would have the
        # two overwriting each other on alternate passes.
        vif = ap_section(wireless, radio)
        if wireless.get("wireless.{}".format(vif)) != "wifi-iface":
            continue  # no hotspot section to repair
        wanted = saved_ap_settings(radio, store)
        origin = (store.get(radio) or {}).get("origin")
        live = read_ap_identity(wireless, vif)
        if wanted and _same_network(live, wanted):
            continue
        if not _looks_factory(wireless, vif, origin, factory_stem):
            # Someone's own choice, made outside this page. Never revert it;
            # remember it instead, so it is what comes back if the firmware
            # regenerates this section later.
            learned[radio] = live
            continue
        if not wanted:
            continue  # nothing remembered for this radio yet
        for opt in AP_IDENTITY_OPTS:
            val = wanted.get(opt)
            if val:
                uci_set("wireless.{}.{}={}".format(vif, opt, val))
            else:
                uci_delete("wireless.{}.{}".format(vif, opt))
        _apply_sae_pmf(vif, wanted.get("encryption") or "none")
        # Verbatim what the customer asked for, so an AP with no passphrase
        # here means they chose an open network -- not that a passphrase was
        # lost, which is what the caller's `secured` flag guards.
        secured = (bool(wanted.get("key"))
                   or wanted.get("encryption") == "none")
        restored.append((radio, vif, secured))
        pcat_logger.logger.info(
            "wifi_radio: %s reverted to '%s'; restoring the saved "
            "hotspot '%s'", vif, live.get("ssid", ""), wanted["ssid"])
    if learned:
        for radio, live in learned.items():
            entry = dict(store.get(radio) or {})
            entry.update(live)
            store[radio] = entry
            pcat_logger.logger.info(
                "wifi_radio: remembering %s hotspot '%s' set outside this UI",
                radio, live.get("ssid", ""))
        save_ap_settings(store)
    return restored


# ── reading the current wireless config ──────────────────────────────────────

def wifi_devices(wireless):
    """Names of every wifi-device section, in config order."""
    names = []
    for key, value in wireless.items():
        if value == "wifi-device" and key.count(".") == 1:
            names.append(key.split(".", 1)[1])
    return names


def ap_vifs(wireless, radio):
    """AP-mode wifi-iface sections bound to `radio`.

    Mode is checked so the upstream STA relay is never unparked here: that
    section belongs to wifi_wan, which enables it only when the relay is
    supposed to be running.
    """
    out = []
    for key, value in wireless.items():
        if value != "wifi-iface" or key.count(".") != 1:
            continue
        section = key.split(".", 1)[1]
        if wireless.get("wireless.{}.device".format(section)) != radio:
            continue
        if wireless.get("wireless.{}.mode".format(section), "ap") != "ap":
            continue
        out.append(section)
    return out


def is_enabled(wireless, radio, vifs):
    """True when neither the radio nor any of its AP vifs is disabled.

    Mirrors command_parser.is_wifi_interface_enabled, but reads a snapshot
    instead of forking a `uci get` per key. An absent `disabled` option means
    enabled, which is why "" counts as on.
    """
    if wireless.get("wireless.{}.disabled".format(radio), "") not in ("", "0"):
        return False
    for vif in vifs:
        if wireless.get("wireless.{}.disabled".format(vif), "") not in ("", "0"):
            return False
    return True


def phy_count():
    """Number of mac80211 phys the kernel has enumerated."""
    try:
        return len([n for n in os.listdir(PHY_DIR) if n.startswith("phy")])
    except OSError:
        return 0


def radio_present(wireless, radio):
    """Is the hardware behind this radio section actually plugged in?

    A section whose card has been removed is left alone -- enabling it would
    be a no-op for netifd, but it would also record an intent for hardware
    nobody has. A section with no `path` cannot be checked, so it is assumed
    present rather than skipped: refusing to heal is the worse failure here.
    """
    path = wireless.get("wireless.{}.path".format(radio), "")
    if not path:
        return True
    from pcat_util import command_parser
    if command_parser.device_exist(path):
        return True
    # The stored path goes stale on its own: the onboard module hangs off a USB
    # host controller whose instance number changes between boots. Taking that
    # at face value makes the reconcile skip a perfectly healthy radio, so ask
    # whether a phy of the same class is fitted before giving up on it.
    _resolved, fitted = command_parser.resolve_radio_path(
        wireless, radio, wifi_devices(wireless),
        command_parser.live_phy_paths())
    return fitted


def orphaned_radios(wireless):
    """Radios an iface still points at that have no wifi-device section left.

    OpenWrt drops a radio's device section when it cannot match the stored
    path to a phy, and leaves the AP iface behind pointing at nothing. The
    radio is then invisible to every UI and off the air, and without this
    nothing ever puts it back -- the reconcile's own detection trigger
    (a phy with no section) does not fire, because the pruned section took the
    phy's accounting with it.
    """
    radios = set(wifi_devices(wireless))
    orphans = []
    for key, value in wireless.items():
        if value != "wifi-iface" or key.count(".") != 1:
            continue
        device = wireless.get(
            "wireless.{}.device".format(key.split(".", 1)[1]), "")
        if device and device not in radios and device not in orphans:
            orphans.append(device)
    return sorted(orphans)


def is_stock_ap(wireless, vif):
    """True when this AP section is the firmware's rather than the customer's.

    A nameless section counts: `wifi config` writes those too, and either way
    there is no passphrase behind it.
    """
    ssid = wireless.get("wireless.{}.ssid".format(vif), "")
    if not ssid:
        return True
    return _strip_one_band_suffix(ssid) in STOCK_SSIDS


# ── the two repairs ──────────────────────────────────────────────────────────

def detect_new_radios():
    """Let OpenWrt write config for a phy that has none yet.

    Only run when a phy is genuinely unaccounted for, so on a normal boot
    this never fires. `wifi config` appends sections for undetected devices
    and leaves existing ones alone; it writes /etc/config/wireless directly,
    hence the cache drop afterwards.
    """
    try:
        subprocess.run(["/sbin/wifi", "config"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=30)
    except Exception as e:
        pcat_logger.logger.warning("wifi_radio: wifi config failed: %s", e)
        return False
    invalidate_uci_cache()
    return True


def stacked_suffix_renames(wireless, radios):
    """Corrections for APs whose name carries more than one band suffix.

    "photonicat2-46a4d8dd-5g-5g" is not a name anybody chooses; it is what the
    old branding produced, and because branding only ever touches a stock
    "OpenWrt" section the wrong name would otherwise stay on air for good --
    the user's only way out is renaming the network by hand and re-pairing
    every client. Repairing it is therefore worth the one rule we have to
    break (don't touch a configured SSID), but only for the doubled case: a
    single "-5g" is an ordinary choice and is left alone.

    Returns [(vif, old_ssid, new_ssid)], empty when everything looks sane.
    """
    renames = []
    for radio in radios:
        for vif in ap_vifs(wireless, radio):
            ssid = wireless.get("wireless.{}.ssid".format(vif), "")
            stem = ssid_stem(ssid)
            if _strip_one_band_suffix(ssid) == stem:
                continue  # no suffix at all, or exactly one
            fixed = ap_ssid_for(wireless, radio, stem)
            if fixed and fixed != ssid:
                renames.append((vif, ssid, fixed))
    return renames


def _relay_ap_backup(radio):
    """The AP identity wifi_wan snapshotted before this radio became the
    upstream relay, or None."""
    try:
        from pcat_util import wifi_wan
        return wifi_wan.saved_ap_identity(radio)
    except Exception as e:
        pcat_logger.logger.warning(
            "wifi_radio: could not read relay AP backup: %s", e)
        return None


def _relay_radio_backup(radio):
    """The band/htmode/channel snapshotted before this radio became the
    upstream relay, or None."""
    try:
        from pcat_util import wifi_wan
        return wifi_wan.saved_radio_state(radio)
    except Exception as e:
        pcat_logger.logger.warning(
            "wifi_radio: could not read relay radio backup: %s", e)
        return None


def active_relay_radio():
    """The radio currently carrying the upstream STA uplink, or None.

    wifi_wan owns that radio for as long as the relay runs: it parks the AP
    (`default_<radio>.disabled=1`) on purpose and tunes the radio to the
    *upstream's* band and channel. Both look exactly like the damage this
    module repairs -- an AP that is off, on a band the customer did not pick
    -- so the relay radio has to be recognised and left alone, or the repair
    un-parks the AP behind wifi_wan's back and retunes the radio off the
    upstream's channel, taking the WAN uplink down with it.
    """
    try:
        from pcat_util import wifi_wan
        return wifi_wan.active_sta_radio()
    except Exception as e:
        pcat_logger.logger.warning(
            "wifi_radio: could not read the relay radio: %s", e)
        return None


def restore_regenerated_bands(wireless, radios):
    """Put a regenerated radio back on the band the customer had it on.

    A section the firmware rewrites comes back on the firmware's default band,
    which for the onboard module here meant 5 GHz where the customer had 2.4.
    Restoring the name and passphrase but leaving the radio on a band its
    hardware may not even support would "fix" it into being just as missing.

    Only radios whose AP was factory-stock are touched -- a band the customer
    chose themselves is left alone -- and only from the relay's own snapshot.
    Returns the radios written.
    """
    changed = []
    for radio in radios:
        if not any(is_stock_ap(wireless, v) for v in ap_vifs(wireless, radio)):
            continue
        state = _relay_radio_backup(radio)
        if not state:
            continue
        for key, value in sorted(state.items()):
            uci_set("wireless.{}.{}={}".format(radio, key, value))
        changed.append(radio)
    return changed


def _sibling_identity(wireless, radio, radios):
    """A configured AP on some *other* radio, to copy an identity from."""
    for other in radios:
        if other == radio:
            continue
        for vif in ap_vifs(wireless, other):
            if is_stock_ap(wireless, vif):
                continue
            return {
                "ssid": wireless.get("wireless.{}.ssid".format(vif), ""),
                "encryption": wireless.get(
                    "wireless.{}.encryption".format(vif), "none"),
                "key": wireless.get("wireless.{}.key".format(vif), ""),
            }
    return None


def restore_stock_aps(wireless, radios, skip_vifs=()):
    """Put the device's own identity back on every factory-stock AP section.

    brand_new_ap() only ever ran against radios that *this* boot's `wifi
    config` added. An AP the firmware regenerated on its own was left alone --
    and that is the common case, not the rare one: a radio whose section is
    pruned while its AP is parked for the upstream relay comes back as stock
    'OpenWrt' with `encryption none`. The result was a hotspot that had lost
    the customer's name and passphrase and either sat switched off for good or
    went on the air with no passphrase at all.

    The customer's own settings are recovered from the relay's backup when
    there is one -- that snapshot is the only place they still exist -- and
    otherwise derived from the other radio the way the firmware names them.

    Returns [(radio, vif, secured)] for the sections written, where `secured`
    says whether a passphrase came with the identity -- the caller must not
    switch on a radio it could only give a name to. The caller commits.

    `skip_vifs` are sections already written this pass by restore_saved_aps,
    whose remembered settings are the customer's own and must not be replaced
    by an identity derived from the neighbouring radio.
    """
    branded = []
    for radio in radios:
        vifs = [v for v in ap_vifs(wireless, radio)
                if is_stock_ap(wireless, v) and v not in skip_vifs]
        if not vifs:
            continue
        template = _relay_ap_backup(radio)
        if template:
            # Verbatim: this is this radio's own name from before the relay
            # took it over, so it needs no re-suffixing.
            ssid = template.get("ssid", "")
        else:
            template = _sibling_identity(wireless, radio, radios)
            if not template:
                continue  # nothing to inherit; caller leaves the radio off
            ssid = ap_ssid_for(wireless, radio, ssid_stem(template["ssid"]))
        if not ssid:
            continue
        encryption = template.get("encryption") or "none"
        key = template.get("key") or ""
        secured = bool(key) and encryption != "none"
        for vif in vifs:
            uci_set("wireless.{}.ssid={}".format(vif, ssid))
            uci_set("wireless.{}.encryption={}".format(vif, encryption))
            if key:
                uci_set("wireless.{}.key={}".format(vif, key))
            branded.append((radio, vif, secured))
    return branded


def enable_radio(radio, vifs):
    """Clear `disabled` on the radio and its AP vifs. Commit is verified."""
    uci_set("wireless.{}.disabled=0".format(radio))
    for vif in vifs:
        uci_set("wireless.{}.disabled=0".format(vif))
    if not uci_commit("wireless"):
        return False
    fresh = uci_show_dict("wireless", max_age=0)
    return is_enabled(fresh, radio, vifs)


def reload_wifi():
    try:
        subprocess.run(["/sbin/wifi", "reload"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=30)
    except Exception as e:
        pcat_logger.logger.warning("wifi_radio: wifi reload failed: %s", e)


# ── the reconcile ────────────────────────────────────────────────────────────

def declared_radio_count():
    """How many radios this board says it has, per /etc/board.json.

    The board file is written long before userspace starts, so unlike
    phy_count() it does not climb while we are looking at it. 0 when it cannot
    be read, which just means the live count is all we have.
    """
    try:
        from pcat_util import command_parser
        return len(command_parser._read_board_phy_capabilities())
    except Exception:
        return 0


def expected_radio_count(declared=None):
    """How many radio sections the config should end up with.

    `declared` is accepted pre-read because the poll loop below asks this
    question every couple of seconds and the board file cannot change while
    it does.
    """
    if declared is None:
        declared = declared_radio_count()
    return max(phy_count(), declared)


def _read_wireless():
    """The wireless config, once the boot has finished writing it.

    This module assumed the web app "comes up long after netifd". It does not.
    The reconcile has been caught running with one of the two radios still
    un-enumerated, finding nothing wrong, and returning -- and the firmware
    then regenerated that radio's section as factory stock a moment later,
    with nobody left to notice. Silently, on exactly the boot it exists for.

    So wait for three things: every radio the board declares to have actually
    enumerated (a phy appearing is what makes the firmware rewrite the config
    in the first place), a section for each of them, and two identical reads
    in a row so we never act on a half-written config.

    Waiting is safe -- this is the deferred-startup thread -- and bounded, so
    a box whose second radio is genuinely dead still finishes booting.
    """
    declared = declared_radio_count()
    deadline = time.monotonic() + WIRELESS_WAIT_SECONDS
    previous = None
    while True:
        wireless = uci_show_dict("wireless", max_age=0)
        settled = (wireless
                   and phy_count() >= declared
                   and len(wifi_devices(wireless)) >= expected_radio_count(declared)
                   and wireless == previous)
        if settled:
            return wireless
        if time.monotonic() >= deadline:
            pcat_logger.logger.warning(
                "wifi_radio: wireless config still unsettled after %ss "
                "(%d radios, %d/%d phys); reconciling on what is there",
                WIRELESS_WAIT_SECONDS, len(wifi_devices(wireless or {})),
                phy_count(), declared)
            return wireless
        previous = wireless
        time.sleep(WIRELESS_POLL_SECONDS)


def _schedule_recheck():
    """Run the reconcile again at each RECHECK_SCHEDULE offset after boot.

    Waiting for the config to settle narrows the race but cannot close it: the
    firmware rewrites /etc/config/wireless at times of its own choosing during
    boot -- measured at ~100s on this hardware -- and a first pass can always
    be overtaken by one of them. The cost of losing that race is a customer
    whose WiFi is gone, so pay for a few cheap second looks rather than rely on
    the timing holding.

    Nothing is written unless something is actually wrong, so on a healthy
    device each pass is one `uci show`. Late passes cannot fight the user
    either: a radio someone switched off themselves still reads as their own
    configured section, which the repair never touches.
    """
    def run():
        # Offsets are measured from boot, not from the end of the previous
        # pass: a pass that sat out the full settle wait would otherwise push
        # every later one out by a minute and miss the window it was aimed at.
        start = time.monotonic()
        for offset in RECHECK_SCHEDULE:
            time.sleep(max(0, start + offset - time.monotonic()))
            try:
                note = _reconcile()
                if note:
                    pcat_logger.logger.info("WiFi radio recheck: %s", note)
            except Exception as e:
                pcat_logger.logger.error("WiFi radio recheck failed: %r", e)

    thread = threading.Thread(target=run, name="wifi-radio-recheck",
                              daemon=True)
    thread.start()
    return thread


# How many times running the keeper may repair the same thing before it stops
# reloading. Past this something outside is rewriting the config faster than we
# can fix it, and bouncing every client off the AP twice an hour on top of that
# helps nobody -- the config is still corrected, and the log says why.
KEEPER_REPAIR_LIMIT = 3
_keeper_repairs = 0


def keep_ap_settings():
    """Re-assert the remembered hotspot settings. A keeper tick, not a boot pass.

    startup() and its rechecks only cover the first few minutes after a
    restart, and the config does not only revert at boot -- the report this
    exists for was "the WiFi name and password go back to the defaults when I
    restart, and sometimes without restarting". This is the same repair on a
    slow tick: one `uci show` on a healthy device, and nothing written unless
    the hotspot has actually reverted.

    Returns a short string when something was repaired, else None.
    """
    global _keeper_repairs
    wireless = uci_show_dict("wireless", max_age=0)
    if not wireless:
        return None
    restored = restore_saved_aps(wireless, wifi_devices(wireless))
    if not restored:
        _keeper_repairs = 0
        return None
    radios = ",".join(sorted({radio for radio, _v, _s in restored}))
    if not uci_commit("wireless"):
        return "hotspot restore commit failed for " + radios
    _keeper_repairs += 1
    if _keeper_repairs > KEEPER_REPAIR_LIMIT:
        pcat_logger.logger.warning(
            "wifi_radio: something keeps rewriting the %s hotspot config "
            "(%d times); leaving the radio alone rather than reloading again",
            radios, _keeper_repairs)
        return "restored saved hotspot settings on {} (no reload)".format(radios)
    reload_wifi()
    return "restored saved hotspot settings on " + radios


def startup():
    """Reconcile radio on/off state at boot, then again a little later.

    Returns a short string for the log, or None when nothing needed doing --
    which is the normal case on every boot after the first. The second pass
    logs separately; see _schedule_recheck.
    """
    note = _reconcile()
    if RECHECK_SCHEDULE:
        _schedule_recheck()
    return note


def _reconcile():
    """One pass of the reconcile."""
    wireless = _read_wireless()
    if not wireless:
        return None  # uci unreadable; a later boot can try again

    radios = wifi_devices(wireless)
    relay = active_relay_radio()
    notes = []

    # A phy with no section of its own (the first-boot enumeration race), or a
    # section the firmware pruned out from under a still-present AP iface.
    orphans = orphaned_radios(wireless)
    if (phy_count() > len(radios) or orphans) and detect_new_radios():
        wireless = uci_show_dict("wireless", max_age=0)
        added = [r for r in wifi_devices(wireless) if r not in radios]
        radios = wifi_devices(wireless)
        for radio in added:
            notes.append("detected " + radio)
        recovered = [r for r in orphans if r in added]
        if recovered:
            notes.append("recovered pruned section for " + ",".join(recovered))

    # Two repairs that never heal on their own, because both leave a section
    # that later passes for the customer's own: an AP the firmware regenerated
    # as factory stock, and one an earlier branding run left named "...-5g-5g".
    repaired = set()
    insecure = set()
    renames = stacked_suffix_renames(wireless, radios)
    for vif, _old, new in renames:
        uci_set("wireless.{}.ssid={}".format(vif, new))
    # Not the relay radio: its band is the *upstream's*, set by wifi_wan, and
    # the snapshot we would put back is the pre-relay one. Writing that while
    # the relay is live retunes the radio off the upstream's channel and drops
    # the WAN link. Its parked AP section is still worth re-identifying, which
    # is safe -- an AP that is disabled does not go on the air.
    retuned = restore_regenerated_bands(
        wireless, [r for r in radios if r != relay])
    # The customer's own saved settings first: they are a better answer than
    # anything restore_stock_aps can derive, and the sections they cover are
    # then left to them.
    restored = restore_saved_aps(wireless, radios)
    branded = restored + restore_stock_aps(
        wireless, radios, skip_vifs={vif for _r, vif, _s in restored})
    changed_config = False
    if renames or branded or retuned:
        if uci_commit("wireless"):
            wireless = uci_show_dict("wireless", max_age=0)
            changed_config = True
            notes.extend("renamed {} '{}' -> '{}'".format(vif, old, new)
                         for vif, old, new in renames)
            repaired = {radio for radio, _vif, _sec in branded}
            insecure = {radio for radio, _vif, sec in branded if not sec}
            if restored:
                notes.append("restored saved hotspot settings on "
                             + ",".join(sorted({r for r, _v, _s in restored})))
            if repaired - {r for r, _v, _s in restored}:
                notes.append("restored AP identity on "
                             + ",".join(sorted(
                                 repaired - {r for r, _v, _s in restored})))
            if retuned:
                notes.append("restored band on " + ",".join(sorted(retuned)))
        else:
            notes.append("ssid repair commit failed")

    intents = get_intents()
    changed_intents = False
    enabled_any = False

    for radio in radios:
        if radio == relay:
            # Off because wifi_wan parked it to carry the uplink, not because
            # the firmware lost it. Enabling would un-park an AP wifi_wan is
            # holding down, and reading the parked AP as an off switch would
            # pin the user's intent to "off" for good -- so the hotspot would
            # not come back when the relay does end.
            continue
        if not radio_present(wireless, radio):
            continue
        vifs = ap_vifs(wireless, radio)
        if not vifs:
            # Nothing to serve -- an STA-only radio is wifi_wan's business.
            continue

        if is_enabled(wireless, radio, vifs):
            if intents.get(radio) is not True:
                intents[radio] = True
                changed_intents = True
            if any(is_stock_ap(wireless, v) for v in vifs):
                # Already on air with no identity and no passphrase. Switching
                # it off would drop whoever is on it, so say so and leave it.
                pcat_logger.logger.warning(
                    "wifi_radio: %s is on air with a factory-stock AP", radio)
            continue

        if radio in insecure:
            # A name was recovered but no passphrase came with it. Switching it
            # on would bridge an unauthenticated network onto the LAN, which is
            # worse than the missing radio we set out to fix. Checked before
            # the stock test below because it is the more specific verdict on
            # the same section.
            notes.append(radio + " left off: recovered AP has no passphrase")
            continue

        if any(is_stock_ap(wireless, v) for v in vifs):
            # Nothing could be recovered for it above, so switching it on would
            # put an open factory network on the air. Leave it off.
            notes.append(radio + " left off: no AP identity")
            continue

        remembered = intents.get(radio)
        # `repaired`: this section was factory-stock moments ago, so its being
        # off is the firmware's doing. Reading that as a deliberate switch-off
        # is what pinned a pruned radio off permanently.
        if remembered is None or radio in repaired:
            if enable_radio(radio, vifs):
                intents[radio] = True
                changed_intents = True
                enabled_any = True
                notes.append("enabled " + radio)
            else:
                # Left as unknown intent deliberately: retry on the next boot
                # rather than mistake our own failure for a user's choice.
                notes.append("enable failed for " + radio)
        elif remembered is True:
            intents[radio] = False
            changed_intents = True
            notes.append("{} switched off elsewhere; leaving it off".format(radio))

    if changed_intents:
        save_intents(intents)
    if enabled_any or changed_config:
        reload_wifi()
    return "; ".join(notes) if notes else None
