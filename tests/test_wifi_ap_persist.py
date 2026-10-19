#!/usr/bin/env python3
"""Unit tests for hotspot settings surviving a firmware config rewrite.

The reported bug (photonicat.com forum, r7897): "the WiFi SSID and password I
configure go back to the defaults after a restart -- sometimes without even
restarting". Two things let that happen and both are covered here:

  * the save was fire-and-forget. A `uci commit` that leaves the change in the
    /tmp/.uci staging delta works for the rest of the session and is gone at
    the next reboot, and nobody checked -- so the page reported success either
    way (openwrt.uci_commit_verified, wifi_radio.save_ap_config);
  * nothing put the settings back. The firmware regenerates
    /etc/config/wireless with the *factory* identity, which does not look
    stock, so no repair here touched it and the customer's network name and
    password were simply gone (wifi_radio.restore_saved_aps).

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_wifi_ap_persist`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

# pcat_logger opens its log file at import time, which fails in sandboxed test
# runs -- mock it before anything imports it, same idiom as test_wifi_radio.
_logger_patch = patch.dict('sys.modules', {
    'pcat_logger': MagicMock(),
    'pcat_util.pcat_logger': MagicMock(),
})
_logger_patch.start()

from pcat_util import openwrt  # noqa: E402
from pcat_util import wifi_radio as wr  # noqa: E402


HOSTNAME = "photonicat2-4a99abb9"

# Two radios with the customer's own hotspot on both -- the healthy state.
CONFIGURED = {
    "wireless.radio0": "wifi-device",
    "wireless.radio0.path": "platform/soc/wifi0",
    "wireless.default_radio0": "wifi-iface",
    "wireless.default_radio0.device": "radio0",
    "wireless.default_radio0.mode": "ap",
    "wireless.default_radio0.ssid": "HomeNetwork",
    "wireless.default_radio0.encryption": "psk2",
    "wireless.default_radio0.key": "hunter2hunter2",
    "wireless.radio1": "wifi-device",
    "wireless.radio1.path": "soc/22000000.pcie/pci0000:00/0000:00:01.0",
    "wireless.default_radio1": "wifi-iface",
    "wireless.default_radio1.device": "radio1",
    "wireless.default_radio1.mode": "ap",
    "wireless.default_radio1.ssid": "HomeNetwork-5g",
    "wireless.default_radio1.encryption": "psk2",
    "wireless.default_radio1.key": "hunter2hunter2",
}

SAVED_R0 = {"ssid": "HomeNetwork", "encryption": "psk2",
            "key": "hunter2hunter2", "hidden": ""}
# What the customer's first save replaced: the identity the device shipped with.
FACTORY_R0 = {"ssid": HOSTNAME, "encryption": "psk2",
              "key": "photonicat", "hidden": ""}


def regenerated(cfg=None, radio="radio0", ssid=HOSTNAME, key="photonicat"):
    """`cfg` with one radio's AP section back on the factory identity, which is
    what the firmware writes when it regenerates /etc/config/wireless."""
    out = dict(CONFIGURED if cfg is None else cfg)
    out["wireless.default_{}.ssid".format(radio)] = ssid
    out["wireless.default_{}.key".format(radio)] = key
    return out


class _StoreCase(unittest.TestCase):
    """Backs wifi_radio's user-config store with an in-memory dict, and
    captures every uci write instead of forking `uci`."""

    def setUp(self):
        self.config_store = {}
        fake_config = MagicMock()
        fake_config.get_user_config_key.side_effect = \
            lambda key, default=None: self.config_store.get(key, default)
        fake_config.set_user_config_key.side_effect = \
            lambda key, value: self.config_store.__setitem__(key, value)

        self.sets = []
        self.deletes = []
        patches = [
            patch.dict('sys.modules', {'pcat_config': fake_config}),
            patch.object(wr, 'uci_set',
                         lambda kv: self.sets.append(kv) or True),
            patch.object(wr, 'uci_delete',
                         lambda k: self.deletes.append(k) or True),
            patch.object(openwrt, 'uci_get', lambda key: HOSTNAME),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def written(self):
        """{option: value} of the AP options written this pass."""
        out = {}
        for kv in self.sets:
            key, _, value = kv.partition("=")
            out[key] = value
        return out


# ---------- what a save remembers --------------------------------------------


class RememberedSettingsTests(_StoreCase):

    def test_first_save_records_what_it_replaced_as_the_origin(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        entry = wr.get_ap_settings()["radio0"]
        self.assertEqual(entry["ssid"], "HomeNetwork")
        self.assertEqual(entry["key"], "hunter2hunter2")
        self.assertEqual(entry["origin"]["ssid"], HOSTNAME,
                         "the identity the first save replaced is the factory one")

    def test_later_saves_keep_the_first_origin(self):
        # Otherwise every save overwrites the factory record with the previous
        # user value, and a regenerated section stops being recognisable.
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        wr.remember_ap_settings("radio0", dict(SAVED_R0, ssid="Renamed"),
                                replaced=SAVED_R0)
        entry = wr.get_ap_settings()["radio0"]
        self.assertEqual(entry["ssid"], "Renamed")
        self.assertEqual(entry["origin"]["ssid"], HOSTNAME)

    def test_settings_are_kept_per_radio(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        wr.remember_ap_settings("radio1", dict(SAVED_R0, ssid="HomeNetwork-5g"))
        self.assertEqual(wr.saved_ap_settings("radio0")["ssid"], "HomeNetwork")
        self.assertEqual(wr.saved_ap_settings("radio1")["ssid"], "HomeNetwork-5g")

    def test_nameless_entry_is_not_a_usable_identity(self):
        wr.remember_ap_settings("radio0", {"ssid": "", "key": "x"})
        self.assertIsNone(wr.saved_ap_settings("radio0"),
                          "restoring an empty SSID would take the AP off the air")

    def test_corrupt_store_is_ignored_rather_than_raising(self):
        self.config_store[wr.AP_SETTINGS_KEY] = ["not", "a", "dict"]
        self.assertEqual(wr.get_ap_settings(), {})
        self.assertIsNone(wr.saved_ap_settings("radio0"))


# ---------- putting them back ------------------------------------------------


class RestoreSavedApTests(_StoreCase):

    def _remember(self, radio="radio0", identity=None, replaced=FACTORY_R0):
        wr.remember_ap_settings(radio, identity or SAVED_R0, replaced=replaced)

    def test_regenerated_factory_identity_is_restored(self):
        self._remember()
        restored = wr.restore_saved_aps(regenerated(), ["radio0", "radio1"])
        self.assertEqual([(r, v) for r, v, _s in restored],
                         [("radio0", "default_radio0")])
        written = self.written()
        self.assertEqual(written["wireless.default_radio0.ssid"], "HomeNetwork")
        self.assertEqual(written["wireless.default_radio0.key"], "hunter2hunter2")
        self.assertEqual(written["wireless.default_radio0.encryption"], "psk2")

    def test_stock_openwrt_section_is_restored(self):
        self._remember()
        cfg = regenerated(ssid="OpenWrt", key="")
        wr.restore_saved_aps(cfg, ["radio0"])
        self.assertEqual(self.written()["wireless.default_radio0.ssid"],
                         "HomeNetwork")

    def test_device_named_ap_is_restored_without_any_origin(self):
        # The customer never used this page: their SSID was learned from the
        # live config, so there is no `origin` to match. The device's own
        # factory name is what gives the regeneration away.
        wr.restore_saved_aps(CONFIGURED, ["radio0"])          # learns it
        self.sets = []
        restored = wr.restore_saved_aps(regenerated(), ["radio0"])
        self.assertEqual(len(restored), 1)
        self.assertEqual(self.written()["wireless.default_radio0.ssid"],
                         "HomeNetwork")

    def test_a_matching_section_is_not_touched(self):
        self._remember()
        self.assertEqual(wr.restore_saved_aps(CONFIGURED, ["radio0", "radio1"]), [])
        self.assertEqual(self.sets, [])
        self.assertEqual(self.deletes, [])

    def test_an_edit_made_elsewhere_is_kept_and_learned(self):
        # Somebody renamed the hotspot in Luci. Reverting that at the next boot
        # would be this repair fighting the user; adopting it means their name
        # is what comes back if the firmware later regenerates the section.
        self._remember()
        elsewhere = dict(CONFIGURED)
        elsewhere["wireless.default_radio0.ssid"] = "SetInLuci"
        elsewhere["wireless.default_radio0.key"] = "somethingelse"
        self.assertEqual(wr.restore_saved_aps(elsewhere, ["radio0"]), [])
        self.assertEqual(self.sets, [])
        self.assertEqual(wr.saved_ap_settings("radio0")["ssid"], "SetInLuci")
        self.assertEqual(wr.get_ap_settings()["radio0"]["origin"]["ssid"], HOSTNAME,
                         "learning must not lose the factory record")

    def test_nothing_remembered_means_nothing_written(self):
        self.assertEqual(wr.restore_saved_aps(regenerated(), ["radio0"]), [])
        self.assertEqual(self.sets, [])

    def test_factory_config_is_never_learned_as_the_customers(self):
        # A device nobody has configured: the live identity IS the factory one,
        # and recording it would make a later restore a no-op that cements the
        # bug it exists to fix.
        wr.restore_saved_aps(regenerated(), ["radio0"])
        self.assertIsNone(wr.saved_ap_settings("radio0"))

    def test_hidden_is_cleared_when_the_saved_hotspot_is_visible(self):
        self._remember()
        cfg = regenerated()
        cfg["wireless.default_radio0.hidden"] = "1"
        wr.restore_saved_aps(cfg, ["radio0"])
        self.assertIn("wireless.default_radio0.hidden", self.deletes)

    def test_a_restored_sae_hotspot_gets_pmf_and_ocv_back(self):
        # PMF is derived from the encryption at every write site, and the
        # boot-time restore is a write site: putting a WPA3 hotspot back
        # without ieee80211w=2/ocv=0 restores a network clients cannot use.
        self._remember(identity={"ssid": "HomeNetwork", "encryption": "sae",
                                 "key": "hunter2hunter2", "hidden": ""})
        wr.restore_saved_aps(regenerated(), ["radio0"])
        written = self.written()
        self.assertEqual(written["wireless.default_radio0.ieee80211w"], "2")
        self.assertEqual(written["wireless.default_radio0.ocv"], "0")

    def test_a_restored_psk2_hotspot_clears_stale_pmf(self):
        self._remember()  # psk2 identity
        cfg = regenerated()
        cfg["wireless.default_radio0.ieee80211w"] = "2"
        cfg["wireless.default_radio0.ocv"] = "0"
        wr.restore_saved_aps(cfg, ["radio0"])
        self.assertIn("wireless.default_radio0.ieee80211w", self.deletes)
        self.assertIn("wireless.default_radio0.ocv", self.deletes)

    def test_an_open_network_the_user_chose_counts_as_secured(self):
        # `secured` marks "a name was recovered but the passphrase was lost",
        # which makes the reconcile leave the radio off. A passphrase-less
        # hotspot the customer asked for is not that case.
        self._remember(identity={"ssid": "OpenGuest", "encryption": "none",
                                 "key": "", "hidden": ""})
        restored = wr.restore_saved_aps(regenerated(), ["radio0"])
        self.assertEqual([sec for _r, _v, sec in restored], [True])

    def test_a_second_ap_on_the_radio_is_left_out_of_it(self):
        # A guest network added in Luci is not this page's hotspot. Treating it
        # as one would have the two overwriting each other's remembered
        # settings on alternate passes.
        self._remember()
        cfg = dict(CONFIGURED)
        cfg["wireless.guest0"] = "wifi-iface"
        cfg["wireless.guest0.device"] = "radio0"
        cfg["wireless.guest0.mode"] = "ap"
        cfg["wireless.guest0.ssid"] = "GuestWiFi"
        cfg["wireless.guest0.key"] = "guestpassword"
        self.assertEqual(wr.restore_saved_aps(cfg, ["radio0"]), [])
        self.assertEqual(self.sets, [])
        self.assertEqual(wr.saved_ap_settings("radio0")["ssid"], "HomeNetwork",
                         "the guest network must not become the remembered one")

    def test_a_radio_with_no_hotspot_section_is_skipped(self):
        self._remember()
        self.assertEqual(wr.restore_saved_aps({"wireless.radio0": "wifi-device"},
                                              ["radio0"]), [])
        self.assertEqual(self.sets, [])

    def test_the_relay_sta_section_is_never_restored_over(self):
        # wifi_wan owns that section; ap_vifs only ever yields mode=ap.
        self._remember()
        cfg = regenerated()
        cfg["wireless.wifinet_sta"] = "wifi-iface"
        cfg["wireless.wifinet_sta.device"] = "radio0"
        cfg["wireless.wifinet_sta.mode"] = "sta"
        cfg["wireless.wifinet_sta.ssid"] = "UpstreamHotspot"
        wr.restore_saved_aps(cfg, ["radio0"])
        self.assertNotIn("wireless.wifinet_sta.ssid", self.written())

    def test_restore_writes_identity_only_never_state(self):
        # A parked AP (its radio relaying the upstream) or a switched-off one
        # must stay exactly as parked/off as it was: putting the name and
        # passphrase back is safe on a disabled section, un-parking it behind
        # wifi_wan's back is not -- and band/channel belong to the relay while
        # it runs.
        self._remember()
        cfg = regenerated()
        cfg["wireless.radio0.disabled"] = "1"
        cfg["wireless.default_radio0.disabled"] = "1"
        cfg["wireless.radio0.band"] = "5g"
        cfg["wireless.radio0.channel"] = "149"
        restored = wr.restore_saved_aps(cfg, ["radio0"])
        self.assertEqual(len(restored), 1, "identity is still repaired")
        touched = set(self.written()) | set(self.deletes)
        for key in ("wireless.radio0.disabled",
                    "wireless.default_radio0.disabled",
                    "wireless.radio0.band",
                    "wireless.radio0.channel",
                    "wireless.radio0.htmode"):
            self.assertNotIn(key, touched,
                             "{} is not the restore's to change".format(key))


# ---------- the boot reconcile drives it -------------------------------------


class ReconcileRestoresHotspotTests(_StoreCase):
    """The restore has to be part of the boot pass, committed and reloaded --
    a repair that only exists as a function nobody calls fixes nothing."""

    def setUp(self):
        super().setUp()
        self.commits = []
        self.reloads = []
        patches = [
            patch.object(wr, 'uci_commit',
                         lambda cfg: self.commits.append(cfg) or True),
            patch.object(wr, 'reload_wifi',
                         lambda: self.reloads.append(True)),
            patch.object(wr, 'radio_present', lambda cfg, radio: True),
            patch.object(wr, 'detect_new_radios', lambda: False),
            patch.object(wr, '_relay_ap_backup', lambda radio: None),
            patch.object(wr, '_relay_radio_backup', lambda radio: None),
            patch.object(wr, 'active_relay_radio', lambda: None),
            patch.object(wr, 'get_intents', lambda: {"radio0": True, "radio1": True}),
            patch.object(wr, 'save_intents', lambda intents: True),
            patch.object(wr, 'WIRELESS_WAIT_SECONDS', 0),
            patch.object(wr, 'RECHECK_SCHEDULE', ()),
            patch.object(wr, 'declared_radio_count', lambda: 0),
            patch.object(wr, 'phy_count', lambda: 2),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _startup(self, cfg):
        with patch.object(wr, 'uci_show_dict', lambda *a, **k: cfg):
            return wr.startup()

    def test_boot_puts_the_hotspot_back_and_reloads(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        note = self._startup(regenerated())
        self.assertEqual(self.written()["wireless.default_radio0.ssid"],
                         "HomeNetwork")
        self.assertIn("wireless", self.commits)
        self.assertTrue(self.reloads, "the AP is only on air again after a reload")
        self.assertIn("restored saved hotspot settings on radio0", note)

    def test_a_healthy_boot_writes_nothing(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        note = self._startup(CONFIGURED)
        self.assertEqual(self.sets, [])
        self.assertEqual(self.commits, [])
        self.assertIsNone(note)

    def test_saved_settings_win_over_the_sibling_derived_identity(self):
        # restore_stock_aps names a stock section after the other radio, which
        # here would make it 'HomeNetwork'. For a section we hold the
        # customer's own settings for, that guess must not overwrite them.
        wr.remember_ap_settings(
            "radio0", {"ssid": "MainAP", "encryption": "psk2",
                       "key": "differentpass", "hidden": ""},
            replaced=FACTORY_R0)
        self._startup(regenerated(ssid="OpenWrt", key=""))
        written = self.written()
        self.assertEqual(written["wireless.default_radio0.ssid"], "MainAP")
        self.assertEqual(written["wireless.default_radio0.key"], "differentpass")


# ---------- the keeper tick --------------------------------------------------


class KeepApSettingsTests(_StoreCase):
    """The config does not only revert at boot -- "sometimes without even
    restarting" -- so the repair has to run on a tick too."""

    def setUp(self):
        super().setUp()
        self.commits = []
        self.reloads = []
        self.commit_ok = True
        wr._keeper_repairs = 0
        patches = [
            patch.object(wr, 'uci_commit',
                         lambda cfg: self.commits.append(cfg) or self.commit_ok),
            patch.object(wr, 'reload_wifi', lambda: self.reloads.append(True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, wr, '_keeper_repairs', 0)

    def _keep(self, cfg):
        with patch.object(wr, 'uci_show_dict', lambda *a, **k: cfg):
            return wr.keep_ap_settings()

    def test_a_mid_session_revert_is_repaired_and_reloaded(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        note = self._keep(regenerated())
        self.assertEqual(self.written()["wireless.default_radio0.ssid"],
                         "HomeNetwork")
        self.assertEqual(self.commits, ["wireless"])
        self.assertTrue(self.reloads)
        self.assertIn("radio0", note)

    def test_a_healthy_tick_costs_one_read_and_says_nothing(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        self.assertIsNone(self._keep(CONFIGURED))
        self.assertEqual(self.sets, [])
        self.assertEqual(self.commits, [])
        self.assertEqual(self.reloads, [])

    def test_an_unreadable_config_is_left_alone(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        self.assertIsNone(self._keep({}))
        self.assertEqual(self.sets, [])

    def test_a_failed_commit_is_reported_and_not_reloaded(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        self.commit_ok = False
        note = self._keep(regenerated())
        self.assertIn("commit failed", note)
        self.assertEqual(self.reloads, [])

    def test_a_config_that_keeps_reverting_stops_bouncing_clients(self):
        # Something outside is rewriting it faster than we fix it. Keep the
        # config right, but stop dropping every client off the AP twice an hour.
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        for _ in range(wr.KEEPER_REPAIR_LIMIT):
            self._keep(regenerated())
        self.assertEqual(len(self.reloads), wr.KEEPER_REPAIR_LIMIT)
        note = self._keep(regenerated())
        self.assertIn("no reload", note)
        self.assertEqual(len(self.reloads), wr.KEEPER_REPAIR_LIMIT)
        self.assertEqual(self.written()["wireless.default_radio0.ssid"],
                         "HomeNetwork", "the config is still corrected")

    def test_a_clean_tick_resets_the_repair_count(self):
        wr.remember_ap_settings("radio0", SAVED_R0, replaced=FACTORY_R0)
        self._keep(regenerated())
        self._keep(CONFIGURED)
        self.assertEqual(wr._keeper_repairs, 0)


# ---------- the save path ----------------------------------------------------


class SaveApConfigTests(_StoreCase):

    def setUp(self):
        super().setUp()
        self.commit_ok = True
        patches = [patch.object(wr, 'uci_commit_verified', self._commit)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.expected = None

    def _commit(self, config, expect=(), attempts=2):
        self.expected = list(expect)
        return self.commit_ok

    RADIO_OPTS = {"band": "2g", "htmode": "HT20", "channel": "6", "country": "US"}

    def _save(self, cfg, identity, radio="radio0"):
        return wr.save_ap_config(radio, cfg, self.RADIO_OPTS, identity)

    def test_a_save_writes_the_resolved_section_not_a_guessed_one(self):
        # This layout names the AP section 'wifinet0'. Writing to
        # 'default_radio0' would fail -- and used to fail silently, which is a
        # save the page reports as done that changes nothing.
        cfg = {
            "wireless.radio0": "wifi-device",
            "wireless.wifinet0": "wifi-iface",
            "wireless.wifinet0.device": "radio0",
            "wireless.wifinet0.mode": "ap",
            "wireless.wifinet0.ssid": "Old",
        }
        ok, vif = self._save(cfg, {"ssid": "New", "encryption": "psk2",
                                   "key": "12345678", "hidden": ""})
        self.assertTrue(ok)
        self.assertEqual(vif, "wifinet0")
        self.assertEqual(self.written()["wireless.wifinet0.ssid"], "New")
        self.assertNotIn("wireless.default_radio0.ssid", self.written())

    def test_a_pruned_ap_section_is_recreated(self):
        cfg = {"wireless.radio0": "wifi-device"}
        ok, vif = self._save(cfg, {"ssid": "New", "encryption": "psk2",
                                   "key": "12345678", "hidden": ""})
        self.assertTrue(ok)
        written = self.written()
        self.assertEqual(written["wireless.default_radio0"], "wifi-iface")
        self.assertEqual(written["wireless.default_radio0.mode"], "ap")
        self.assertEqual(written["wireless.default_radio0.network"], "lan")
        self.assertEqual(written["wireless.default_radio0.ssid"], "New")

    def test_a_save_that_did_not_persist_is_reported_as_failed(self):
        self.commit_ok = False
        ok, _vif = self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                           "key": "12345678", "hidden": ""})
        self.assertFalse(ok, "the page must not report a save that isn't on flash")
        self.assertIsNone(wr.saved_ap_settings("radio0"),
                          "and must not remember settings the device never took")

    def test_a_failed_uci_write_fails_the_save(self):
        with patch.object(wr, 'uci_set', lambda kv: False):
            ok, _vif = self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                               "key": "12345678", "hidden": ""})
        self.assertFalse(ok)

    def test_a_successful_save_is_remembered_with_what_it_replaced(self):
        ok, _vif = self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                           "key": "12345678", "hidden": ""})
        self.assertTrue(ok)
        entry = wr.get_ap_settings()["radio0"]
        self.assertEqual(entry["ssid"], "New")
        self.assertEqual(entry["key"], "12345678")
        self.assertEqual(entry["origin"]["ssid"], "HomeNetwork")

    def test_an_empty_password_keeps_and_verifies_the_existing_one(self):
        ok, _vif = self._save(CONFIGURED, {"ssid": "Renamed", "encryption": "psk2",
                                           "key": None, "hidden": ""})
        self.assertTrue(ok)
        self.assertNotIn("wireless.default_radio0.key", self.written(),
                         "an untouched passphrase must not be rewritten")
        self.assertEqual(wr.saved_ap_settings("radio0")["key"], "hunter2hunter2",
                         "the remembered identity has to be the effective one")
        self.assertIn("hunter2hunter2", self.expected)

    def test_the_commit_is_verified_against_the_ssid_written(self):
        self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                "key": "12345678", "hidden": ""})
        self.assertIn("New", self.expected)

    def test_hidden_is_written_and_cleared(self):
        self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                "key": "12345678", "hidden": "1"})
        self.assertEqual(self.written()["wireless.default_radio0.hidden"], "1")
        self.sets, self.deletes = [], []
        self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                "key": "12345678", "hidden": ""})
        self.assertIn("wireless.default_radio0.hidden", self.deletes)

    def test_sae_gets_pmf_and_ocv_and_psk2_clears_them(self):
        # SAE without ieee80211w=2/ocv=0 is WPA3 clients cannot stay connected
        # to on this hardware -- and the pair must go away with SAE, or a
        # fallback to psk2 leaves PMF=required behind (the bee8920 regression).
        self._save(CONFIGURED, {"ssid": "New", "encryption": "sae",
                                "key": "12345678", "hidden": ""})
        written = self.written()
        self.assertEqual(written["wireless.default_radio0.ieee80211w"], "2")
        self.assertEqual(written["wireless.default_radio0.ocv"], "0")
        self.sets, self.deletes = [], []
        self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                "key": "12345678", "hidden": ""})
        self.assertIn("wireless.default_radio0.ieee80211w", self.deletes)
        self.assertIn("wireless.default_radio0.ocv", self.deletes)

    def test_the_radio_options_go_to_the_device_section(self):
        self._save(CONFIGURED, {"ssid": "New", "encryption": "psk2",
                                "key": "12345678", "hidden": ""})
        written = self.written()
        self.assertEqual(written["wireless.radio0.band"], "2g")
        self.assertEqual(written["wireless.radio0.htmode"], "HT20")
        self.assertEqual(written["wireless.radio0.channel"], "6")
        self.assertEqual(written["wireless.radio0.country"], "US")


# ---------- proving a commit reached flash -----------------------------------


class CommitVerifiedTests(unittest.TestCase):
    """openwrt.uci_commit_verified: the difference between a committed config
    and one that only exists in /tmp/.uci until the next reboot."""

    def setUp(self):
        self.commits = []

    def _run(self, file_text, expect, commit_ok=True):
        def _commit(config):
            self.commits.append(config)
            return commit_ok

        opened = []

        def _open(path, *a, **kw):
            opened.append(path)
            if isinstance(file_text, Exception):
                raise file_text
            import io
            return io.StringIO(file_text)

        with patch.object(openwrt, 'uci_commit', _commit), \
             patch('builtins.open', side_effect=_open):
            result = openwrt.uci_commit_verified("wireless", expect=expect)
        self.opened = opened
        return result

    def test_a_value_on_flash_is_a_real_commit(self):
        ok = self._run("\toption ssid 'HomeNetwork'\n", ["HomeNetwork"])
        self.assertTrue(ok)
        self.assertEqual(len(self.commits), 1, "no pointless second commit")
        self.assertEqual(self.opened, ["/etc/config/wireless"])

    def test_a_value_missing_from_flash_is_retried_then_reported(self):
        ok = self._run("\toption ssid 'photonicat2-4a99abb9'\n", ["HomeNetwork"])
        self.assertFalse(ok, "a staged-only change is the reboot-revert bug")
        self.assertEqual(len(self.commits), 2, "one retry before giving up")

    def test_every_expected_value_has_to_be_there(self):
        self.assertFalse(
            self._run("\toption ssid 'HomeNetwork'\n",
                      ["HomeNetwork", "hunter2hunter2"]),
            "the passphrase not landing is just as bad as the name",
        )

    def test_a_failed_commit_is_never_reported_as_landed(self):
        self.assertFalse(
            self._run("\toption ssid 'HomeNetwork'\n", ["HomeNetwork"],
                      commit_ok=False))

    def test_an_unreadable_file_falls_back_to_the_commit_result(self):
        # Refusing an otherwise good save because /etc/config cannot be read
        # helps nobody, and it must not loop retrying either.
        self.assertTrue(self._run(OSError("nope"), ["HomeNetwork"]))
        self.assertEqual(len(self.commits), 1)
        self.assertFalse(self._run(OSError("nope"), ["HomeNetwork"],
                                   commit_ok=False))

    def test_a_value_uci_would_escape_is_not_checked(self):
        # uci writes an embedded quote as 'it'\''s'; guessing at that spelling
        # would fail verification on a name that saved perfectly well.
        self.assertTrue(self._run("\toption ssid 'it'\\''s'\n", ["it's"]))

    def test_nothing_to_expect_still_verifies_the_commit(self):
        self.assertTrue(self._run("", []))
        self.assertFalse(self._run("", [], commit_ok=False))


if __name__ == "__main__":
    unittest.main()
