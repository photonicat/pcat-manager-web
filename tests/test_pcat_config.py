#!/usr/bin/env python3
"""Unit tests for pcat_config -- the user-config substrate everything else
reads through.

This module was at 0% coverage despite being imported by nearly every other
module: mode validation, fan clamping, the smart-fan curve, the saved-upstream
list and the power-cut-safe atomic writer all lived untested. Each test class
isolates the module onto a temp config file, so nothing touches /etc and no
state leaks between tests.

Runs anywhere: no device, no sysfs.

    python3 -m unittest tests.test_pcat_config -v
"""

import builtins
import configparser
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

_APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, _APP_DIR)


def _load_real_pcat_config():
    """Load the real module even under tests/run_tests.py, which wraps the
    whole suite in patch.dict(sys.modules, {'pcat_config': MagicMock()}) (see
    the note in test_remember.py). A plain `import pcat_config` would hand
    this file the MagicMock and every assertion here would test nothing, so
    load from the source file under a private name instead."""
    spec = importlib.util.spec_from_file_location(
        "pcat_config_under_test", os.path.join(_APP_DIR, "pcat_config.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pc = _load_real_pcat_config()


class ConfigTestBase(unittest.TestCase):
    """Redirect the config file to a temp dir and start from defaults."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = pc.user_config_path
        self._orig_cfg = pc.global_user_config
        pc.user_config_path = os.path.join(self.tmpdir.name, "user.json")
        pc.global_user_config = dict(pc.default_user_config)

    def tearDown(self):
        pc.user_config_path = self._orig_path
        pc.global_user_config = self._orig_cfg
        self.tmpdir.cleanup()

    def saved(self):
        """What actually reached disk."""
        with open(pc.user_config_path) as f:
            return json.load(f)


class NetworkModeTest(ConfigTestBase):

    def test_defaults_to_auto(self):
        self.assertEqual(pc.get_network_mode(), "auto")

    def test_legacy_smart_wan_reads_as_priority(self):
        """A device that stored the retired 'smart_wan' mode must never be
        reported as a mode the rest of the app no longer handles."""
        pc.global_user_config["network_mode"] = "smart_wan"
        self.assertEqual(pc.get_network_mode(), "priority")

    def test_setting_smart_wan_stores_priority(self):
        pc.set_network_mode("smart_wan")
        self.assertEqual(pc.global_user_config["network_mode"], "priority")

    def test_invalid_mode_falls_back_to_auto(self):
        pc.set_network_mode("warp_drive")
        self.assertEqual(pc.get_network_mode(), "auto")

    def test_valid_modes_round_trip(self):
        for mode in ("auto", "eth_only", "cell_only", "all_off", "priority"):
            pc.set_network_mode(mode)
            self.assertEqual(pc.get_network_mode(), mode)
            self.assertEqual(self.saved()["network_mode"], mode)


class ModeValidationTest(ConfigTestBase):

    def test_invalid_power_mode_falls_back_to_manual(self):
        pc.set_power_mode("hyperdrive")
        self.assertEqual(pc.get_power_mode(), "manual")

    def test_power_modes_round_trip(self):
        for mode in ("manual", "car", "timer", "direct_power"):
            pc.set_power_mode(mode)
            self.assertEqual(pc.get_power_mode(), mode)

    def test_invalid_fan_mode_falls_back_to_smart_quiet(self):
        pc.set_fan_mode("turbo")
        self.assertEqual(pc.get_fan_mode(), "smart_quiet")

    def test_invalid_wifi_wan_radio_falls_back_to_onboard(self):
        pc.set_wifi_wan_radio("usb")
        self.assertEqual(pc.get_wifi_wan_radio(), "onboard")

    def test_garbage_stored_radio_reads_as_onboard(self):
        """Validation on read too: a hand-edited or corrupt config file must
        not leak an unknown radio class into wifi_wan's radio lookup."""
        pc.global_user_config["wifi_wan_radio"] = "radio7"
        self.assertEqual(pc.get_wifi_wan_radio(), "onboard")


class FanSpeedTest(ConfigTestBase):

    def test_clamp_bounds_and_garbage(self):
        cases = [(12, 9), (-3, 0), (0, 0), (9, 9), (4.7, 4), ("7", 7),
                 ("abc", 5), (None, 5)]
        for raw, want in cases:
            self.assertEqual(pc._clamp_fan_speed(raw), want,
                             "clamp({!r})".format(raw))

    def test_get_fan_speed_clamps_stored_garbage(self):
        pc.global_user_config["fan_speed"] = "not a number"
        self.assertEqual(pc.get_fan_speed(), 5)
        pc.global_user_config["fan_speed"] = 99
        self.assertEqual(pc.get_fan_speed(), 9)

    def test_preference_survives_hardware_write_failure(self):
        """Issue #107: the preference must persist even when the sysfs write
        fails, or the slider reverts to 5 on the next page load."""
        with patch.object(pc, "set_fan_speed_direct",
                          side_effect=OSError("pwm gone")):
            try:
                pc.set_fan_speed(8)
            except OSError:
                pass  # the hardware failure may propagate; the save must not
        self.assertEqual(self.saved()["fan_speed"], 8)


class SmartFanCurveTest(ConfigTestBase):
    """set_fan_speed_smart: the temperature -> speed policy, with the
    hardware write and charging probe stubbed out."""

    def setUp(self):
        super().setUp()
        self.applied = []
        self._p1 = patch.object(pc, "set_fan_speed_direct", self.applied.append)
        self._p1.start()

    def tearDown(self):
        self._p1.stop()
        super().tearDown()

    def smart(self, temp, user_max=9, mode="smart_quiet", charging=True):
        with patch.object(pc, "is_device_charging", return_value=charging):
            return pc.set_fan_speed_smart(temp, user_max, mode)

    def test_none_temperature_turns_fan_off(self):
        self.assertEqual(self.smart(None), 0)
        self.assertEqual(self.applied[-1], 0)

    def test_critical_temp_overrides_user_cap(self):
        """Above 60C the machine protects itself even if the user set the fan
        to near-silent."""
        self.assertEqual(self.smart(61, user_max=1), 8)

    def test_below_threshold_is_off(self):
        self.assertEqual(self.smart(44, charging=True), 0)
        self.assertEqual(self.smart(51, charging=False), 0)

    def test_charging_threshold_starts_at_45(self):
        self.assertEqual(self.smart(45, charging=True), 1)
        self.assertEqual(self.smart(50, charging=True), 6)

    def test_discharging_threshold_starts_at_52(self):
        """On battery the fan itself costs runtime, so it starts later."""
        self.assertEqual(self.smart(52, charging=False), 1)
        self.assertEqual(self.smart(45, charging=False), 0)

    def test_user_max_caps_the_curve(self):
        self.assertEqual(self.smart(55, user_max=3, charging=True), 3)

    def test_none_user_max_defaults_to_medium(self):
        self.assertEqual(self.smart(59, user_max=None, charging=True), 5)

    def test_smart_cool_starts_earlier(self):
        self.assertEqual(self.smart(43, mode="smart_cool", charging=True), 1)
        self.assertEqual(self.smart(43, mode="smart_quiet", charging=True), 0)

    def test_every_result_reaches_the_hardware(self):
        self.smart(55, user_max=4)
        self.assertEqual(self.applied[-1], 4)


class CpinTest(ConfigTestBase):

    def test_short_cpin_reads_as_empty(self):
        """A SIM PIN is at least 4 digits; 1-2 stray characters in the config
        must not be sent to the modem as a PIN."""
        pc.global_user_config["cpin"] = "12"
        self.assertEqual(pc.get_cpin(), "")

    def test_real_cpin_round_trips(self):
        pc.set_cpin("1234")
        self.assertEqual(pc.get_cpin(), "1234")

    def test_clearing_cpin(self):
        pc.set_cpin("1234")
        pc.set_cpin("")
        self.assertEqual(pc.get_cpin(), "")


class WifiWanNetworksTest(ConfigTestBase):
    """The saved-upstreams list: most-recently-used first, deduped by SSID,
    capped at 16. wifi_wan's reconcile/roam logic trusts this ordering."""

    def remember(self, ssid, **kw):
        args = dict(encryption="psk2", key="pw", band="2g", channel=6)
        args.update(kw)
        pc.remember_wifi_wan_network(ssid, args["encryption"], args["key"],
                                     args["band"], args["channel"],
                                     random_mac=args.get("random_mac", False),
                                     custom_mac=args.get("custom_mac", ""),
                                     login_cmd=args.get("login_cmd", ""),
                                     eap=args.get("eap"))

    def test_most_recent_first(self):
        self.remember("home")
        self.remember("office")
        self.assertEqual([n["ssid"] for n in pc.get_wifi_wan_networks()],
                         ["office", "home"])

    def test_rejoining_moves_to_front_and_updates(self):
        self.remember("home", key="old")
        self.remember("office")
        self.remember("home", key="new")
        nets = pc.get_wifi_wan_networks()
        self.assertEqual([n["ssid"] for n in nets], ["home", "office"])
        self.assertEqual(nets[0]["key"], "new")
        self.assertEqual(len([n for n in nets if n["ssid"] == "home"]), 1,
                         "rejoin duplicated the entry instead of refreshing it")

    def test_capped_at_16(self):
        for i in range(20):
            self.remember("net{}".format(i))
        nets = pc.get_wifi_wan_networks()
        self.assertEqual(len(nets), 16)
        self.assertEqual(nets[0]["ssid"], "net19")  # newest kept
        self.assertNotIn("net0", [n["ssid"] for n in nets])  # oldest dropped

    def test_eap_fields_stored_only_for_eap(self):
        self.remember("corp", encryption="eap",
                      eap={"type": "ttls", "identity": "alice",
                           "anonymous_identity": "anon", "ca_cert": "/ca.pem"})
        self.remember("home", eap={"type": "peap"})  # psk2: eap must be ignored
        by_ssid = {n["ssid"]: n for n in pc.get_wifi_wan_networks()}
        self.assertEqual(by_ssid["corp"]["eap_type"], "ttls")
        self.assertEqual(by_ssid["corp"]["identity"], "alice")
        self.assertNotIn("eap_type", by_ssid["home"])

    def test_custom_mac_is_stored_and_defaults_empty(self):
        # The custom STA MAC must survive in the saved record so a
        # reconnect/rebuild presents the same (authorized) address.
        self.remember("gated", custom_mac="dc:56:7b:aa:bb:01")
        self.remember("home")
        by_ssid = {n["ssid"]: n for n in pc.get_wifi_wan_networks()}
        self.assertEqual(by_ssid["gated"]["custom_mac"], "dc:56:7b:aa:bb:01")
        self.assertEqual(by_ssid["home"]["custom_mac"], "")

    def test_forget_removes_only_that_ssid(self):
        self.remember("home")
        self.remember("office")
        pc.forget_wifi_wan_network("home")
        self.assertEqual([n["ssid"] for n in pc.get_wifi_wan_networks()],
                         ["office"])

    def test_malformed_stored_value_reads_as_empty_list(self):
        pc.global_user_config["wifi_wan_networks"] = "not a list"
        self.assertEqual(pc.get_wifi_wan_networks(), [])

    def test_persisted_to_disk(self):
        self.remember("home")
        self.assertEqual(self.saved()["wifi_wan_networks"][0]["ssid"], "home")


class UserConfigLoadTest(ConfigTestBase):
    """get_user_configs: corrupt or missing file resets to defaults -- this
    device gets power-cut, so a half-written JSON must never brick startup."""

    def test_missing_file_creates_defaults(self):
        cfg = pc.get_user_configs()
        self.assertEqual(cfg, pc.default_user_config)
        self.assertTrue(os.path.exists(pc.user_config_path))

    def test_corrupt_json_resets_to_defaults(self):
        with open(pc.user_config_path, "w") as f:
            f.write('{"network_mode": "auto", TRUNCATED')
        cfg = pc.get_user_configs()
        self.assertEqual(cfg, pc.default_user_config)
        # and the corrupt file was replaced with something loadable
        self.assertEqual(self.saved(), pc.default_user_config)

    def test_valid_file_loads_verbatim(self):
        with open(pc.user_config_path, "w") as f:
            json.dump({"network_mode": "priority", "fan_speed": 3}, f)
        cfg = pc.get_user_configs()
        self.assertEqual(cfg["network_mode"], "priority")


class AtomicWriteTest(ConfigTestBase):
    """_atomic_write: the power-cut safety net for every config save."""

    def test_writes_content(self):
        path = os.path.join(self.tmpdir.name, "out.txt")
        pc._atomic_write(path, lambda f: f.write("hello"))
        with open(path) as f:
            self.assertEqual(f.read(), "hello")

    def test_no_temp_files_left_behind(self):
        path = os.path.join(self.tmpdir.name, "out.txt")
        pc._atomic_write(path, lambda f: f.write("x"))
        leftovers = [n for n in os.listdir(self.tmpdir.name)
                     if n.startswith(".tmp_")]
        self.assertEqual(leftovers, [])

    def test_failed_write_leaves_target_untouched(self):
        """A crash mid-write must preserve the previous good file -- that is
        the whole reason this helper exists."""
        path = os.path.join(self.tmpdir.name, "out.txt")
        pc._atomic_write(path, lambda f: f.write("good"))

        def explode(f):
            f.write("partial garbage")
            raise IOError("power cut")

        with self.assertRaises(IOError):
            pc._atomic_write(path, explode)
        with open(path) as f:
            self.assertEqual(f.read(), "good")
        leftovers = [n for n in os.listdir(self.tmpdir.name)
                     if n.startswith(".tmp_")]
        self.assertEqual(leftovers, [], "failed write leaked its temp file")


class DevModeTest(ConfigTestBase):

    def setUp(self):
        super().setUp()
        self._flags = (pc.dev_mode, pc.skip_auth)

    def tearDown(self):
        pc.dev_mode, pc.skip_auth = self._flags
        super().tearDown()

    def test_production_default_is_off(self):
        pc.dev_mode = False
        pc.skip_auth = False
        self.assertFalse(pc.dev_mode_enabled())

    def test_skip_auth_implies_dev_mode(self):
        """--skip-auth already signals a development run; the dev endpoints
        must follow so on-device debugging keeps working."""
        pc.dev_mode = False
        pc.skip_auth = True
        self.assertTrue(pc.dev_mode_enabled())


# ---------------------------------------------------------------------------
# Helpers for code paths that hardcode absolute /sys and /proc paths.
# is_device_charging(), set_fan_speed_direct() and _cpu_powersave_boot_guard()
# open fixed sysfs/procfs paths directly, so the only way to exercise them off
# the device is a selective builtins.open replacement: canned content for the
# faked prefixes, recorded writes, FileNotFoundError for unknown paths under
# those prefixes, and delegation to the real open for everything else (temp
# files, coverage bookkeeping, ...).
# ---------------------------------------------------------------------------

_REAL_OPEN = builtins.open


class _RecordingWriteFile:
    """Context-manager file stand-in that records what was written where."""

    def __init__(self, sink, path):
        self._sink = sink
        self._path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        self._sink.append((self._path, data))


def _fake_sysfs_open(reads, writes, prefixes):
    prefixes = tuple(prefixes)

    def fake_open(path, mode="r", *args, **kwargs):
        if isinstance(path, str) and path.startswith(prefixes):
            if "w" in mode or "a" in mode:
                return _RecordingWriteFile(writes, path)
            if path in reads:
                content = reads[path]
                if isinstance(content, Exception):
                    raise content
                return io.StringIO(content)
            raise FileNotFoundError(path)
        return _REAL_OPEN(path, mode, *args, **kwargs)

    return fake_open


class PackageUrlsTest(ConfigTestBase):
    """get_package_urls: builds the OTA package URLs from the OEM firmware
    base. A wrong join here points every device's package check at a 404."""

    def test_base_without_trailing_slash_gets_one(self):
        """OEM configs are hand-written; a missing trailing slash must not
        produce 'r1packages-volatile.txt' and break OTA updates."""
        with patch.object(pc._oem, "get_firmware_base_url",
                          return_value="http://fw.example.com/r1"):
            urls = pc.get_package_urls()
        self.assertEqual(urls["package_version_url"],
                         "http://fw.example.com/r1/packages-volatile.txt")
        self.assertEqual(urls["volatile_packages_url"],
                         "http://fw.example.com/r1/volatile/")

    def test_base_with_trailing_slash_not_doubled(self):
        with patch.object(pc._oem, "get_firmware_base_url",
                          return_value="http://fw.example.com/r1/"):
            urls = pc.get_package_urls()
        self.assertEqual(urls["package_version_url"],
                         "http://fw.example.com/r1/packages-volatile.txt")

    def test_no_firmware_base_means_empty_urls(self):
        """An OEM build without a firmware server must disable the package
        check, not point it at a half-built URL."""
        with patch.object(pc._oem, "get_firmware_base_url", return_value=""):
            urls = pc.get_package_urls()
        self.assertEqual(urls, {"package_version_url": "",
                                "volatile_packages_url": ""})


class OemDelegationTest(ConfigTestBase):
    """The OEM getters are thin pass-throughs to oem_config; templates call
    them on every page render, so a broken delegation blanks the branding."""

    def test_delegates_to_oem_config(self):
        cases = [
            ("get_oem_name", "ACME"),
            ("get_logo_uri", "/static/acme.png"),
            ("get_text_logo_uri", "/static/acme-text.png"),
            ("get_vendor_site_domain", "acme.example.com"),
            ("get_vendor_wiki_url", "https://wiki.acme.example.com"),
        ]
        for name, value in cases:
            with patch.object(pc._oem, name, return_value=value):
                self.assertEqual(getattr(pc, name)(), value, name)


class UserConfigLoadFailureTest(ConfigTestBase):
    """get_user_configs when even the fallback rewrite fails (read-only or
    full flash): startup must still get a usable in-memory config."""

    def test_unwritable_fallback_still_returns_defaults(self):
        with open(pc.user_config_path, "w") as f:
            f.write("{corrupt")
        with patch.object(pc, "_atomic_write",
                          side_effect=OSError("read-only fs")):
            cfg = pc.get_user_configs()
        self.assertEqual(cfg, pc.default_user_config)


class SaveUserConfigsFailureTest(ConfigTestBase):
    """save_user_configs: a full or read-only flash must not raise into the
    API request that triggered the save."""

    def test_write_error_is_swallowed(self):
        with patch.object(pc, "_atomic_write",
                          side_effect=IOError("disk full")):
            pc.save_user_configs({"network_mode": "auto"})  # must not raise


class AtomicWriteCleanupFailureTest(ConfigTestBase):
    """_atomic_write when even the temp-file cleanup fails: the original
    error must surface, not the secondary cleanup error."""

    def test_original_error_propagates_past_failed_cleanup(self):
        path = os.path.join(self.tmpdir.name, "out.txt")

        def explode(f):
            raise RuntimeError("power cut")

        with patch("os.remove", side_effect=OSError("gone")):
            with self.assertRaises(RuntimeError):
                pc._atomic_write(path, explode)
        self.assertFalse(os.path.exists(path))


class SaveConfigIniTest(ConfigTestBase):
    """save_config: the legacy INI file (/etc/pcat-manager-web.ini). Modem
    and device settings land here; missing sections crash later reads."""

    def setUp(self):
        super().setUp()
        self._orig_ini_path = pc.config_path
        self._orig_config = pc.config
        pc.config_path = os.path.join(self.tmpdir.name, "web.ini")
        pc.config = configparser.ConfigParser()

    def tearDown(self):
        pc.config_path = self._orig_ini_path
        pc.config = self._orig_config
        super().tearDown()

    def test_creates_required_sections(self):
        """Readers do config['modem'][...] unguarded; a fresh device must get
        both sections on the first save or the first read crashes."""
        pc.save_config()
        with open(pc.config_path) as f:
            content = f.read()
        self.assertIn("[modem]", content)
        self.assertIn("[device]", content)

    def test_values_round_trip_without_delimiter_spaces(self):
        pc.config.add_section("modem")
        pc.config.set("modem", "apn", "internet")
        pc.save_config()
        with open(pc.config_path) as f:
            content = f.read()
        self.assertIn("apn=internet", content)
        # existing section must not be re-added (add_section would raise)
        pc.save_config()


class SmsHookScriptTest(ConfigTestBase):
    """The SMS hook script: a user-supplied shell script run on incoming SMS.
    It must round-trip byte-for-byte and come back executable, or the hook
    silently never fires."""

    def setUp(self):
        super().setUp()
        self._orig_hook = pc.HOOK_PATH
        pc.HOOK_PATH = os.path.join(self.tmpdir.name, "hooks", "hook.sh")

    def tearDown(self):
        pc.HOOK_PATH = self._orig_hook
        super().tearDown()

    def test_missing_script_reads_as_empty(self):
        self.assertEqual(pc.load_sms_hook_script(), "")

    def test_unreadable_script_reads_as_empty(self):
        """A corrupt hook path (here: a directory) must degrade to 'no hook',
        not crash the SMS settings page."""
        os.makedirs(pc.HOOK_PATH)
        self.assertEqual(pc.load_sms_hook_script(), "")

    def test_save_round_trips_and_is_owner_executable(self):
        script = "#!/bin/sh\necho \"sms: $1\"\n"
        self.assertTrue(pc.save_sms_hook_script(script))
        self.assertEqual(pc.load_sms_hook_script(), script)
        mode = stat.S_IMODE(os.stat(pc.HOOK_PATH).st_mode)
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                         "hook must be executable by owner only")

    def test_save_failure_returns_false_and_leaves_no_temp(self):
        """The UI shows save success from this return value; a failed write
        must report False and not litter temp files next to the hook."""
        os.makedirs(os.path.dirname(pc.HOOK_PATH))
        with patch("os.replace", side_effect=OSError("read-only fs")):
            self.assertFalse(pc.save_sms_hook_script("#!/bin/sh\n"))
        leftovers = [n for n in os.listdir(os.path.dirname(pc.HOOK_PATH))
                     if n.startswith(".hook_tmp_")]
        self.assertEqual(leftovers, [], "failed save leaked its temp file")

    def test_failure_before_temp_exists_still_returns_false(self):
        """If even creating the hook directory fails, the cleanup path must
        not blow up on the temp file that was never created."""
        with patch("os.makedirs", side_effect=OSError("read-only fs")):
            self.assertFalse(pc.save_sms_hook_script("#!/bin/sh\n"))

    def test_enable_flag_round_trips_and_persists(self):
        self.assertFalse(pc.get_sms_hook_enabled())
        pc.enable_sms_hook(True)
        self.assertTrue(pc.get_sms_hook_enabled())
        self.assertTrue(self.saved()["sms_hook_enabled"])
        pc.enable_sms_hook(False)
        self.assertFalse(pc.get_sms_hook_enabled())


class BootGuardTest(ConfigTestBase):
    """_cpu_powersave_boot_guard: parking big cores during boot would slow
    init and fsck, so powersave must hold off for the first minutes."""

    def guard(self, uptime_content):
        def fake_open(path, *args, **kwargs):
            if path == "/proc/uptime":
                if uptime_content is None:
                    raise OSError("no /proc")
                return io.StringIO(uptime_content)
            return _REAL_OPEN(path, *args, **kwargs)
        with patch("builtins.open", new=fake_open):
            return pc._cpu_powersave_boot_guard()

    def test_early_uptime_blocks_powersave(self):
        self.assertTrue(self.guard("42.61 120.00\n"))

    def test_settled_system_allows_powersave(self):
        self.assertFalse(self.guard("1000000.00 3999999.00\n"))

    def test_no_uptime_source_does_not_block_the_feature(self):
        """On a dev machine without /proc the guard must not permanently
        disable powersave."""
        self.assertFalse(self.guard(None))

    def test_garbage_uptime_does_not_block_the_feature(self):
        self.assertFalse(self.guard("not-a-number junk\n"))
        self.assertFalse(self.guard(""))


class CpuTestBase(ConfigTestBase):
    """Adds a fake cpu sysfs tree under the temp dir and resets the
    per-process big-core cache and last-power-source memo around each test."""

    def setUp(self):
        super().setUp()
        self._orig_gov_path = pc.CPU_GOV_PATH
        self._orig_core_cache = pc._big_core_ids_cache
        self._orig_last_dc = pc._last_dc_connected
        self.cpuroot = os.path.join(self.tmpdir.name, "cpu")
        os.makedirs(self.cpuroot)
        pc.CPU_GOV_PATH = self.cpuroot
        pc._big_core_ids_cache = None
        pc._last_dc_connected = None

    def tearDown(self):
        pc.CPU_GOV_PATH = self._orig_gov_path
        pc._big_core_ids_cache = self._orig_core_cache
        pc._last_dc_connected = self._orig_last_dc
        super().tearDown()

    def add_cpu(self, name, freq=None, online=None, governor=None):
        cpu_dir = os.path.join(self.cpuroot, name)
        os.makedirs(os.path.join(cpu_dir, "cpufreq"), exist_ok=True)
        if freq is not None:
            with open(os.path.join(cpu_dir, "cpufreq",
                                   "cpuinfo_max_freq"), "w") as f:
                f.write(str(freq) + "\n")
        if online is not None:
            with open(os.path.join(cpu_dir, "online"), "w") as f:
                f.write(online)
        if governor is not None:
            with open(os.path.join(cpu_dir, "cpufreq",
                                   "scaling_governor"), "w") as f:
                f.write(governor + "\n")
        return cpu_dir

    def read_cpu_file(self, name, *rel):
        with open(os.path.join(self.cpuroot, name, *rel)) as f:
            return f.read().strip()


class BigCoreDetectionTest(CpuTestBase):
    """_get_big_core_ids: picks the A72 cores by max frequency. Getting this
    wrong parks the wrong cores -- or none -- when powersave engages."""

    def test_highest_freq_cores_are_the_big_ones(self):
        self.add_cpu("cpu0", freq=408000)
        self.add_cpu("cpu1", freq=408000)
        self.add_cpu("cpu4", freq=2016000)
        self.add_cpu("cpu5", freq=2016000)
        self.assertEqual(sorted(pc._get_big_core_ids()), ["cpu4", "cpu5"])

    def test_homogeneous_cores_mean_no_big_little(self):
        """On a chip with identical cores nothing must ever be parked."""
        for i in range(4):
            self.add_cpu("cpu{}".format(i), freq=1200000)
        self.assertEqual(pc._get_big_core_ids(), [])
        self.assertEqual(pc._big_core_ids_cache, [],
                         "the no-big-LITTLE verdict must be cached")

    def test_result_is_cached_per_process(self):
        """The probe onlines parked cores; re-running it on the 5s reconcile
        tick would flap the very cores powersave parked."""
        self.add_cpu("cpu0", freq=408000)
        self.add_cpu("cpu4", freq=2016000)
        first = pc._get_big_core_ids()
        shutil.rmtree(self.cpuroot)  # cache must answer without re-scanning
        self.assertEqual(pc._get_big_core_ids(), first)

    def test_failed_probe_is_not_cached(self):
        """An empty scan (sysfs not ready yet) must retry next call instead
        of permanently deciding there are no big cores."""
        self.assertEqual(pc._get_big_core_ids(), [])
        self.assertIsNone(pc._big_core_ids_cache)

    def test_garbage_freq_file_is_skipped(self):
        self.add_cpu("cpu0", freq="fast")
        self.add_cpu("cpu1", freq=408000)
        self.add_cpu("cpu2", freq=2016000)
        self.assertEqual(pc._get_big_core_ids(), ["cpu2"])

    def test_offline_core_is_probed_and_parked_again(self):
        """A big core already parked when the probe runs must still be
        detected (briefly onlined to read its freq) and then re-parked, or
        detection misses exactly the cores it manages."""
        self.add_cpu("cpu0", freq=408000)
        cpu4 = self.add_cpu("cpu4", freq=2016000, online="0")
        freq_path = os.path.join(cpu4, "cpufreq", "cpuinfo_max_freq")
        state = {"failed_once": False}

        def flaky_open(path, mode="r", *args, **kwargs):
            # Simulate the kernel: freq is unreadable until the core onlines.
            if path == freq_path and not state["failed_once"]:
                state["failed_once"] = True
                raise OSError("core offline")
            return _REAL_OPEN(path, mode, *args, **kwargs)

        with patch("builtins.open", new=flaky_open), \
             patch.object(pc.time, "sleep", lambda s: None):
            self.assertEqual(pc._get_big_core_ids(), ["cpu4"])
        self.assertEqual(self.read_cpu_file("cpu4", "online"), "0",
                         "probe must re-park the core it onlined")

    def test_failed_repark_after_probe_is_swallowed(self):
        """If the core refuses to go back offline after the probe, detection
        must still return the big-core list rather than crash the reconcile
        thread."""
        self.add_cpu("cpu0", freq=408000)
        cpu4 = self.add_cpu("cpu4", freq=2016000, online="0")
        freq_path = os.path.join(cpu4, "cpufreq", "cpuinfo_max_freq")
        online_path = os.path.join(cpu4, "online")
        state = {"freq_failed": False, "online_writes": 0}

        def flaky_open(path, mode="r", *args, **kwargs):
            if path == freq_path and not state["freq_failed"]:
                state["freq_failed"] = True
                raise OSError("core offline")
            if path == online_path and "w" in mode:
                state["online_writes"] += 1
                if state["online_writes"] == 2:  # the re-park write
                    raise OSError("hotplug busy")
            return _REAL_OPEN(path, mode, *args, **kwargs)

        with patch("builtins.open", new=flaky_open), \
             patch.object(pc.time, "sleep", lambda s: None):
            self.assertEqual(pc._get_big_core_ids(), ["cpu4"])

    def test_unreadable_offline_core_is_skipped(self):
        self.add_cpu("cpu0", freq=408000)
        self.add_cpu("cpu1", freq=2016000)
        self.add_cpu("cpu4", online="0")  # freq never readable
        with patch.object(pc.time, "sleep", lambda s: None):
            self.assertEqual(pc._get_big_core_ids(), ["cpu1"])
        self.assertEqual(self.read_cpu_file("cpu4", "online"), "0")


class CpuSchedulingStateTest(CpuTestBase):
    """get_cpu_scheduling_enabled: what the UI toggle shows -- must reflect
    the live core state, not a stale preference."""

    def test_any_big_core_online_reads_as_disabled(self):
        self.add_cpu("cpu4", online="1")
        self.add_cpu("cpu5", online="0")
        with patch.object(pc, "_get_big_core_ids",
                          return_value=["cpu4", "cpu5"]):
            self.assertFalse(pc.get_cpu_scheduling_enabled())

    def test_all_big_cores_offline_reads_as_enabled(self):
        self.add_cpu("cpu4", online="0")
        self.add_cpu("cpu5", online="0")
        with patch.object(pc, "_get_big_core_ids",
                          return_value=["cpu4", "cpu5"]):
            self.assertTrue(pc.get_cpu_scheduling_enabled())

    def test_missing_online_files_read_as_enabled(self):
        with patch.object(pc, "_get_big_core_ids", return_value=["cpu4"]):
            self.assertTrue(pc.get_cpu_scheduling_enabled())

    def test_no_big_little_falls_back_to_governor(self):
        self.add_cpu("cpu0", governor="powersave")
        with patch.object(pc, "_get_big_core_ids", return_value=[]):
            self.assertTrue(pc.get_cpu_scheduling_enabled())
        self.add_cpu("cpu0", governor="schedutil")
        with patch.object(pc, "_get_big_core_ids", return_value=[]):
            self.assertFalse(pc.get_cpu_scheduling_enabled())

    def test_no_governor_file_reads_as_disabled(self):
        with patch.object(pc, "_get_big_core_ids", return_value=[]):
            self.assertFalse(pc.get_cpu_scheduling_enabled())


class CpuSchedulingApplyTest(CpuTestBase):
    """_apply_cpu_scheduling: the actual sysfs writes that park/release
    cores. A silent failure here means the toggle lies."""

    def test_big_little_parks_and_releases_big_cores(self):
        self.add_cpu("cpu4", online="1")
        with patch.object(pc, "_get_big_core_ids", return_value=["cpu4"]):
            pc._apply_cpu_scheduling(True)
            self.assertEqual(self.read_cpu_file("cpu4", "online"), "0")
            pc._apply_cpu_scheduling(False)
            self.assertEqual(self.read_cpu_file("cpu4", "online"), "1")

    def test_missing_core_does_not_abort_the_others(self):
        """cpu9 vanished (hotplug race): cpu4 must still be parked."""
        self.add_cpu("cpu4", online="1")
        with patch.object(pc, "_get_big_core_ids",
                          return_value=["cpu9", "cpu4"]):
            pc._apply_cpu_scheduling(True)
        self.assertEqual(self.read_cpu_file("cpu4", "online"), "0")

    def test_no_big_little_writes_governors(self):
        self.add_cpu("cpu0", governor="schedutil")
        self.add_cpu("cpu1", governor="schedutil")
        with patch.object(pc, "_get_big_core_ids", return_value=[]):
            pc._apply_cpu_scheduling(True)
            self.assertEqual(
                self.read_cpu_file("cpu0", "cpufreq", "scaling_governor"),
                "powersave")
            self.assertEqual(
                self.read_cpu_file("cpu1", "cpufreq", "scaling_governor"),
                "powersave")
            pc._apply_cpu_scheduling(False)
            self.assertEqual(
                self.read_cpu_file("cpu0", "cpufreq", "scaling_governor"),
                "schedutil")


    def test_unwritable_governor_does_not_abort_the_others(self):
        # cpu0's governor node is unopenable (a directory here, EACCES/EINVAL
        # on the device); cpu1 must still be switched.
        cpu0 = os.path.join(self.cpuroot, "cpu0", "cpufreq")
        os.makedirs(os.path.join(cpu0, "scaling_governor"))
        self.add_cpu("cpu1", governor="schedutil")
        with patch.object(pc, "_get_big_core_ids", return_value=[]):
            pc._apply_cpu_scheduling(True)
        self.assertEqual(
            self.read_cpu_file("cpu1", "cpufreq", "scaling_governor"),
            "powersave")


class CpuSchedulingPolicyTest(CpuTestBase):
    """The powersave policy: saved preference x power source x boot guard.
    Wrong resolution either parks cores on mains power (slow for no reason)
    or leaves them running on battery (drains it)."""

    def reconcile(self, dc, saved=True, live=False, guard=False):
        applied = []
        with patch.object(pc, "_cpu_powersave_boot_guard",
                          return_value=guard), \
             patch.object(pc, "get_cpu_scheduling_enabled",
                          return_value=live), \
             patch.object(pc, "_apply_cpu_scheduling", applied.append):
            pc.global_user_config["cpu_scheduling_enabled"] = saved
            result = pc.apply_cpu_scheduling_for_power(dc)
        return result, applied

    def test_saved_default_is_disabled(self):
        self.assertFalse(pc.get_saved_cpu_scheduling_enabled())
        pc.global_user_config["cpu_scheduling_enabled"] = 1
        self.assertTrue(pc.get_saved_cpu_scheduling_enabled())

    def test_on_battery_with_preference_parks_cores(self):
        result, applied = self.reconcile(dc=False)
        self.assertTrue(result)
        self.assertEqual(applied, [True])
        self.assertFalse(pc._last_dc_connected)

    def test_dc_connected_releases_cores(self):
        """Plugging in DC must bring the A72 cores back for full speed even
        though the preference stays enabled."""
        result, applied = self.reconcile(dc=True, live=True)
        self.assertFalse(result)
        self.assertEqual(applied, [False])
        self.assertTrue(pc._last_dc_connected)

    def test_no_write_when_state_already_matches(self):
        """Called on a 5s timer: it must not rewrite sysfs every tick."""
        result, applied = self.reconcile(dc=False, live=True)
        self.assertTrue(result)
        self.assertEqual(applied, [])

    def test_boot_guard_keeps_cores_online(self):
        result, applied = self.reconcile(dc=False, guard=True, live=True)
        self.assertFalse(result)
        self.assertEqual(applied, [False],
                         "the guard must release cores parked too early")

    def test_apply_saved_without_power_info_applies_preference(self):
        """At startup the charger state is unknown; the saved choice must be
        applied as-is so it survives reboot."""
        pc.global_user_config["cpu_scheduling_enabled"] = True
        applied = []
        with patch.object(pc, "_cpu_powersave_boot_guard",
                          return_value=False), \
             patch.object(pc, "_apply_cpu_scheduling", applied.append):
            self.assertTrue(pc.apply_saved_cpu_scheduling())
        self.assertEqual(applied, [True])

    def test_apply_saved_respects_boot_guard(self):
        pc.global_user_config["cpu_scheduling_enabled"] = True
        applied = []
        with patch.object(pc, "_cpu_powersave_boot_guard",
                          return_value=True), \
             patch.object(pc, "_apply_cpu_scheduling", applied.append):
            self.assertTrue(pc.apply_saved_cpu_scheduling())  # pref unchanged
        self.assertEqual(applied, [False], "cores must stay online in boot")

    def test_apply_saved_with_power_info_delegates(self):
        with patch.object(pc, "apply_cpu_scheduling_for_power",
                          return_value="sentinel") as m:
            self.assertEqual(pc.apply_saved_cpu_scheduling(True), "sentinel")
        m.assert_called_once_with(True)

    def test_set_enabled_persists_and_enacts(self):
        """The UI toggle: preference must reach disk (survive reboot) and be
        enacted against the last known power source."""
        with patch.object(pc, "apply_cpu_scheduling_for_power") as m:
            pc.set_cpu_scheduling_enabled(True)
        self.assertTrue(self.saved()["cpu_scheduling_enabled"])
        m.assert_called_once_with(False)  # power source unknown: assume battery

    def test_set_enabled_uses_last_known_power_source(self):
        pc._last_dc_connected = True
        with patch.object(pc, "apply_cpu_scheduling_for_power") as m:
            pc.set_cpu_scheduling_enabled(True)
        m.assert_called_once_with(True)


class ActualFanSpeedTest(ConfigTestBase):
    """get_actual_fan_speed: the RPM readout on the dashboard. Must skip
    dead/garbage hwmon nodes and report 0 (not crash) with no fan."""

    def fan_file(self, name, content):
        path = os.path.join(self.tmpdir.name, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_first_valid_reading_wins(self):
        stopped = self.fan_file("fan_stopped", "0\n")
        garbage = self.fan_file("fan_garbage", "junk\n")
        missing = os.path.join(self.tmpdir.name, "fan_missing")
        good = self.fan_file("fan_good", "3200\n")
        with patch("glob.glob",
                   return_value=[stopped, garbage, missing, good]):
            self.assertEqual(pc.get_actual_fan_speed(), 3200)

    def test_no_hwmon_fan_reads_as_zero(self):
        with patch("glob.glob", return_value=[]):
            self.assertEqual(pc.get_actual_fan_speed(), 0)

    def test_stopped_fan_reads_as_zero(self):
        stopped = self.fan_file("fan_stopped", "0\n")
        with patch("glob.glob", return_value=[stopped]):
            self.assertEqual(pc.get_actual_fan_speed(), 0)


class FanSpeedDirectTest(ConfigTestBase):
    """set_fan_speed_direct: the sysfs write behind every fan change. It must
    find the pcat-pm-fan cooling device among unrelated ones and never raise
    into the API path."""

    THERMAL = "/sys/class/thermal/"

    def run_direct(self, speed, types):
        reads = {}
        for dev, dev_type in types.items():
            reads["{}{}/type".format(self.THERMAL, dev)] = dev_type + "\n"
        writes = []
        fake = _fake_sysfs_open(reads, writes, [self.THERMAL])
        with patch("builtins.open", new=fake):
            pc.set_fan_speed_direct(speed)
        return writes

    def test_writes_only_to_pcat_pm_fan_devices(self):
        writes = self.run_direct(7, {"cooling_device0": "gpu-thermal",
                                     "cooling_device1": "pcat-pm-fan"})
        self.assertEqual(
            writes,
            [(self.THERMAL + "cooling_device1/cur_state", "7")],
            "speed must land on the fan, not the GPU cooling device")

    def test_no_fan_device_is_a_no_op(self):
        """A board without the fan driver loaded must not crash the caller."""
        self.assertEqual(self.run_direct(7, {}), [])

    def test_garbage_speed_is_clamped_before_the_write(self):
        writes = self.run_direct("not a number",
                                 {"cooling_device0": "pcat-pm-fan"})
        self.assertEqual(writes,
                         [(self.THERMAL + "cooling_device0/cur_state", "5")])


class ChargingDetectionTest(ConfigTestBase):
    """is_device_charging: feeds the smart-fan curve. Miscalling it flips the
    fan between the charging (45C) and on-battery (52C) thresholds."""

    BAT = "/sys/class/power_supply/battery/"

    def charging(self, voltage_uv="4100000", current_ua="500000", soc="60",
                 missing=False):
        reads = {} if missing else {
            self.BAT + "voltage_now": voltage_uv + "\n",
            self.BAT + "current_now": current_ua + "\n",
            self.BAT + "capacity": soc + "\n",
        }
        fake = _fake_sysfs_open(reads, [], [self.BAT])
        with patch("builtins.open", new=fake):
            return pc.is_device_charging()

    def test_charging_when_powered_and_not_full(self):
        self.assertTrue(self.charging())

    def test_full_battery_on_dc_is_not_charging(self):
        self.assertFalse(self.charging(soc="96"))

    def test_heavy_discharge_is_not_charging(self):
        self.assertFalse(self.charging(current_ua="-2000000"))

    def test_no_battery_sysfs_assumes_not_charging(self):
        """A board variant without the battery driver must default to the
        conservative on-battery fan curve, not crash."""
        self.assertFalse(self.charging(missing=True))

    def test_garbage_sysfs_assumes_not_charging(self):
        self.assertFalse(self.charging(voltage_uv="garbage"))


class SmsLimitForScreenTest(ConfigTestBase):
    """SMS count shown on the e-ink screen: unbounded values would overflow
    the screen renderer."""

    def test_defaults_to_five(self):
        self.assertEqual(pc.get_sms_limit_for_screen(), 5)

    def test_clamped_to_1_100(self):
        pc.set_sms_limit_for_screen(0)
        self.assertEqual(pc.get_sms_limit_for_screen(), 1)
        pc.set_sms_limit_for_screen(500)
        self.assertEqual(pc.get_sms_limit_for_screen(), 100)

    def test_round_trips_and_persists(self):
        pc.set_sms_limit_for_screen(42)
        self.assertEqual(pc.get_sms_limit_for_screen(), 42)
        self.assertEqual(self.saved()["sms_limit_for_screen"], 42)


class ShowDebugLogsTest(ConfigTestBase):

    def test_defaults_to_hidden(self):
        self.assertFalse(pc.get_show_debug_logs())

    def test_round_trips_as_bool_and_persists(self):
        """Truthy garbage from the API must be stored as a real bool so the
        JSON config stays typed."""
        pc.set_show_debug_logs("yes")
        self.assertIs(pc.get_show_debug_logs(), True)
        self.assertIs(self.saved()["show_debug_logs"], True)
        pc.set_show_debug_logs(False)
        self.assertIs(pc.get_show_debug_logs(), False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
