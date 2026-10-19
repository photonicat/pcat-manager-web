#!/usr/bin/env python3
"""Which optional pages appear in the nav, and which switches appear in
settings.

LXC, GPS and the phone page are all opt-in and ship off. The phone page has a
hardware floor on top of that: in-call audio needs an RM500U or RM520 module
AND a REV2 board to route PCM through. Advertising it anywhere else means a
user turns it on, dials, and hears nothing.

Two halves are tested here because they fail differently:

  * the predicates (phone_capable / phone_menu_enabled) -- a wrong truth table
    offers telephony on hardware that cannot do it, or hides it on hardware
    that can;
  * the templates -- the gates have to be Jinja, evaluated before the response
    is sent. An Alpine x-show paints the row first and removes it a frame
    later, so an unsupported box visibly flashes a switch it must never be
    offered, and every box flashes its toggles in the wrong position until
    Alpine hydrates.

    python3 -m unittest tests.test_optional_page_gating -v
"""

import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

TEMPLATES = os.path.join(REPO_ROOT, "app", "templates")


def read(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def strip_comments(src):
    """Template minus its comments -- they legitimately name the constructs
    these tests forbid."""
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


class _WebModuleTest(unittest.TestCase):
    """pcat_manager_web with its device-only imports stubbed out."""

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_config': MagicMock(),
            'pcat_util.user_auth': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.command_parser': MagicMock(),
            'pcat_util.hardware_util': MagicMock(),
            'pcat_util.isp_dict': MagicMock(),
            'pc_socket_client': MagicMock(),
            'pc_modem_client': MagicMock(),
            'pc_sms_client': MagicMock(),
            'pc_screen_client': MagicMock(),
            'app': MagicMock(),
            'flask': MagicMock(),
            'flask_socketio': MagicMock(),
        })
        self.module_patch.start()
        import pcat_manager_web
        self.module = pcat_manager_web

    def tearDown(self):
        self.module_patch.stop()


class PhoneCapabilityTests(_WebModuleTest):
    """Telephony needs a Photonicat 2, a module that carries call audio AND a
    board revision that routes it. Any one missing means no phone page."""

    MODELS = {
        "rm500u": dict(is_500u=True, is_520=False),
        "rm520": dict(is_500u=False, is_520=True),
        "em05": dict(is_500u=False, is_520=False),
        "unknown": dict(is_500u=False, is_520=False),  # early boot, no ATI yet
    }

    def capable(self, model, hw_version, photonicat2=True):
        with patch.object(self.module, 'pc_app') as app, \
             patch.object(self.module, 'is_photonicat2', photonicat2):
            flags = self.MODELS[model]
            app.modem_client.is_500u.return_value = flags["is_500u"]
            app.modem_client.is_520.return_value = flags["is_520"]
            app.socket_client.pmu_hw_version = hw_version
            return self.module.phone_capable()

    def test_photonicat1_never_qualifies(self):
        """rk3568 boards have no PCM path to a codec at all, and REV1/REV2 is
        a Photonicat 2 notion -- a REV2-looking answer there proves nothing."""
        self.assertFalse(self.capable("rm500u", "PCAT2-REV2", photonicat2=False))
        self.assertFalse(self.capable("rm520", "PCAT2-REV2", photonicat2=False))

    def test_rm500u_and_rm520_on_rev2_are_the_supported_combinations(self):
        self.assertTrue(self.capable("rm500u", "PCAT2-REV2"))
        self.assertTrue(self.capable("rm520", "PCAT2-REV2"))

    def test_other_modules_never_qualify_however_new_the_board(self):
        self.assertFalse(self.capable("em05", "PCAT2-REV2"))

    def test_rev1_board_cannot_carry_call_audio(self):
        self.assertFalse(self.capable("rm500u", "PCAT2-REV1"))
        self.assertFalse(self.capable("rm520", "PCAT2-REV1"))

    def test_a_board_that_cannot_identify_itself_reads_as_unsupported(self):
        """Old jumper-blind MCU firmware answers NT2421A4 for every board, and
        the value is empty until the first answer lands. Neither is a REV2
        claim; offering telephony on a guess is the worse failure."""
        for unknown in ("NT2421A4", "", None):
            self.assertFalse(self.capable("rm500u", unknown),
                             "hw version %r must not pass as REV2" % (unknown,))

    def test_model_not_yet_known_is_not_capable(self):
        self.assertFalse(self.capable("unknown", "PCAT2-REV2"))

    def test_a_broken_client_degrades_to_unsupported(self):
        with patch.object(self.module, 'pc_app') as app:
            app.modem_client.is_500u.side_effect = RuntimeError("modem gone")
            app.socket_client.pmu_hw_version = "PCAT2-REV2"
            self.assertFalse(self.module.phone_capable())


class PhoneMenuTests(_WebModuleTest):
    """The nav entry needs the user's opt-in as well as the hardware."""

    def menu(self, capable, user_pref):
        with patch.object(self.module, 'phone_capable', lambda: capable), \
             patch.object(self.module, 'pcat_config') as cfg:
            cfg.get_user_config_key.side_effect = \
                lambda key, fallback=None: user_pref
            return self.module.phone_menu_enabled()

    def test_capable_hardware_alone_does_not_show_the_nav_entry(self):
        self.assertFalse(self.menu(capable=True, user_pref=False))

    def test_opt_in_on_capable_hardware_shows_it(self):
        self.assertTrue(self.menu(capable=True, user_pref=True))

    def test_opt_in_is_ignored_on_hardware_that_cannot_deliver(self):
        """A box that had the toggle on and then lost the module (or a config
        carried onto REV1 hardware) must not keep advertising the page."""
        self.assertFalse(self.menu(capable=False, user_pref=True))

    def test_default_is_off(self):
        """Nothing stored yet -> the fallback the caller passes is what counts."""
        with patch.object(self.module, 'phone_capable', lambda: True), \
             patch.object(self.module, 'pcat_config') as cfg:
            cfg.get_user_config_key.side_effect = \
                lambda key, fallback=None: fallback
            self.assertFalse(self.module.phone_menu_enabled())


class OptionalPageDefaultsTests(_WebModuleTest):
    """What a device ships with, and what "reset to default" restores."""

    def test_lxc_gps_and_phone_all_ship_off(self):
        for key in ("lxc_enabled", "gps_enabled", "phone_enabled"):
            self.assertIs(self.module.GENERAL_SETTINGS_DEFAULTS[key], False,
                          "%s must default to off" % key)

    def test_phone_toggle_is_saved_like_the_other_menu_switches(self):
        """Regression guard: the save handler iterates a literal tuple, so a
        new key silently does nothing until it is added there."""
        with open(os.path.join(REPO_ROOT, "app", "pcat_manager_web.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            '"nas_enabled", "lxc_enabled", "gps_enabled", "phone_enabled"', src,
            "phone_enabled must be in the general_settings save loop")


class NavGateTemplateTests(unittest.TestCase):
    """The nav is rendered, not scripted."""

    def setUp(self):
        self.code = strip_comments(read("_base.html"))

    def test_optional_entries_are_gated_server_side(self):
        for flag in ("phone_enabled", "lxc_enabled", "gps_enabled",
                     "nas_enabled", "terminal_enabled"):
            self.assertIn("{%% if %s %%}" % flag, self.code,
                          "%s nav entry must be a Jinja gate" % flag)

    def test_no_entry_is_hidden_by_alpine(self):
        """x-show/x-if renders the element first and hides it on hydration --
        the nav would flash entries the device is not offering."""
        for flag in ("phone_enabled", "lxc_enabled", "gps_enabled"):
            self.assertNotRegex(
                self.code, r'x-(show|if)="[^"]*%s' % flag,
                "%s must not be gated in Alpine" % flag)


class SettingsGateTemplateTests(unittest.TestCase):
    """The settings page: which switches exist, and what state they paint on
    the first frame."""

    TOGGLES = ("terminal", "nas", "lxc", "gps", "phone")

    def setUp(self):
        self.src = read("general_settings.html")
        self.code = strip_comments(self.src)

    def test_phone_switch_only_exists_on_capable_hardware(self):
        self.assertIn("{% if phone_capable %}", self.code,
                      "the phone toggle must be gated on the hardware check")
        # The gate has to wrap the input, not just the label.
        gated = re.search(r"\{%\s*if phone_capable\s*%\}(.*?)\{%\s*endif\s*%\}",
                          self.code, re.S)
        self.assertIsNotNone(gated)
        self.assertIn('x-model="phone_enabled"', gated.group(1))

    def test_phone_switch_is_not_hidden_by_alpine(self):
        self.assertNotRegex(self.code, r'x-(show|if)="[^"]*phone_capable')

    def test_every_toggle_paints_its_saved_state_before_alpine_runs(self):
        """Without the server-rendered `checked`, an enabled switch paints in
        the off position until Alpine hydrates -- the same flash, one row
        down from the one we just fixed."""
        for key in self.TOGGLES:
            self.assertRegex(
                self.code,
                r'x-model="%s_enabled"\s*\{%%\s*if current_%s_enabled\s*%%\}checked'
                % (key, key),
                "%s toggle must render its initial checked state" % key)

    def test_every_toggle_label_has_server_rendered_text(self):
        for key in self.TOGGLES:
            self.assertRegex(
                self.code,
                r"x-text=\"%s_enabled \?[^\"]*\">\{\{ locales\.common_enable if "
                r"current_%s_enabled else locales\.common_disable \}\}</span>"
                % (key, key),
                "%s toggle label must not start blank" % key)

    def test_phone_preference_reaches_the_save_payload(self):
        self.assertIn("phone_enabled: this.phone_enabled", self.code,
                      "the toggle would silently never save")
        self.assertIn("phone_enabled: {{current_phone_enabled|tojson}}",
                      self.code, "Alpine must start from the stored value")


class PhoneLocaleTests(unittest.TestCase):
    """User-facing strings exist in both languages (project rule)."""

    def test_phone_toggle_strings_are_translated(self):
        import locales as loc_pkg
        for lang in ("EN", "CN"):
            page = loc_pkg.get_locales("general_settings", lang)
            for key in ("phone_enabled_label", "phone_enabled_help"):
                self.assertTrue(page.get(key),
                                "%s missing from %s" % (key, lang))


if __name__ == "__main__":
    unittest.main(verbosity=2)
