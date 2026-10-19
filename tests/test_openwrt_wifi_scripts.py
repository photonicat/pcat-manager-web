#!/usr/bin/env python3
"""Unit tests for the firmware's WiFi first-run / hotplug scripts (bug 3).

The reported symptom was "my WiFi settings revert to defaults", and the
mechanism lived outside this repo: pcat-wifi-config-reset re-runs on every
boot until pcat-firstrun.sh's one-shot latch lands, and on a unit whose card
missed enumeration on the first boot (the hub is unreliable) the latch stayed
unset — so a later boot ran `rm -f /etc/config/wireless` over whatever the
customer had configured in between.

These tests execute the REAL scripts from the sibling openwrt checkout inside
a sandbox: absolute paths are rewritten into a temp dir and the external
commands (uci, wifi, strings, sleep, usbmode) are PATH stubs that record
their invocations. Skipped cleanly when ../openwrt is not checked out.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_openwrt_wifi_scripts`.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

BASE_FILES = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "openwrt",
    "target", "linux", "rockchip", "armv8", "base-files"))
RESET_SCRIPT = os.path.join(BASE_FILES, "usr", "bin", "pcat-wifi-config-reset")
FIRSTRUN_SCRIPT = os.path.join(BASE_FILES, "usr", "bin", "pcat-firstrun.sh")
HOTPLUG_SCRIPT = os.path.join(
    BASE_FILES, "etc", "hotplug.d", "ieee80211", "05-usb-wifi-config")

HAVE_TREE = all(os.path.exists(p)
                for p in (RESET_SCRIPT, FIRSTRUN_SCRIPT, HOTPLUG_SCRIPT))

CUSTOMER_UCI = """wireless.radio0=wifi-device
wireless.default_radio0=wifi-iface
wireless.default_radio0.ssid='MyHomeWiFi'
wireless.radio1=wifi-device
wireless.default_radio1.ssid='MyHomeWiFi-5G'
"""

FACTORY_UCI = """wireless.default_radio0.ssid='photonicat2-4a99c96b'
"""

STOCK_UCI = """wireless.default_radio0.ssid='OpenWrt'
wireless.default_radio1.ssid='OpenWrt'
"""


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(HAVE_TREE, "../openwrt checkout not present")
class _ScriptSandbox(unittest.TestCase):
    """Temp-dir sandbox: rewritten scripts + recording command stubs."""

    def setUp(self):
        self.sb = tempfile.mkdtemp(prefix="pcat-wifi-scripts-")
        self.addCleanup(shutil.rmtree, self.sb, ignore_errors=True)
        for sub in ("bin", "etc/config", "tmp", "sys", "usr/bin"):
            os.makedirs(os.path.join(self.sb, sub), exist_ok=True)

        self.wireless = os.path.join(self.sb, "etc/config/wireless")
        self.marker = os.path.join(self.sb, "tmp/pcat-wifi-ready.tmp")
        self.latch = os.path.join(self.sb, "etc/pcat-wlan-init-completed")
        self.calls = os.path.join(self.sb, "calls.log")
        self.uci_show = os.path.join(self.sb, "uci-show-wireless.txt")
        self.reset = os.path.join(self.sb, "usr/bin/pcat-wifi-config-reset")
        self.firstrun = os.path.join(self.sb, "usr/bin/pcat-firstrun.sh")

        self._stub("uci", '''#!/bin/bash
echo "uci $*" >> "$CALL_LOG"
case "$*" in
  *"show wireless"*) cat "$UCI_SHOW_WIRELESS" 2>/dev/null ;;
  *"show fstab"*) : ;;
esac
exit 0
''')
        self._stub("wifi", '#!/bin/bash\necho "wifi $*" >> "$CALL_LOG"\nexit 0\n')
        self._stub("strings", '#!/bin/bash\necho "ariaboard,photonicat2"\n')
        self._stub("sleep", '#!/bin/bash\nexit 0\n')
        self._stub("usbmode", '#!/bin/bash\nexit 0\n')

        self._install(RESET_SCRIPT, self.reset)
        self._install(FIRSTRUN_SCRIPT, self.firstrun)

    def _stub(self, name, body):
        path = os.path.join(self.sb, "bin", name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def _install(self, src_path, dst_path):
        """The real script, with its absolute paths bent into the sandbox."""
        src = read(src_path)
        src = src.replace("#!/bin/sh", "#!/bin/bash", 1)
        for orig, sandboxed in (
                ("/etc/config/wireless", self.wireless),
                ("/tmp/pcat-wifi-ready.tmp", self.marker),
                ("/etc/pcat-wlan-init-completed", self.latch),
                ("/usr/bin/pcat-wifi-config-reset", self.reset),
                ("/usr/bin/pcat-firstrun.sh", self.firstrun),
                ("/usr/share/pcat-manager-web/oem.txt",
                 os.path.join(self.sb, "oem.txt")),
                ("/sys/", os.path.join(self.sb, "sys") + "/"),
        ):
            src = src.replace(orig, sandboxed)
        with open(dst_path, "w") as fh:
            fh.write(src)
        os.chmod(dst_path, os.stat(dst_path).st_mode | stat.S_IEXEC)

    def _run(self, script, timeout=60):
        env = dict(os.environ,
                   PATH=os.path.join(self.sb, "bin") + os.pathsep +
                        os.environ.get("PATH", ""),
                   CALL_LOG=self.calls,
                   UCI_SHOW_WIRELESS=self.uci_show)
        return subprocess.run(["/bin/bash", script], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def _seed(self, uci_output, wireless="config wifi-device 'radio0'\n"):
        with open(self.uci_show, "w") as fh:
            fh.write(uci_output)
        with open(self.wireless, "w") as fh:
            fh.write(wireless)

    def call_log(self):
        return read(self.calls) if os.path.exists(self.calls) else ""


class ConfigResetGuardTests(_ScriptSandbox):

    def test_customer_config_is_never_destroyed(self):
        # The bug: a re-run on the boot AFTER the customer configured their
        # WiFi wiped /etc/config/wireless back to factory.
        self._seed(CUSTOMER_UCI, wireless="customer config bytes\n")
        proc = self._run(self.reset)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read(self.wireless), "customer config bytes\n",
                         "the customer's wireless config was rewritten")
        self.assertNotIn("wifi down", self.call_log(),
                         "the radios were torn down on a configured device")
        self.assertTrue(os.path.exists(self.marker),
                        "the ready marker must land so the one-shot latch "
                        "can finally set — otherwise this re-runs forever")

    def test_own_factory_branding_counts_as_done(self):
        # A previous successful run wrote photonicat2-<suffix>: also not
        # stock, also must not be wiped, and the latch must land.
        self._seed(FACTORY_UCI, wireless="factory config bytes\n")
        proc = self._run(self.reset)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read(self.wireless), "factory config bytes\n")
        self.assertTrue(os.path.exists(self.marker))

    def test_stock_config_with_no_card_retries_next_boot(self):
        # True first boot, WiFi card missing (the unreliable hub): no wipe,
        # no marker — the next boot gets another chance.
        self._seed(STOCK_UCI, wireless="stock config bytes\n")
        proc = self._run(self.reset)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read(self.wireless), "stock config bytes\n")
        self.assertFalse(os.path.exists(self.marker),
                         "a cardless boot must NOT claim first-run success")
        self.assertNotIn("wifi down", self.call_log())


class FirstrunLatchTests(_ScriptSandbox):

    def test_configured_device_latches_and_self_deletes(self):
        # Mechanism A end to end: latch missing, customer config present.
        # firstrun must leave the config alone, write the latch, and delete
        # itself so no later boot can ever wipe the device.
        self._seed(CUSTOMER_UCI, wireless="customer config bytes\n")
        proc = self._run(self.firstrun)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(read(self.wireless), "customer config bytes\n")
        self.assertTrue(os.path.exists(self.latch), "latch never landed")
        self.assertFalse(os.path.exists(self.firstrun),
                         "firstrun must self-delete once done")

    def test_cardless_boot_keeps_the_retry(self):
        self._seed(STOCK_UCI)
        proc = self._run(self.firstrun)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(self.latch))
        self.assertTrue(os.path.exists(self.firstrun),
                        "firstrun must stay in place to retry next boot")


@unittest.skipUnless(HAVE_TREE, "../openwrt checkout not present")
class ScriptStaticContractTests(unittest.TestCase):
    """Shape checks that don't need execution."""

    def test_scripts_parse(self):
        for path in (RESET_SCRIPT, FIRSTRUN_SCRIPT, HOTPLUG_SCRIPT):
            proc = subprocess.run(["/bin/bash", "-n", path],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             "%s: %s" % (path, proc.stderr))

    def test_hotplug_uses_case_not_double_bracket_globs(self):
        # busybox builds differ on whether a quoted [[ pattern ]] globs;
        # `case` is the only portable spelling and must not regress.
        src = read(HOTPLUG_SCRIPT)
        code = re.sub(r"(?m)#.*$", "", src)  # the fix's comment names [[
        self.assertNotIn("[[", code)
        self.assertEqual(code.count("*platform/soc/23000000.usb/xhci-hcd.*"), 2,
                         "both onboard-USB path matches must survive")

    def test_hotplug_keeps_the_delayed_ap_retry(self):
        src = read(HOTPLUG_SCRIPT)
        self.assertIn("ADDED_PHY=", src)
        self.assertRegex(src, r"grep -q \"\^\$\{ADDED_PHY\}-\"",
                         "the no-netifd-interface check is gone")

    def test_reset_guard_precedes_the_wipe(self):
        src = read(RESET_SCRIPT)
        self.assertLess(src.index("pcat-wifi-ready.tmp"),
                        src.index("rm -f /etc/config/wireless"),
                        "the non-stock-SSID guard must run before the wipe")

    def test_reset_never_brands_the_pcie_card_alone(self):
        src = read(RESET_SCRIPT)
        self.assertRegex(
            src, r'SCAN_INTERNAL_USB_WLAN.*=.*x"1".*-a.*MACADDR.*=.*x""',
            "the PCIe-only guard is gone: a boot where only the PCIe card "
            "enumerated would brand it as a 2.4GHz radio0")


if __name__ == "__main__":
    unittest.main()
