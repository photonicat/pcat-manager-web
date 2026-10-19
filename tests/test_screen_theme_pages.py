#!/usr/bin/env python3
"""Unit tests for which LCD pages a colour theme actually reaches.

applyTheme() recoloured whichever layout it found -- the user's if one was
saved, else the firmware default -- and a saved layout only holds the pages
that existed when it was saved. The GPS page ships in the default and is
appended by the firmware at runtime, so it was absent from any older saved
layout; the firmware then merges per key, leaving that page in the old
palette. A themed device showed pages 0-3 in the new colours and the GPS page
still in PCAT Classic yellow.

The recolour rules live in screen.html (JavaScript), so they are mirrored
here and checked against real config shapes. That keeps the page-union rule
and the role mapping pinned even though the template is not importable.

    python3 -m unittest tests.test_screen_theme_pages -v
"""

import copy
import unittest


AURORA = {
    "primary": [70, 235, 145],
    "accent": [80, 170, 255],
    "data": [215, 255, 235],
}
CLASSIC_YELLOW = [255, 229, 0]
CLASSIC_DATA = [255, 229, 255]

BIG_FONTS = ("huge", "gigantic", "big", "reg")
THEMABLE_TYPES = ("icon", "cpu_bars", "hbar", "disk_bars")


def theme_role(el):
    """Mirror of themeRole() in screen.html."""
    if el.get("type") == "icon":
        return "accent" if "hline" in (el.get("icon_path") or "") else "primary"
    if el.get("type") in ("cpu_bars", "hbar", "disk_bars"):
        return "primary"
    if el.get("type") == "fixed_text":
        return "accent"
    return "primary" if el.get("font") in BIG_FONTS else "data"


def recolor_elements(elements, colors):
    """Mirror of recolorElements() in screen.html."""
    for page in elements.values():
        for el in page:
            if not isinstance(el.get("color"), list) \
                    and el.get("type") not in THEMABLE_TYPES:
                continue
            el["color"] = list(colors[theme_role(el)])


def apply_theme(default_cfg, user_cfg, colors):
    """Mirror of applyTheme()'s layout selection, INCLUDING the fix: start
    from the saved layout, then fill in any default page it lacks."""
    dt = (user_cfg.get("display_template")
          if (user_cfg.get("display_template") or {}).get("elements")
          else default_cfg.get("display_template", {"elements": {}}))
    elements = copy.deepcopy(dt.get("elements", {}))
    def_els = (default_cfg.get("display_template") or {}).get("elements", {})
    for k, v in def_els.items():
        if k not in elements:
            elements[k] = copy.deepcopy(v)
    recolor_elements(elements, colors)
    return elements


def make_default():
    """Firmware default: five pages, page4 is GPS, all classic yellow."""
    def page(keys, gps=False):
        els = []
        for i, k in enumerate(keys):
            els.append({
                "type": "text",
                "font": "huge" if i == 0 else "small",
                "data_key": ("Gps" + k) if gps else k,
                "color": list(CLASSIC_YELLOW if i == 0 else CLASSIC_DATA),
            })
        return els
    return {"display_template": {"elements": {
        "page0": page(["CpuUsage", "MemUsage"]),
        "page1": page(["WanIp", "LanIp"]),
        "page2": page(["Battery", "Temp"]),
        "page3": page(["NetUp", "NetDown"]),
        "page4": page(["Speed", "Altitude", "Sats"], gps=True),
    }}}


def make_user_saved_before_gps(default_cfg):
    """A layout saved before the GPS page existed: pages 0-3 only."""
    els = copy.deepcopy(default_cfg["display_template"]["elements"])
    els.pop("page4")
    return {"display_template": {"elements": els}}


class ThemePageCoverageTest(unittest.TestCase):
    def setUp(self):
        self.default = make_default()
        self.user = make_user_saved_before_gps(self.default)

    def _colors_on(self, elements, page):
        return {tuple(el["color"]) for el in elements[page] if el.get("color")}

    def test_gps_page_is_themed_even_when_missing_from_saved_layout(self):
        out = apply_theme(self.default, self.user, AURORA)
        self.assertIn("page4", out, "the GPS page must survive theming")
        self.assertNotIn(tuple(CLASSIC_YELLOW), self._colors_on(out, "page4"),
                         "the GPS page kept its pre-theme yellow")
        self.assertTrue(
            self._colors_on(out, "page4") <= {tuple(v) for v in AURORA.values()})

    def test_every_default_page_is_themed(self):
        out = apply_theme(self.default, self.user, AURORA)
        themed = {tuple(v) for v in AURORA.values()}
        for page in self.default["display_template"]["elements"]:
            self.assertIn(page, out)
            self.assertTrue(
                self._colors_on(out, page) <= themed,
                "page {} was left un-themed".format(page))

    def test_saved_layout_still_wins_over_the_default(self):
        # The union must not discard user edits: a page the user saved keeps
        # its own elements (here: an extra element the default lacks).
        self.user["display_template"]["elements"]["page0"].append(
            {"type": "text", "font": "small", "data_key": "Custom",
             "color": list(CLASSIC_DATA)})
        out = apply_theme(self.default, self.user, AURORA)
        keys = [el["data_key"] for el in out["page0"]]
        self.assertIn("Custom", keys,
                      "the user's own element was dropped by the union")

    def test_theming_is_idempotent(self):
        once = apply_theme(self.default, self.user, AURORA)
        twice = apply_theme(self.default,
                            {"display_template": {"elements": once}}, AURORA)
        self.assertEqual(once, twice,
                         "re-applying a theme must not drift; roles come from "
                         "type/font, never from the current colour")

    def test_a_layout_covering_every_page_is_unaffected_by_the_union(self):
        full = {"display_template": {
            "elements": copy.deepcopy(
                self.default["display_template"]["elements"])}}
        out = apply_theme(self.default, full, AURORA)
        self.assertEqual(sorted(out), sorted(
            self.default["display_template"]["elements"]))


class ThemeRoleTest(unittest.TestCase):
    """The role split the SMS palette resolver also depends on."""

    def test_big_font_text_takes_primary(self):
        for f in BIG_FONTS:
            self.assertEqual(theme_role({"type": "text", "font": f}), "primary")

    def test_small_text_takes_data(self):
        self.assertEqual(theme_role({"type": "text", "font": "small"}), "data")

    def test_fixed_text_labels_take_accent(self):
        self.assertEqual(theme_role({"type": "fixed_text"}), "accent")

    def test_separator_icons_take_accent_other_icons_primary(self):
        self.assertEqual(
            theme_role({"type": "icon", "icon_path": "assets/hline.svg"}),
            "accent")
        self.assertEqual(
            theme_role({"type": "icon", "icon_path": "assets/batt.svg"}),
            "primary")

    def test_bars_take_primary(self):
        for t in ("cpu_bars", "hbar", "disk_bars"):
            self.assertEqual(theme_role({"type": t}), "primary")


class SmsPaletteResolverTest(unittest.TestCase):
    """Mirror of smsThemeColors() in processSms.go.

    The SMS pages are drawn in Go, not from template elements, so a theme
    could never reach them; the palette is instead derived from the themed
    pages using the same role split. Unthemed must stay classic yellow so
    existing devices look unchanged."""

    @staticmethod
    def resolve(elements, num_pages=8, fallback=tuple(CLASSIC_YELLOW)):
        for i in range(num_pages):
            page = elements.get("page{}".format(i))
            if not page:
                continue
            for el in page:
                col = el.get("color")
                if not col or len(col) < 3 or el.get("type") == "icon":
                    continue
                if el.get("font") in BIG_FONTS:
                    return tuple(col)
        return fallback

    def test_unthemed_default_resolves_to_classic_yellow(self):
        els = make_default()["display_template"]["elements"]
        self.assertEqual(self.resolve(els), tuple(CLASSIC_YELLOW))

    def test_themed_layout_resolves_to_the_theme_primary(self):
        default = make_default()
        user = make_user_saved_before_gps(default)
        themed = apply_theme(default, user, AURORA)
        self.assertEqual(self.resolve(themed), tuple(AURORA["primary"]))

    def test_icons_are_skipped_when_resolving(self):
        els = {"page0": [
            {"type": "icon", "font": "huge", "color": [1, 2, 3]},
            {"type": "text", "font": "huge", "color": list(AURORA["primary"])},
        ]}
        self.assertEqual(self.resolve(els), tuple(AURORA["primary"]))

    def test_empty_layout_falls_back(self):
        self.assertEqual(self.resolve({}), tuple(CLASSIC_YELLOW))


if __name__ == "__main__":
    unittest.main(verbosity=2)
