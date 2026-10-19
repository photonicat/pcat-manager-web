#!/usr/bin/env python3
"""Regression tests for issue #112 — "关闭上游WIFI功能后板载wifi ssid变成了openwrt".

Reproduction on r7875: use the upstream WiFi (板载/onboard radio) feature,
then turn it off. The WiFi page shows the hotspot SSID reset to the factory
'OpenWrt' instead of the customer's own name.

Cause: while the radio serves as the upstream STA its own AP section is
parked, and on the onboard radio the vif teardown/`wifi reload` cycle leaves
that section regenerated from board defaults. The radio backup only recorded
band/htmode/channel, so the exit path un-parked the AP with nothing to put the
customer's SSID/password back.

Fix: snapshot the AP identity in _backup_radio_state() and write it back in
_restore_radio_state(), for every exit path (toggle off, forget, radio swap).

Runs anywhere: no device, no radio, no network. uci is stubbed.

    python3 -m unittest tests.test_wifi_wan_ap_identity -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app")))

from pcat_util import wifi_wan


class FakeUci(object):
    """In-memory stand-in for the device's UCI tree.

    Values are flat 'wireless.<section>.<option>' keys, matching what
    wifi_wan's uci_get/uci_set/uci_delete operate on.
    """

    def __init__(self, values):
        self.values = dict(values)

    def get(self, key):
        return self.values.get(key, "")

    def set(self, assignment):
        key, _, val = assignment.partition("=")
        self.values[key] = val
        return True

    def delete(self, key):
        # Deleting a section drops its options too.
        for k in [k for k in self.values if k == key or k.startswith(key + ".")]:
            del self.values[k]
        return True


class FakeConfig(object):
    """Stand-in for pcat_config's user-config key/value store."""

    def __init__(self):
        self.keys = {}
        self.wifi_wan_radio = "onboard"

    def get_user_config_key(self, key, fallback=None):
        return self.keys.get(key, fallback)

    def set_user_config_key(self, key, value):
        self.keys[key] = value

    def get_wifi_wan_radio(self):
        return self.wifi_wan_radio


class ApIdentityRestoreTest(unittest.TestCase):
    # The customer's own hotspot, as configured before the relay is used.
    CUSTOMER_AP = {
        "wireless.default_radio0": "wifi-iface",
        "wireless.default_radio0.device": "radio0",
        "wireless.default_radio0.network": "lan",
        "wireless.default_radio0.mode": "ap",
        "wireless.default_radio0.ssid": "photonicat2-a1b2c3",
        "wireless.default_radio0.encryption": "psk2",
        "wireless.default_radio0.key": "customer-secret",
        "wireless.radio0.band": "2g",
        "wireless.radio0.htmode": "HE20",
        "wireless.radio0.channel": "6",
        # Onboard radio (no "pcie" in the path) -- what get_sta_radio() keys off.
        "wireless.radio0.path": "platform/soc/ffe0000.sdio/mmc_host",
    }

    def setUp(self):
        self.uci = FakeUci(self.CUSTOMER_AP)
        self.cfg = FakeConfig()

        self._orig = (wifi_wan.uci_get, wifi_wan.uci_set, wifi_wan.uci_delete,
                      wifi_wan.uci_show_dict)
        wifi_wan.uci_get = self.uci.get
        wifi_wan.uci_set = self.uci.set
        wifi_wan.uci_delete = self.uci.delete
        # get_sta_radio() -> _radio_by_class() reads the whole wireless tree.
        wifi_wan.uci_show_dict = lambda _pkg, **_kw: self.uci.values

        # wifi_wan does `import pcat_config` inside functions; intercept it.
        self._orig_mod = sys.modules.get("pcat_config")
        sys.modules["pcat_config"] = self.cfg

    def tearDown(self):
        (wifi_wan.uci_get, wifi_wan.uci_set, wifi_wan.uci_delete,
         wifi_wan.uci_show_dict) = self._orig
        if self._orig_mod is not None:
            sys.modules["pcat_config"] = self._orig_mod
        else:
            sys.modules.pop("pcat_config", None)

    def _relay_takes_over_and_regenerates_ap(self):
        """Enter the relay, then simulate what the onboard driver does to the
        parked AP section: regenerate it from factory board defaults."""
        wifi_wan._backup_radio_state("radio0")
        self.uci.set("wireless.default_radio0.disabled=1")
        # Board defaults: open network named 'OpenWrt'.
        self.uci.delete("wireless.default_radio0")
        self.uci.set("wireless.default_radio0=wifi-iface")
        self.uci.set("wireless.default_radio0.device=radio0")
        self.uci.set("wireless.default_radio0.mode=ap")
        self.uci.set("wireless.default_radio0.ssid=OpenWrt")

    def test_backup_records_ap_identity(self):
        wifi_wan._backup_radio_state("radio0")
        backup = self.cfg.keys[wifi_wan._RADIO_BACKUP_KEY]
        self.assertEqual(backup["ap"]["ssid"], "photonicat2-a1b2c3")
        self.assertEqual(backup["ap"]["key"], "customer-secret")
        self.assertEqual(backup["ap"]["encryption"], "psk2")

    def test_ssid_survives_relay_exit(self):
        """The issue #112 scenario, end to end."""
        self._relay_takes_over_and_regenerates_ap()
        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"), "OpenWrt")

        wifi_wan._restore_radio_state()

        self.assertEqual(
            self.uci.get("wireless.default_radio0.ssid"), "photonicat2-a1b2c3",
            "AP SSID was not restored to the customer's original after the "
            "upstream WiFi was turned off (issue #112)")
        self.assertEqual(self.uci.get("wireless.default_radio0.key"),
                         "customer-secret")
        self.assertEqual(self.uci.get("wireless.default_radio0.encryption"),
                         "psk2")
        # And the AP is on air again, on its original band/channel.
        self.assertEqual(self.uci.get("wireless.default_radio0.disabled"), "")
        self.assertEqual(self.uci.get("wireless.radio0.channel"), "6")

    def test_restore_recreates_a_deleted_ap_section(self):
        """netifd can drop the section outright, not merely reset it."""
        wifi_wan._backup_radio_state("radio0")
        self.uci.delete("wireless.default_radio0")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0"), "wifi-iface")
        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")
        self.assertEqual(self.uci.get("wireless.default_radio0.mode"), "ap")
        self.assertEqual(self.uci.get("wireless.default_radio0.network"), "lan")

    def test_user_rename_during_relay_is_not_clobbered(self):
        """A hotspot renamed while the relay ran keeps the new name: the
        backup is a repair for a lost identity, not a config rollback."""
        wifi_wan._backup_radio_state("radio0")
        self.uci.set("wireless.default_radio0.ssid=renamed-by-user")
        self.uci.set("wireless.default_radio0.key=new-password")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "renamed-by-user")
        self.assertEqual(self.uci.get("wireless.default_radio0.key"),
                         "new-password")

    def test_ap_regenerated_open_under_the_same_name_is_resecured(self):
        """The section can come back keeping the customer's SSID but with the
        security stripped. Restoring only on a placeholder name would leave
        their hotspot name broadcasting as an OPEN network -- worse than the
        wrong name, so a lost passphrase must trigger the repair too."""
        wifi_wan._backup_radio_state("radio0")
        self.uci.set("wireless.default_radio0.encryption=none")
        self.uci.delete("wireless.default_radio0.key")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.encryption"),
                         "psk2", "AP was left as an open network")
        self.assertEqual(self.uci.get("wireless.default_radio0.key"),
                         "customer-secret")
        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")

    def test_resecuring_keeps_a_user_rename(self):
        """Repairing lost security must not roll back a name the user chose
        while the relay was running."""
        wifi_wan._backup_radio_state("radio0")
        self.uci.set("wireless.default_radio0.ssid=renamed-by-user")
        self.uci.set("wireless.default_radio0.encryption=none")
        self.uci.delete("wireless.default_radio0.key")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "renamed-by-user")
        self.assertEqual(self.uci.get("wireless.default_radio0.key"),
                         "customer-secret")

    def test_open_ap_by_choice_is_left_open(self):
        """A customer whose hotspot was deliberately open must not have a
        passphrase invented for them on exit."""
        self.uci.set("wireless.default_radio0.encryption=none")
        self.uci.delete("wireless.default_radio0.key")
        wifi_wan._backup_radio_state("radio0")
        self.uci.set("wireless.default_radio0.ssid=OpenWrt")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")
        self.assertEqual(self.uci.get("wireless.default_radio0.encryption"),
                         "none")

    def test_backup_never_records_a_placeholder_over_a_real_identity(self):
        """If the AP is already factory-stock when the relay starts there is
        nothing worth snapshotting -- recording 'OpenWrt' would make the
        restore a no-op that cements the bug."""
        self.uci.set("wireless.default_radio0.ssid=OpenWrt")
        wifi_wan._backup_radio_state("radio0")
        self.assertNotIn("ap", self.cfg.keys[wifi_wan._RADIO_BACKUP_KEY])

    def test_ap_disabled_state_is_still_honoured(self):
        """A hotspot deliberately switched off before the relay stays off."""
        self.uci.set("wireless.default_radio0.disabled=1")
        wifi_wan._backup_radio_state("radio0")
        self.uci.set("wireless.default_radio0.ssid=OpenWrt")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.disabled"), "1")
        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")

    def test_backup_is_consumed_so_a_second_relay_resnapshots(self):
        wifi_wan._backup_radio_state("radio0")
        wifi_wan._restore_radio_state()
        self.assertFalse(self.cfg.keys.get(wifi_wan._RADIO_BACKUP_KEY))
        # Second cycle: a fresh snapshot is taken rather than reusing a stale one.
        self.uci.set("wireless.default_radio0.ssid=renamed-later")
        wifi_wan._backup_radio_state("radio0")
        self.assertEqual(
            self.cfg.keys[wifi_wan._RADIO_BACKUP_KEY]["ap"]["ssid"],
            "renamed-later")

    def test_falls_back_to_other_radio_when_no_ap_snapshot(self):
        """Upgrading from a build that predates the AP snapshot: the backup
        has no 'ap' key, so the identity is derived from the other radio."""
        self.uci.set("wireless.default_radio1.ssid=photonicat2-a1b2c3-5g")
        self.uci.set("wireless.default_radio1.encryption=psk2")
        self.uci.set("wireless.default_radio1.key=customer-secret")
        self.cfg.keys[wifi_wan._RADIO_BACKUP_KEY] = {
            "radio": "radio0", "band": "2g", "htmode": "HE20", "channel": "6",
            "ap_disabled": "", }  # no "ap" -- old-format backup
        self.uci.set("wireless.default_radio0.ssid=OpenWrt")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")

    def test_lost_backup_does_not_strand_a_parked_ap(self):
        """A backup lost mid-relay (config reset, upgrade) must not leave the
        hotspot parked forever: _restore_radio_state() would have nothing to
        restore from, and the settings toggle can't un-park it either because
        enable_wifi_interface() no-ops on an already-disabled radio."""
        self.uci.set("wireless.default_radio0.disabled=1")
        self.uci.set("wireless.default_radio1.path=platform/soc/pcie")
        self.cfg.keys.pop(wifi_wan._RADIO_BACKUP_KEY, None)

        self.assertTrue(wifi_wan._restore_radio_state())
        self.assertEqual(self.uci.get("wireless.default_radio0.disabled"), "",
                         "AP left parked with no way for the user to re-enable it")
        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")

    def test_lost_backup_keeps_a_placeholder_ap_off_the_air(self):
        """With no backup AND no identity to recover, leaving the AP off is
        better than broadcasting a factory-stock open 'OpenWrt'."""
        self.uci.set("wireless.default_radio0.disabled=1")
        self.uci.set("wireless.default_radio0.ssid=OpenWrt")
        self.uci.delete("wireless.default_radio0.key")
        self.cfg.keys.pop(wifi_wan._RADIO_BACKUP_KEY, None)

        self.assertFalse(wifi_wan._restore_radio_state())
        self.assertEqual(self.uci.get("wireless.default_radio0.disabled"), "1")

    def test_no_backup_and_unparked_ap_is_a_no_op(self):
        self.cfg.keys.pop(wifi_wan._RADIO_BACKUP_KEY, None)
        self.assertFalse(wifi_wan._restore_radio_state())

    def test_doubled_band_suffix_is_repaired_on_relay_exit(self):
        """Issue #115: switching the upstream off left the onboard AP named
        'photonicat2-xxxx-5g-5g' -- a branding run had inherited the PCIe
        radio's already-suffixed name. Nothing else ever rewrites that name
        (it is not a factory placeholder, so the backup restore and the
        seed-from-the-other-radio fallback both leave it alone), so leaving
        the relay has to put the firmware's own name back."""
        self.uci.set("wireless.default_radio0.disabled=1")
        self.uci.set("wireless.default_radio0.ssid=photonicat2-a1b2c3-5g-5g")
        self.cfg.keys.pop(wifi_wan._RADIO_BACKUP_KEY, None)

        self.assertTrue(wifi_wan._restore_radio_state())

        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")
        self.assertEqual(self.uci.get("wireless.default_radio0.disabled"), "")

    def test_relay_exit_with_a_backup_also_repairs_the_name(self):
        self.uci.set("wireless.default_radio0.ssid=photonicat2-a1b2c3-5g-5g")
        wifi_wan._backup_radio_state("radio0")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "photonicat2-a1b2c3")

    def test_a_user_chosen_single_suffix_survives_relay_exit(self):
        """One '-5g' is an ordinary name; only the doubled form is repaired."""
        self.uci.set("wireless.default_radio0.ssid=my-network-5g")
        wifi_wan._backup_radio_state("radio0")

        wifi_wan._restore_radio_state()

        self.assertEqual(self.uci.get("wireless.default_radio0.ssid"),
                         "my-network-5g")



if __name__ == "__main__":
    unittest.main(verbosity=2)
