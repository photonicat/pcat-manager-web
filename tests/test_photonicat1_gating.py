#!/usr/bin/env python3
"""Photonicat 1 (rk3568) vs Photonicat 2 (rk3576) gating in the web app.

Both boards run this app, but not the same hardware. On a Photonicat 1 there
is no MCU firmware update path, no LXC, no power-LED / beeper / charge-limit
control and no low-battery shutdown policy, and its sysupgrade images live in
a different directory on both the dev and the production server. Everything
that must differ keys off pcat_manager_web.is_photonicat2.

Two kinds of test here, because they fail differently:

  * the server side (firmware directories, MCU endpoints, the LXC opt-in) --
    a wrong branch offers a Photonicat 2 image or MCU blob to a Photonicat 1;
  * the templates -- the gates have to be Jinja, evaluated before the
    response is sent. An Alpine x-show paints the control first and removes
    it a frame later, so the unsupported box visibly flashes a switch it
    must never be offered.

    python3 -m unittest tests.test_photonicat1_gating -v
"""

import logging
import os
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# Device-only modules the web module imports at load time.
for _absent in ("pam", "serial", "serial.tools", "serial.tools.list_ports"):
    if _absent not in sys.modules:
        _mod = types.ModuleType(_absent)
        _mod.__getattr__ = lambda name: types.SimpleNamespace()
        sys.modules[_absent] = _mod

import pcat_util  # noqa: E402

if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_photonicat1_gating")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

import pcat_manager_web as web  # noqa: E402

TEMPLATES = os.path.join(REPO_ROOT, "app", "templates")


def read(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def strip_comments(src):
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def gated_blocks(src, flag):
    """Bodies of every {% if <flag> %}...{% endif %} in the template."""
    return re.findall(r"\{%%\s*if %s\s*%%\}(.*?)\{%%\s*endif\s*%%\}" % flag,
                      src, re.S)


class FirmwareDirectoryTests(unittest.TestCase):
    """Images are built per board; the app must look in its own directory."""

    def setUp(self):
        self._base = patch.object(web.pcat_config, "dev_server_base",
                                  "http://dev.example:81", create=True)
        self._base.start()

    def tearDown(self):
        self._base.stop()

    def test_photonicat2_uses_the_rk3576_directories(self):
        with patch.object(web, "is_photonicat2", True):
            self.assertEqual(web.dev_firmware_base_url(),
                             "http://dev.example:81/rk3576-photonicat2-openwrt/")
            self.assertEqual(web.prod_firmware_base_url(),
                             "https://dl.photonicat.com/images/photonicat2/openwrt/")

    def test_photonicat1_uses_the_rk3568_directories(self):
        with patch.object(web, "is_photonicat2", False):
            self.assertEqual(web.dev_firmware_base_url(),
                             "http://dev.example:81/rk3568-photonicat-openwrt/")
            self.assertEqual(web.prod_firmware_base_url(),
                             "https://dl.photonicat.com/images/photonicat/openwrt/")

    def test_no_firmware_url_is_still_hardcoded_to_one_board(self):
        """Regression guard: every firmware URL must go through the helpers,
        or the next feature quietly offers the wrong board's image."""
        with open(os.path.join(REPO_ROOT, "app", "pcat_manager_web.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count("rk3576-photonicat2-openwrt"), 1,
                         "the dev directory name belongs in DEV_FIRMWARE_DIR only")
        self.assertEqual(src.count("images/photonicat2/openwrt/"), 1,
                         "the prod directory belongs in PROD_FIRMWARE_BASE_URL only")

    def test_listing_parser_accepts_photonicat1_image_names(self):
        """The rk3568 images are named ...ariaboard_photonicat-squashfs-
        sysupgrade.img.gz; the listing filter must keep them."""
        name = ("photonicatwrt-26.10.0-r7812-5b4069f87d-rockchip-armv8-"
                "ariaboard_photonicat-squashfs-sysupgrade.img.gz")
        parser = web.FirmwareListingParser()
        parser.feed('<a href="./%s">%s</a>' % (name, name))
        self.assertEqual(parser.firmware_files, [name])
        self.assertEqual(web.extract_fw_revision(name), 7812)
        self.assertEqual(web.extract_fw_hash(name), "5b4069f87d")


class McuEndpointTests(unittest.TestCase):
    """A Photonicat 1 must never be offered a Photonicat 2 MCU image."""

    def setUp(self):
        web.flask_app.config["TESTING"] = True
        self.client = web.flask_app.test_client()

    def test_checks_answer_unsupported_on_photonicat1(self):
        with patch.object(web, "is_photonicat2", False):
            for path in ("/api/v1/mcu_dev_check.json",
                         "/api/v1/mcu_prod_check.json"):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200, path)
                body = resp.get_json()
                self.assertEqual(body["status"], "unsupported", path)
                self.assertIs(body["reachable"], False, path)

    def test_update_start_is_refused_on_photonicat1(self):
        with self.client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["_csrf_token"] = "t"
        with patch.object(web, "is_photonicat2", False):
            resp = self.client.post(
                "/api/v1/mcu_update.json",
                json={"firmware_url": "http://dev/x.bin"},
                headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["status"], "unsupported")


class Photonicat2OnlyPageTests(unittest.TestCase):
    """/dev (device diagnostics) and /phone (telephony) are Photonicat 2
    pages. A Photonicat 1 has no pack-current/temperature gauge, no
    accelerometer and no MCU revision to report, and no call-audio path at
    all, so both must answer 404 -- including the old /battery and /buzzer
    bookmarks, which would otherwise redirect a browser to a 404."""

    PAGES = ("/dev", "/phone", "/battery", "/buzzer")

    def setUp(self):
        web.flask_app.config["TESTING"] = True
        self.client = web.flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess["username"] = "admin"

    def test_pages_are_404_on_photonicat1(self):
        with patch.object(web, "is_photonicat2", False):
            for path in self.PAGES:
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 404, path)

    def test_old_bookmarks_still_redirect_on_photonicat2(self):
        with patch.object(web, "is_photonicat2", True):
            for path in ("/battery", "/buzzer"):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 302, path)
                self.assertTrue(resp.headers["Location"].endswith("/dev"), path)

    def test_telephony_is_never_offered_on_photonicat1(self):
        """Nav entry and the settings toggle both hang off phone_capable():
        a Photonicat 1 answering a REV2-looking hardware string must still
        not be offered a feature its board cannot carry."""
        with patch.object(web, "is_photonicat2", False), \
             patch.object(web, "_voice_capable_modem", lambda: True), \
             patch.object(web, "_rev2_board", lambda: True):
            self.assertFalse(web.phone_capable())
        with patch.object(web, "is_photonicat2", True), \
             patch.object(web, "_voice_capable_modem", lambda: True), \
             patch.object(web, "_rev2_board", lambda: True):
            self.assertTrue(web.phone_capable())


class LxcOptInTests(unittest.TestCase):
    """LXC is opt-in on Photonicat 2 and absent on Photonicat 1."""

    def test_nav_flag_ignores_a_stored_opt_in_on_photonicat1(self):
        """A config carried over from a Photonicat 2 (or set by hand) must
        not surface the LXC page on hardware without LXC."""
        with patch.object(web, "is_photonicat2", False), \
             patch.object(web.pcat_config, "get_user_config_key",
                          side_effect=lambda k, fb=None: True), \
             patch.object(web, "phone_menu_enabled", lambda: False), \
             patch.object(web, "explicit_theme", lambda: None), \
             patch.object(web, "theme_accent", lambda: ""):
            self.assertFalse(web.inject_cache_bust()["lxc_enabled"])
            self.assertFalse(web.inject_cache_bust()["is_photonicat2"])

    def test_nav_flag_honours_the_opt_in_on_photonicat2(self):
        with patch.object(web, "is_photonicat2", True), \
             patch.object(web.pcat_config, "get_user_config_key",
                          side_effect=lambda k, fb=None: True), \
             patch.object(web, "phone_menu_enabled", lambda: False), \
             patch.object(web, "explicit_theme", lambda: None), \
             patch.object(web, "theme_accent", lambda: ""):
            self.assertTrue(web.inject_cache_bust()["lxc_enabled"])


class DeviceControlTemplateTests(unittest.TestCase):
    """Power LED, beeper and charge limit exist only on Photonicat 2."""

    def setUp(self):
        self.code = strip_comments(read("device_control.html"))
        self.gated = "\n".join(gated_blocks(self.code, "is_photonicat2"))

    def test_the_three_controls_sit_inside_a_jinja_gate(self):
        for key in ("power_led_control", "beeper_control", "charge_limit"):
            self.assertIn('locales["%s"]' % key, self.gated,
                          "%s must be inside {%% if is_photonicat2 %%}" % key)
        # ...and nowhere outside it.
        outside = re.sub(r"\{%\s*if is_photonicat2\s*%\}.*?\{%\s*endif\s*%\}",
                         "", self.code, flags=re.S)
        for key in ("power_led_control", "beeper_control", "charge_limit"):
            self.assertNotIn('locales["%s"]' % key, outside)

    def test_the_page_does_not_poll_absent_controls(self):
        for call in ("fetch_power_led_settings()", "fetch_beeper_settings()",
                     "fetch_charge_limit()"):
            self.assertIn("this.%s" % call, self.gated,
                          "%s must only run on Photonicat 2" % call)


class TimerTemplateTests(unittest.TestCase):
    def setUp(self):
        self.code = strip_comments(read("timer.html"))
        self.gated = "\n".join(gated_blocks(self.code, "is_photonicat2"))

    def test_low_battery_shutdown_is_gated(self):
        self.assertIn('x-model="low_battery_shutdown_enabled"', self.gated)
        self.assertIn('fetch("/api/v1/low_battery_shutdown.json")', self.gated)


class GeneralSettingsTemplateTests(unittest.TestCase):
    def setUp(self):
        self.code = strip_comments(read("general_settings.html"))

    def test_lxc_switch_only_exists_where_lxc_does(self):
        gated = gated_blocks(self.code, "lxc_capable")
        self.assertTrue(gated, "the LXC toggle must be gated on lxc_capable")
        self.assertIn('x-model="lxc_enabled"', "\n".join(gated))
        self.assertNotRegex(self.code, r'x-(show|if)="[^"]*lxc_capable')


class UpdateTemplateTests(unittest.TestCase):
    def setUp(self):
        self.code = strip_comments(read("update.html"))
        self.gated = "\n".join(gated_blocks(self.code, "is_photonicat2"))

    def test_mcu_card_and_checks_are_gated(self):
        self.assertIn("{% include '_mcu_update.html' %}", self.gated)
        self.assertIn("this.check_mcu_updates()", self.gated)
        self.assertIn("this.check_ongoing_mcu_update()", self.gated)


class BatteryHistoryTests(unittest.TestCase):
    """A Photonicat 1 measures neither battery current nor temperature. Its
    history must carry NULL for both, and the chart must not offer them."""

    def setUp(self):
        web.flask_app.config["TESTING"] = True
        self.client = web.flask_app.test_client()

    def test_api_nulls_current_and_temperature_on_photonicat1(self):
        """Rows recorded before the recorder learned this carry a literal 0;
        the API must not hand those to the chart as measurements."""
        rows = [{"ts": 1, "soc": 100, "voltage": 4.2, "current": 0.0,
                 "charging": 1, "temperature": 0.0}]
        with patch.object(web, "is_photonicat2", False), \
             patch.object(web.battery_history, "get_history", lambda hours: rows), \
             patch.object(web.power_event_log, "get_events", lambda **kw: []):
            body = self.client.get("/api/v1/battery_history.json?hours=24").get_json()
        self.assertIsNone(body["data"][0]["current"])
        self.assertIsNone(body["data"][0]["temperature"])
        self.assertEqual(body["data"][0]["voltage"], 4.2)

    def test_api_keeps_measurements_on_photonicat2(self):
        rows = [{"ts": 1, "soc": 100, "voltage": 4.2, "current": -350.0,
                 "charging": 0, "temperature": 31.0}]
        with patch.object(web, "is_photonicat2", True), \
             patch.object(web.battery_history, "get_history", lambda hours: rows), \
             patch.object(web.power_event_log, "get_events", lambda **kw: []):
            body = self.client.get("/api/v1/battery_history.json?hours=24").get_json()
        self.assertEqual(body["data"][0]["current"], -350.0)
        self.assertEqual(body["data"][0]["temperature"], 31.0)

    def test_recorder_stores_null_not_zero_on_photonicat1(self):
        pc_app = web.pc_app
        client = types.SimpleNamespace(battery_current=None, board_temperature=0)
        with patch.object(pc_app, "is_photonicat2", lambda: False):
            self.assertEqual(pc_app.battery_history_extras(client), (None, None))

    def test_recorder_keeps_gauge_readings_on_photonicat2(self):
        pc_app = web.pc_app
        client = types.SimpleNamespace(battery_current=-351.4, board_temperature=31)
        with patch.object(pc_app, "is_photonicat2", lambda: True):
            self.assertEqual(pc_app.battery_history_extras(client), (-351.0, 31))
        # A failed gauge read on Photonicat 2 stays 0 mA, as it always was.
        client.battery_current = None
        with patch.object(pc_app, "is_photonicat2", lambda: True):
            self.assertEqual(pc_app.battery_history_extras(client), (0, 31))


class StatisticsTemplateTests(unittest.TestCase):
    def setUp(self):
        self.code = strip_comments(read("statistics.html"))
        self.gated = "\n".join(gated_blocks(self.code, "is_photonicat2"))

    def test_power_and_temperature_toggles_are_gated(self):
        self.assertIn('x-model="bat_show_current"', self.gated)
        self.assertIn('x-model="bat_show_temp"', self.gated)
        # ...and the SOC / voltage toggles are not.
        outside = re.sub(r"\{%\s*if is_photonicat2\s*%\}.*?\{%\s*endif\s*%\}",
                         "", self.code, flags=re.S)
        self.assertIn('x-model="bat_show_soc"', outside)
        self.assertIn('x-model="bat_show_voltage"', outside)

    def test_tooltip_quotes_no_wattage_without_a_current(self):
        self.assertIn("closest.current == null ? null", self.code)


class DevTemplateTests(unittest.TestCase):
    def test_pack_current_and_power_tiles_are_gated(self):
        code = strip_comments(read("dev.html"))
        gated = "\n".join(gated_blocks(code, "is_photonicat2"))
        for key in ("dev_pack_current", "dev_pack_power"):
            self.assertIn('locales["%s"]' % key, gated)
        self.assertIn('x-text="flow_label()"', gated)


class DirectPowerModeTests(unittest.TestCase):
    """Direct power is a Photonicat 2 MCU power-on mode (set_power_on_mode 2).
    The Photonicat 1 PMU has no such mode: no tab, no page, no API."""

    def setUp(self):
        web.flask_app.config["TESTING"] = True
        self.client = web.flask_app.test_client()
        self.code = strip_comments(read("timer.html"))
        self.gated = "\n".join(gated_blocks(self.code, "is_photonicat2"))

    def test_tab_and_page_sit_inside_a_jinja_gate(self):
        self.assertIn("mode_page = 'force_power_mode_page'", self.gated)
        self.assertIn('class="force_power_mode_page"', self.gated)
        outside = re.sub(r"\{%\s*if is_photonicat2\s*%\}.*?\{%\s*endif\s*%\}",
                         "", self.code, flags=re.S)
        self.assertNotIn('locales["force_power_mode_tip_1"]', outside)
        # The other three modes stay.
        for page in ("car_mode_page", "timer_mode_page", "unknown_mode_page"):
            self.assertIn("mode_page = '%s'" % page, outside)

    def test_api_refuses_on_photonicat1(self):
        with self.client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["_csrf_token"] = "t"
        socket_client = MagicMock()
        with patch.object(web, "is_photonicat2", False), \
             patch.object(web.pc_app, "socket_client", socket_client):
            resp = self.client.post("/api/v1/forcepowermode.json",
                                    json={"forcemode": "true"},
                                    headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["status"], "error")
        socket_client.set_power_on_mode.assert_not_called()

    def test_a_stored_direct_power_mode_does_not_open_a_hidden_page(self):
        """A config carried over from a Photonicat 2 must land on the manual
        page, not on a tab the board does not render."""
        with self.client.session_transaction() as sess:
            sess["username"] = "admin"
        with patch.object(web, "is_photonicat2", False), \
             patch.object(web.pc_app, "socket_client", MagicMock()), \
             patch.object(web.pcat_config, "get_power_mode", lambda: "direct_power"), \
             patch.object(web, "render_template",
                          lambda name, **kw: kw.get("mode_page", "")):
            resp = self.client.get("/timer")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), "unknown_mode_page")


class CpuPowersaveTextTests(unittest.TestCase):
    """RK3576 parks its A72 cores; RK3568 has four equal A55 cores and falls
    back to the powersave governor. The help text must say which."""

    def test_device_control_picks_the_text_for_the_board(self):
        code = strip_comments(read("device_control.html"))
        self.assertRegex(
            code,
            r'\{% if is_photonicat2 %\}\{\{locales\["cpu_powersave_help_text"\]\}\}'
            r'\{% else %\}\{\{locales\["cpu_powersave_help_text_pcat1"\]\}\}\{% endif %\}')

    def test_both_texts_exist_in_both_languages(self):
        import locales as loc_pkg
        for lang in ("EN", "CN"):
            page = loc_pkg.get_locales("device_control", lang)
            for key in ("cpu_powersave_help_text", "cpu_powersave_help_text_pcat1"):
                self.assertTrue(page.get(key), "%s missing from %s" % (key, lang))
        cn = loc_pkg.get_locales("device_control", "CN")
        self.assertIn("A55", cn["cpu_powersave_help_text_pcat1"])
        self.assertNotIn("A72", cn["cpu_powersave_help_text_pcat1"])


class NicModeSelectorTests(unittest.TestCase):
    """"auto" parks the PHY via the eth-powersave package, which photonicat 1
    does not ship. Offering the button there would change nothing, so the LAN
    and WAN selectors are on/off only."""

    def setUp(self):
        self.code = strip_comments(read("device_control.html"))

    def test_the_mode_list_is_chosen_by_board(self):
        self.assertRegex(
            self.code,
            r'nic_modes:\s*\{% if is_photonicat2 %\}\["auto", "on", "off"\]'
            r'\{% else %\}\["on", "off"\]\{% endif %\}')

    def test_neither_selector_hardcodes_three_states(self):
        self.assertEqual(2, self.code.count('x-for="m in nic_modes"'),
                         "both the LAN and WAN selectors must use nic_modes")
        self.assertNotIn("'auto','on','off'", self.code)

    def test_lan_help_text_picks_the_text_for_the_board(self):
        self.assertRegex(
            self.code,
            r'\{% if is_photonicat2 %\}\{\{locales\["lan_interface_toggle_help_text"\]\}\}'
            r'\{% else %\}\{\{locales\["lan_interface_toggle_help_text_pcat1"\]\}\}'
            r'\{% endif %\}')

    def test_the_pcat1_help_text_never_mentions_auto(self):
        import locales as loc_pkg
        for lang, auto_word in (("EN", "Auto"), ("CN", "自动")):
            page = loc_pkg.get_locales("device_control", lang)
            text = page.get("lan_interface_toggle_help_text_pcat1")
            self.assertTrue(text, "pcat1 LAN help text missing from %s" % lang)
            self.assertNotIn(auto_word, text)
            self.assertIn(auto_word, page["lan_interface_toggle_help_text"])


class BrandTemplateTests(unittest.TestCase):
    def test_the_mobile_brand_suffix_2_is_photonicat2_only(self):
        code = strip_comments(read("_base.html"))
        gated = "\n".join(gated_blocks(code, "is_photonicat2"))
        self.assertIn('<span class="ml-1 sm:hidden">2</span>', gated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
