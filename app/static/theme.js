/**
 * Night mode / theme preference.
 * Values: "auto" (follow browser prefers-color-scheme), "light", "dark".
 *
 * Resolved most deliberate signal first, the same rule the UI language uses:
 *
 *   1. An explicit device-wide choice (light/dark), rendered into <head> as
 *      window.PCAT_DEVICE_THEME before this file runs. It applies to every
 *      browser, because "set the appearance" on an appliance is expected to
 *      mean the appliance, not the tab it was clicked in.
 *   2. Otherwise this browser's localStorage, which is all an unauthenticated
 *      visitor on the login page can set.
 *   3. Otherwise the browser's own prefers-color-scheme.
 *
 * "auto" is the absence of a device choice rather than a third stored value,
 * so setting a device back to auto genuinely returns every browser to its own
 * system preference.
 *
 * Still applied from localStorage/inline before paint: this file is loaded
 * with a blocking <script> in <head>, so no request is made before the first
 * paint and there is no flash of the wrong theme.
 */
(function (global) {
  var KEY = "pcat-theme";
  var VALID = { auto: 1, light: 1, dark: 1 };

  function devicePref() {
    var v = global.PCAT_DEVICE_THEME;
    return v === "light" || v === "dark" ? v : null;
  }

  function browserPref() {
    try {
      var v = localStorage.getItem(KEY);
      return VALID[v] ? v : "auto";
    } catch (e) {
      return "auto";
    }
  }

  function getPref() {
    return devicePref() || browserPref();
  }

  function systemDark() {
    return !!(
      global.matchMedia &&
      global.matchMedia("(prefers-color-scheme: dark)").matches
    );
  }

  function resolveDark(pref) {
    if (pref === "dark") return true;
    if (pref === "light") return false;
    return systemDark();
  }

  function apply(pref) {
    pref = VALID[pref] ? pref : getPref();
    var dark = resolveDark(pref);
    var root = document.documentElement;
    root.classList.toggle("dark", dark);
    root.setAttribute("data-theme", pref);
    root.setAttribute("data-theme-resolved", dark ? "dark" : "light");
    try {
      var metas = document.querySelectorAll('meta[name="theme-color"]');
      for (var i = 0; i < metas.length; i++) {
        metas[i].setAttribute("content", dark ? "#0f1419" : "#0ed3cf");
      }
    } catch (e) {}
    try {
      global.dispatchEvent(
        new CustomEvent("pcat-theme-change", {
          detail: { pref: pref, dark: dark },
        })
      );
    } catch (e) {}
    return dark;
  }

  function setPref(pref) {
    if (!VALID[pref]) pref = "auto";
    try {
      localStorage.setItem(KEY, pref);
    } catch (e) {}
    /* Update the device value we were rendered with, so the choice takes
     * effect immediately rather than after a reload -- and so picking "auto"
     * actually escapes a device-wide light/dark instead of being overruled by
     * the stale value on this page. */
    global.PCAT_DEVICE_THEME = pref === "auto" ? null : pref;
    /* Persist device-wide. Best effort by design: the login page's cycle is
     * unauthenticated and gets a 401, which correctly leaves that visitor with
     * a localStorage-only preference. The CSRF wrapper in _base.html installs
     * itself after this file loads but long before any click, so the token is
     * attached by then. */
    try {
      global.fetch("/api/v1/theme.json", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme: pref }),
      })["catch"](function () {});
    } catch (e) {}
    return apply(pref);
  }

  function cycle() {
    var order = ["auto", "light", "dark"];
    var cur = getPref();
    var next = order[(order.indexOf(cur) + 1) % order.length];
    setPref(next);
    return next;
  }

  // Apply immediately (before first paint when loaded in <head>)
  apply(getPref());

  if (global.matchMedia) {
    try {
      var mq = global.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function () {
        if (getPref() === "auto") apply("auto");
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    } catch (e) {}
  }

  global.PcatTheme = {
    KEY: KEY,
    getPref: getPref,
    setPref: setPref,
    apply: apply,
    cycle: cycle,
    isDark: function () {
      return document.documentElement.classList.contains("dark");
    },
    systemDark: systemDark,
  };
})(window);
