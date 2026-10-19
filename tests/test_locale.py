#!/usr/bin/env python3
"""Tests for UI language selection (app/pcat_manager_web.py).

The rule these pin: the browser's own language is only a *default*. Once an
operator picks a language it is a property of the device, so every browser gets
it. Before this, the choice lived in the Flask session, which is per-browser --
two browsers open on the same page rendered in two different languages, and a
language set on the settings page reverted for anyone visiting from another
machine.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_locale`.
"""

import logging
import os
import sys
import types
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

for _absent in ("pam", "serial", "serial.tools", "serial.tools.list_ports"):
    if _absent not in sys.modules:
        _mod = types.ModuleType(_absent)
        _mod.__getattr__ = lambda name: types.SimpleNamespace()
        sys.modules[_absent] = _mod

import pcat_util  # noqa: E402
if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_locale")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

import pcat_manager_web as web  # noqa: E402

CN_BROWSER = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
EN_BROWSER = {"Accept-Language": "en-GB,en;q=0.9"}


class LocaleTestCase(unittest.TestCase):
    """Backs the device-wide store with a dict so nothing touches disk."""

    def setUp(self):
        web.flask_app.config["TESTING"] = True
        self.store = {}
        self._patches = [
            patch.object(web.pcat_config, "get_user_config_key",
                         side_effect=lambda k, fb=None: self.store.get(k, fb)),
            patch.object(web.pcat_config, "set_user_config_key",
                         side_effect=self.store.__setitem__),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def locale_for(self, headers=None):
        """The locale current_locale() resolves to for one request."""
        with web.flask_app.test_request_context("/", headers=headers or {}):
            return web.current_locale()


class BrowserDefaultTests(LocaleTestCase):
    """With nothing chosen, follow each browser."""

    def test_chinese_browser_gets_chinese(self):
        self.assertEqual(self.locale_for(CN_BROWSER), "CN")

    def test_english_browser_gets_english(self):
        self.assertEqual(self.locale_for(EN_BROWSER), "EN")

    def test_no_accept_language_falls_back_to_english(self):
        self.assertEqual(self.locale_for({}), "EN")

    def test_two_browsers_may_differ_while_nothing_is_chosen(self):
        self.assertEqual(self.locale_for(CN_BROWSER), "CN")
        self.assertEqual(self.locale_for(EN_BROWSER), "EN")

    def test_detection_is_not_cached_into_the_session(self):
        """Caching the detected default is what pinned a browser to whatever
        it asked for first, so a later device-wide choice could not move it."""
        with web.flask_app.test_request_context("/", headers=CN_BROWSER):
            web.current_locale()
            from flask import session
            self.assertNotIn("locale", session)


class ExplicitChoiceWinsTests(LocaleTestCase):
    """The reported bug: an explicit choice must reach every browser."""

    def test_explicit_choice_overrides_both_browsers(self):
        web.set_explicit_locale("CN")
        self.assertEqual(self.locale_for(EN_BROWSER), "CN")
        self.assertEqual(self.locale_for(CN_BROWSER), "CN")

    def test_explicit_english_overrides_a_chinese_browser(self):
        web.set_explicit_locale("EN")
        self.assertEqual(self.locale_for(CN_BROWSER), "EN")

    def test_explicit_choice_beats_a_stale_per_browser_cookie(self):
        """A browser that toggled the language before an operator set one
        must not keep overriding the device."""
        web.set_explicit_locale("EN")
        headers = dict(CN_BROWSER)
        headers["Cookie"] = "pcat_lang=CN"
        self.assertEqual(self.locale_for(headers), "EN")

    def test_the_cookie_still_applies_while_nothing_is_chosen(self):
        headers = dict(EN_BROWSER)
        headers["Cookie"] = "pcat_lang=CN"
        self.assertEqual(self.locale_for(headers), "CN")

    def test_unset_is_not_the_same_as_english(self):
        """'Nobody has chosen' has to stay representable, or the browser
        default can never apply."""
        self.assertIsNone(web.explicit_locale())
        web.set_explicit_locale("EN")
        self.assertEqual(web.explicit_locale(), "EN")

    def test_rubbish_values_are_refused(self):
        self.assertFalse(web.set_explicit_locale("klingon"))
        self.assertIsNone(web.explicit_locale())

    def test_a_stored_rubbish_value_is_ignored(self):
        self.store[web.UI_LANGUAGE_KEY] = "nonsense"
        self.assertIsNone(web.explicit_locale())
        self.assertEqual(self.locale_for(CN_BROWSER), "CN")


class SwitchLocaleAuthTests(LocaleTestCase):
    """/switch_locale stays public so the login page is translatable, so the
    device-wide write behind it must be gated on a session."""

    def test_anonymous_toggle_does_not_change_the_device(self):
        client = web.flask_app.test_client()
        client.get("/switch_locale", headers=EN_BROWSER)
        self.assertIsNone(web.explicit_locale())

    def test_authenticated_toggle_sets_the_device_language(self):
        client = web.flask_app.test_client()
        with client.session_transaction() as sess:
            sess["username"] = "admin"
        with patch.object(web, "_openwrt_uci_set"), \
                patch.object(web, "_openwrt_uci_commit"):
            client.get("/switch_locale", headers=EN_BROWSER)
        # EN browser, nothing chosen -> was EN, so the toggle lands on CN.
        self.assertEqual(web.explicit_locale(), "CN")

    def test_anonymous_toggle_still_affects_that_browser_only(self):
        client = web.flask_app.test_client()
        client.get("/switch_locale", headers=EN_BROWSER)
        with client.session_transaction() as sess:
            self.assertEqual(sess.get("locale"), "CN")
        # ...and a different browser is untouched.
        self.assertEqual(self.locale_for(EN_BROWSER), "EN")


class ThemeTests(LocaleTestCase):
    """Appearance follows the same rule as the language, and for the same
    reason: it was per-browser (localStorage), so the settings page could not
    actually set how the device looks."""

    def test_unset_means_auto_not_light(self):
        self.assertIsNone(web.explicit_theme())

    def test_explicit_theme_is_stored_device_wide(self):
        self.assertTrue(web.set_explicit_theme("dark"))
        self.assertEqual(web.explicit_theme(), "dark")

    def test_auto_clears_the_device_choice(self):
        web.set_explicit_theme("dark")
        self.assertTrue(web.set_explicit_theme("auto"))
        self.assertIsNone(web.explicit_theme())

    def test_rubbish_is_refused(self):
        self.assertFalse(web.set_explicit_theme("neon"))
        self.assertIsNone(web.explicit_theme())

    def test_templates_are_given_the_device_theme(self):
        web.set_explicit_theme("dark")
        with web.flask_app.test_request_context("/"):
            self.assertEqual(web.inject_cache_bust()["device_theme"], "dark")
        web.set_explicit_theme("auto")
        with web.flask_app.test_request_context("/"):
            self.assertEqual(web.inject_cache_bust()["device_theme"], "auto")

    def test_endpoint_requires_a_session(self):
        """The login page's theme cycle must not be able to restyle the
        appliance for everyone."""
        client = web.flask_app.test_client()
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        resp = client.post("/api/v1/theme.json", json={"theme": "dark"},
                           headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 401)
        self.assertIsNone(web.explicit_theme())

    def test_authenticated_endpoint_persists(self):
        client = web.flask_app.test_client()
        with client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["_csrf_token"] = "t"
        resp = client.post("/api/v1/theme.json", json={"theme": "dark"},
                           headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(web.explicit_theme(), "dark")

    def test_endpoint_rejects_rubbish(self):
        client = web.flask_app.test_client()
        with client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["_csrf_token"] = "t"
        resp = client.post("/api/v1/theme.json", json={"theme": "neon"},
                           headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 400)


class ResetToDefaultTests(LocaleTestCase):

    def _client(self):
        client = web.flask_app.test_client()
        with client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["_csrf_token"] = "t"
        return client

    def test_requires_a_session(self):
        client = web.flask_app.test_client()
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        resp = client.post("/api/v1/general_settings/reset.json",
                           headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 401)

    def test_clears_appearance_and_language_back_to_per_browser(self):
        web.set_explicit_theme("dark")
        web.set_explicit_locale("CN")
        with patch.object(web, "_set_tcp_congestion"):
            resp = self._client().post("/api/v1/general_settings/reset.json",
                                       headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(web.explicit_theme())
        self.assertIsNone(web.explicit_locale())
        # ...so a Chinese browser is Chinese again, not stuck on the old choice.
        self.assertEqual(self.locale_for(CN_BROWSER), "CN")

    def test_restores_the_documented_menu_defaults(self):
        for key in ("terminal_enabled", "nas_enabled", "lxc_enabled", "gps_enabled"):
            self.store[key] = "wrong"
        with patch.object(web, "_set_tcp_congestion"):
            self._client().post("/api/v1/general_settings/reset.json",
                                headers={"X-CSRF-Token": "t"})
        self.assertEqual(self.store["terminal_enabled"], True)
        self.assertEqual(self.store["nas_enabled"], True)
        self.assertEqual(self.store["lxc_enabled"], False)
        self.assertEqual(self.store["gps_enabled"], False)

    def test_restores_the_kernel_default_congestion_control(self):
        with patch.object(web, "_set_tcp_congestion") as set_tcp:
            self._client().post("/api/v1/general_settings/reset.json",
                                headers={"X-CSRF-Token": "t"})
        set_tcp.assert_called_once_with("cubic")

    def test_restores_hostname_and_timezone(self):
        """It says reset, so it resets everything on the page."""
        with patch.object(web, "_set_tcp_congestion"), \
                patch.object(web, "subprocess"), \
                patch.object(web, "_openwrt_uci_set") as uci_set, \
                patch.object(web, "_openwrt_uci_commit") as uci_commit:
            resp = self._client().post("/api/v1/general_settings/reset.json",
                                       headers={"X-CSRF-Token": "t"})
        written = " ".join(c.args[0] for c in uci_set.call_args_list)
        self.assertIn("system.@system[0].hostname=", written)
        self.assertIn("system.@system[0].zonename=Asia/Shanghai", written)
        self.assertIn("system.@system[0].timezone=CST-8", written)
        uci_commit.assert_called_once_with("system")
        body = resp.get_json()["reset"]
        self.assertIn("hostname", body)
        self.assertIn("timezone", body)

    def test_factory_hostname_follows_the_board(self):
        """Same rule /rom/etc/uci-defaults/99-default-settings applies."""
        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if str(path) == "/proc/device-tree/compatible":
                import io
                return io.BytesIO(b"ariaboard,photonicat2\0rockchip,rk3576\0")
            return real_open(path, *a, **kw)

        with patch("builtins.open", fake_open):
            self.assertEqual(web.factory_hostname(), "photonicat2-openwrt")

    def test_factory_hostname_falls_back_off_device(self):
        with patch("builtins.open", side_effect=OSError):
            self.assertEqual(web.factory_hostname(), "photonicat-openwrt")

    def test_a_failing_sysctl_does_not_abort_the_rest(self):
        web.set_explicit_theme("dark")
        with patch.object(web, "_set_tcp_congestion",
                          side_effect=OSError("read-only fs")):
            resp = self._client().post("/api/v1/general_settings/reset.json",
                                       headers={"X-CSRF-Token": "t"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(web.explicit_theme())
        self.assertNotIn("tcp_congestion", resp.get_json()["reset"])


class SettingsTemplateKeysTests(unittest.TestCase):
    """Every locale key the settings page references must actually exist.

    The reset dialog shipped with a Cancel button that rendered as an empty
    box, because it used locales["cancel"] -- a key that lives in the `update`
    section, not this page's. Jinja renders a missing key as empty string, so
    nothing failed loudly; the button was just blank. This catches that.
    """

    TEMPLATE = os.path.join(REPO_ROOT, "app", "templates", "general_settings.html")
    SECTION = "general_settings"

    def _keys(self):
        import re
        with open(self.TEMPLATE, encoding="utf-8") as fh:
            src = fh.read()
        # Both spellings: source uses locales["x"], the minified build output
        # rewrites it to locales.x.
        return (set(re.findall(r'locales\["([a-zA-Z0-9_]+)"\]', src))
                | set(re.findall(r'locales\.([a-zA-Z0-9_]+)', src)))

    def _resolved(self, lang):
        import locales as loc_pkg
        table = loc_pkg.get_locales(self.SECTION, lang)
        table.update(loc_pkg.get_base_locales(lang))
        return table

    def test_every_referenced_key_exists_in_english(self):
        table = self._resolved("EN")
        missing = sorted(k for k in self._keys() if not table.get(k))
        self.assertEqual(missing, [], "blank text on the page for: %r" % (missing,))

    def test_every_referenced_key_exists_in_chinese(self):
        table = self._resolved("CN")
        missing = sorted(k for k in self._keys() if not table.get(k))
        self.assertEqual(missing, [], "blank text on the page for: %r" % (missing,))

    def test_the_reset_dialog_strings_are_all_present(self):
        for lang in ("EN", "CN"):
            table = self._resolved(lang)
            for key in ("reset_defaults_button", "reset_defaults_title",
                        "reset_defaults_body", "reset_defaults_note",
                        "reset_defaults_confirm", "common_cancel", "saving"):
                self.assertTrue(table.get(key),
                                "%s missing %s" % (lang, key))


if __name__ == "__main__":
    unittest.main()
