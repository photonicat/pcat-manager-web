"""Unit tests for lxc_manager's pure config-surgery functions.

These are the functions that rewrite container configs and /etc/lxc/lxc.conf.
They are the risky part of the module — a bad rewrite silently breaks a
container's next boot — and they are pure, so they test without a device.

The module imports device-only peers (nas_jobs, pcat_logger, openwrt), which
are stubbed here so the file can run on a dev machine.
"""

import importlib.util
import os
import sys
import types
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_lxc_manager():
    for name in ("pcat_util", "pcat_util.nas_jobs", "pcat_util.pcat_logger",
                 "pcat_util.openwrt"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    ow = sys.modules["pcat_util.openwrt"]
    ow.uci_get = lambda *a, **k: ""
    ow.uci_show_dict = lambda *a, **k: {}
    for fn in ("uci_set", "uci_add_list", "uci_commit", "uci_delete"):
        setattr(ow, fn, lambda *a, **k: None)
    sys.modules["pcat_util.nas_jobs"].submit = lambda *a, **k: ("job", None)
    logger = sys.modules["pcat_util.pcat_logger"]
    logger.logger = types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None)

    spec = importlib.util.spec_from_file_location(
        "lxc_manager_under_test",
        os.path.join(APP_DIR, "pcat_util", "lxc_manager.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lm = _load_lxc_manager()

BASE_CONFIG = """# Template used to create this container: /usr/share/lxc/templates/lxc-download
# Parameters passed to the template: --dist debian --release trixie --arch arm64
lxc.include = /usr/share/lxc/config/common.conf
lxc.arch = linux64
lxc.rootfs.path = dir:/srv/lxc/ct1/rootfs
lxc.uts.name = ct1

# Network configuration
lxc.net.0.type = veth
lxc.net.0.link = lxcbr0
lxc.net.0.flags = up
lxc.net.0.hwaddr = 00:16:3e:aa:bb:cc
"""


class TestNetworkMode(unittest.TestCase):

    def test_parse_builtin_modes(self):
        self.assertEqual(lm.parse_network_mode(BASE_CONFIG), "nat")
        self.assertEqual(
            lm.parse_network_mode(
                BASE_CONFIG.replace("lxcbr0", "br-lan")), "bridge")

    def test_parse_custom_and_missing(self):
        self.assertEqual(
            lm.parse_network_mode(BASE_CONFIG.replace("lxcbr0", "br-wan1")),
            "custom:br-wan1")
        self.assertEqual(lm.parse_network_mode("lxc.uts.name = x\n"), "")

    def test_rewrite_between_modes_is_stable(self):
        bridged = lm.rewrite_network_mode(BASE_CONFIG, "bridge")
        self.assertEqual(lm.parse_network_mode(bridged), "bridge")
        back = lm.rewrite_network_mode(bridged, "nat")
        self.assertEqual(lm.parse_network_mode(back), "nat")
        # Only the link line changes — hwaddr and rootfs must survive.
        self.assertIn("lxc.net.0.hwaddr = 00:16:3e:aa:bb:cc", back)
        self.assertIn("lxc.rootfs.path = dir:/srv/lxc/ct1/rootfs", back)
        self.assertEqual(back.count("lxc.net.0.link"), 1)

    def test_rewrite_to_custom_bridge(self):
        out = lm.rewrite_network_mode(BASE_CONFIG, "link:br-wan1")
        self.assertIn("lxc.net.0.link = br-wan1", out)
        self.assertEqual(lm.parse_network_mode(out), "custom:br-wan1")

    def test_rewrite_appends_when_no_net_section(self):
        out = lm.rewrite_network_mode("lxc.uts.name = ct1\n", "nat")
        self.assertEqual(lm.parse_network_mode(out), "nat")
        self.assertIn("lxc.net.0.type = veth", out)


class TestIpConfig(unittest.TestCase):

    def test_defaults_are_dhcp_and_auto(self):
        self.assertEqual(lm.parse_ip_config(BASE_CONFIG), {
            "v4_mode": "dhcp", "v4_addr": "", "v4_gw": "",
            "v6_mode": "auto", "v6_addr": "", "v6_gw": ""})

    def test_static_round_trip(self):
        want = {"v4_mode": "manual", "v4_addr": "10.0.3.50/24",
                "v4_gw": "10.0.3.1", "v6_mode": "manual",
                "v6_addr": "fd00::50/64", "v6_gw": "fd00::1"}
        out = lm.rewrite_ip_config(BASE_CONFIG, want)
        self.assertEqual(lm.parse_ip_config(out), want)

    def test_address_lines_follow_the_net_block(self):
        out = lm.rewrite_ip_config(BASE_CONFIG, {
            "v4_mode": "manual", "v4_addr": "10.0.3.50/24", "v4_gw": ""})
        lines = out.splitlines()
        self.assertEqual(lines[lines.index("lxc.net.0.flags = up") + 1],
                         "lxc.net.0.ipv4.address = 10.0.3.50/24")

    def test_switching_back_drops_the_lines(self):
        static = lm.rewrite_ip_config(BASE_CONFIG, {
            "v4_mode": "manual", "v4_addr": "10.0.3.50/24",
            "v4_gw": "10.0.3.1", "v6_mode": "manual",
            "v6_addr": "fd00::50/64", "v6_gw": "fd00::1"})
        back = lm.rewrite_ip_config(static, {"v4_mode": "dhcp",
                                             "v6_mode": "auto"})
        self.assertNotIn("ipv4.address", back)
        self.assertNotIn("ipv6.gateway", back)
        self.assertEqual(lm.parse_ip_config(back)["v4_mode"], "dhcp")
        self.assertIn("lxc.net.0.link = lxcbr0", back)

    def test_manual_without_address_writes_nothing(self):
        out = lm.rewrite_ip_config(BASE_CONFIG,
                                   {"v4_mode": "manual", "v4_addr": ""})
        self.assertNotIn("ipv4.address", out)

    def test_set_ip_config_rejects_bad_input(self):
        self.assertEqual(lm.set_ip_config("bad name!", {})["code"],
                         "bad_name")
        for addr in ("999.1.1.1/24", "10.0.3.50", "not-an-ip/24"):
            res = lm.set_ip_config("ct1", {"v4_mode": "manual",
                                           "v4_addr": addr})
            self.assertEqual(res["code"], "bad_address", addr)
        res = lm.set_ip_config("ct1", {"v6_mode": "manual",
                                       "v6_addr": "fd00::50"})
        self.assertEqual(res["code"], "bad_address")


class TestPathRewrites(unittest.TestCase):

    def test_rootfs_paths_move(self):
        out = lm.rewrite_rootfs_paths(BASE_CONFIG, "/srv/lxc/ct1",
                                      "/mnt/nvme0n1p1/lxc-storage/ct1")
        self.assertIn(
            "lxc.rootfs.path = dir:/mnt/nvme0n1p1/lxc-storage/ct1/rootfs", out)
        self.assertNotIn("/srv/lxc/ct1/rootfs", out)

    def test_clone_config_renames_and_remacs(self):
        out = lm.rewrite_clone_config(BASE_CONFIG, "/srv/lxc", "ct1", "ct2")
        self.assertIn("lxc.uts.name = ct2", out)
        self.assertIn("lxc.rootfs.path = dir:/srv/lxc/ct2/rootfs", out)
        self.assertNotIn("00:16:3e:aa:bb:cc", out)      # fresh MAC
        self.assertEqual(
            out, lm.rewrite_clone_config(BASE_CONFIG, "/srv/lxc", "ct1",
                                         "ct2"))       # deterministic

    def test_lxcpath_parse_and_rewrite(self):
        conf = "# comment\nlxc.lxcpath = /srv/lxc\n"
        self.assertEqual(lm.parse_lxcpath(conf), "/srv/lxc")
        out = lm.rewrite_lxcpath(conf, "/mnt/nvme0n1p1/lxc")
        self.assertEqual(lm.parse_lxcpath(out), "/mnt/nvme0n1p1/lxc")
        self.assertEqual(lm.parse_lxcpath(""), lm.DEFAULT_LXCPATH)


class TestLimits(unittest.TestCase):

    def test_parse_unlimited_by_default(self):
        self.assertEqual(lm.parse_config_limits(BASE_CONFIG),
                         {"memory_mb": 0, "cpu_cores": 0.0})

    def test_round_trip(self):
        out = lm.rewrite_limits(BASE_CONFIG, 512, 1.5)
        got = lm.parse_config_limits(out)
        self.assertEqual(got["memory_mb"], 512)
        self.assertEqual(got["cpu_cores"], 1.5)
        cleared = lm.rewrite_limits(out, 0, 0)
        self.assertEqual(lm.parse_config_limits(cleared),
                         {"memory_mb": 0, "cpu_cores": 0.0})


class TestNameValidation(unittest.TestCase):

    def test_container_names(self):
        for good in ("ct1", "web-01", "a.b_c"):
            self.assertTrue(lm.NAME_RE.match(good), good)
        for bad in ("", "-leading", "has space", "semi;colon", "a" * 65):
            self.assertFalse(lm.NAME_RE.match(bad), bad)

    def test_network_names(self):
        for good in ("wan1", "lab", "n0"):
            self.assertTrue(lm.NET_NAME_RE.match(good), good)
        for bad in ("", "1st", "Upper", "has-dash", "toolongname11"):
            self.assertFalse(lm.NET_NAME_RE.match(bad), bad)

    def test_create_network_rejects_bad_name_and_addressing(self):
        self.assertEqual(lm.create_network("Bad Name")["code"], "bad_name")
        self.assertEqual(
            lm.create_network("ok", addressing="nat", subnet="")["code"],
            "bad_subnet")
        self.assertEqual(lm.delete_network("Bad Name")["code"], "bad_name")


class TestDefaultConfig(unittest.TestCase):
    """The create path writes the container config itself now (it no longer
    gets one from lxc-create), so the placeholder MAC in the shipped
    default.conf must actually get substituted."""

    SHIPPED = ("lxc.net.0.type = veth\n"
               "lxc.net.0.link = lxcbr0\n"
               "lxc.net.0.flags = up\n"
               "lxc.net.0.hwaddr = 00:16:3e:xx:xx:xx\n")

    def _config(self, name, shipped=None):
        orig_read, orig_path = lm._read_file, lm.lxcpath
        lm._read_file = lambda p: (
            self.SHIPPED if shipped is None else shipped)
        lm.lxcpath = lambda: "/srv/lxc"
        try:
            return lm._default_config_for(name)
        finally:
            lm._read_file, lm.lxcpath = orig_read, orig_path

    def test_placeholder_mac_is_replaced(self):
        out = self._config("ct233")
        self.assertNotIn("xx:xx:xx", out)
        self.assertIn("lxc.net.0.hwaddr = {}".format(lm._mac_for("ct233")),
                      out)

    def test_rootfs_and_uts_are_set(self):
        out = self._config("ct233")
        self.assertIn("lxc.rootfs.path = dir:/srv/lxc/ct233/rootfs", out)
        self.assertIn("lxc.uts.name = ct233", out)
        self.assertEqual(out.count("lxc.rootfs.path"), 1)

    def test_missing_default_conf_still_yields_a_usable_config(self):
        out = self._config("ct1", shipped="")
        for needle in ("lxc.net.0.type = veth", "lxc.net.0.link = lxcbr0",
                       "lxc.net.0.hwaddr = ", "lxc.rootfs.path = "):
            self.assertIn(needle, out)

    def test_macs_differ_per_container_and_are_stable(self):
        self.assertNotEqual(lm._mac_for("ct232"), lm._mac_for("ct233"))
        self.assertEqual(lm._mac_for("ct233"), lm._mac_for("ct233"))
        self.assertTrue(lm._mac_for("ct233").startswith("00:16:3e:"))


class TestImageResolve(unittest.TestCase):

    INDEX = ("alpine;3.21;arm64;default;20260810_13:00;/images/a/old/\n"
             "alpine;3.21;arm64;default;20260811_13:00;/images/a/new/\n"
             "alpine;3.21;amd64;default;20260811_13:00;/images/a/amd64/\n"
             "debian;trixie;arm64;default;20260811_13:00;/images/d/\n")

    def _resolve(self, *args, index=None):
        orig = lm._run
        lm._run = lambda argv, **k: (
            0, self.INDEX if index is None else index, "")
        try:
            return lm._resolve_image(*args)
        finally:
            lm._run = orig

    def test_picks_newest_build_for_exact_match(self):
        self.assertEqual(
            self._resolve("srv", "alpine", "3.21", "arm64"), "/images/a/new/")

    def test_arch_is_not_confused(self):
        self.assertEqual(
            self._resolve("srv", "alpine", "3.21", "amd64"),
            "/images/a/amd64/")

    def test_unknown_image_returns_none(self):
        self.assertIsNone(self._resolve("srv", "alpine", "9.99", "arm64"))

    def test_index_fetch_failure_returns_none(self):
        orig = lm._run
        lm._run = lambda argv, **k: (1, "", "network down")
        try:
            self.assertIsNone(
                lm._resolve_image("srv", "alpine", "3.21", "arm64"))
        finally:
            lm._run = orig


class TestInternalDirsAreNotContainers(unittest.TestCase):
    """Our own dot-directories under lxcpath (image pool, download scratch)
    were listed as BROKEN containers whose every action then failed name
    validation — NAME_RE is the filter that keeps them out."""

    def test_dot_directories_never_match_a_container_name(self):
        for internal in (".pcat-image-pool", ".pcat-dl-cache", ".hidden"):
            self.assertFalse(lm.NAME_RE.match(internal), internal)

    def test_real_container_names_still_match(self):
        for good in ("ccc32", "ct233", "web-01"):
            self.assertTrue(lm.NAME_RE.match(good), good)


class TestVmidSelection(unittest.TestCase):

    def _with_sections(self, secs):
        orig = lm._uci_container_sections
        lm._uci_container_sections = lambda: secs
        self.addCleanup(lambda: setattr(lm, "_uci_container_sections", orig))

    def test_next_free_starts_at_100(self):
        self._with_sections({})
        self.assertEqual(lm.next_free_vmid(), 100)

    def test_next_free_skips_used(self):
        self._with_sections({
            "a": {"name": "x", "vmid": "100"},
            "b": {"name": "y", "vmid": "101"},
            "c": {"name": "z", "vmid": "103"}})
        self.assertEqual(lm.next_free_vmid(), 102)

    def test_in_use_detection(self):
        self._with_sections({"a": {"name": "x", "vmid": "103"}})
        self.assertTrue(lm.vmid_in_use(103))
        self.assertTrue(lm.vmid_in_use("103"))
        self.assertFalse(lm.vmid_in_use(104))
        self.assertFalse(lm.vmid_in_use("not-a-number"))

    def test_create_rejects_out_of_range_and_taken_ids(self):
        self._with_sections({"a": {"name": "x", "vmid": "103"}})
        for bad in (99, 0, -5, "abc"):
            self.assertEqual(
                lm.create_container("ok", "alpine", "3.21", vmid=bad)[1]["code"],
                "bad_vmid", bad)
        err = lm.create_container("ok", "alpine", "3.21", vmid=103)[1]
        self.assertEqual(err["code"], "vmid_in_use")

    def test_blank_vmid_is_not_an_error(self):
        # "" / None mean "pick one for me" and must not trip validation.
        self._with_sections({})
        for blank in ("", None):
            err = lm.create_container("Bad Name!", "alpine", "3.21",
                                      vmid=blank)[1]
            self.assertEqual(err["code"], "bad_name")   # failed on name, not vmid


class TestBackupNaming(unittest.TestCase):
    """The backup filename is also the record: restore parses the container
    name and timestamp back out of it, so the round trip has to hold."""

    def test_round_trip(self):
        fn = lm._backup_name("ccc32", "20260813_004500")
        self.assertEqual(fn, "vzdump-lxc-ccc32-20260813_004500.tar.gz")
        m = lm._BACKUP_RE.match(fn)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "ccc32")
        self.assertEqual(m.group(2), "20260813_004500")

    def test_names_with_dots_and_dashes_round_trip(self):
        for name in ("web-01", "a.b_c", "ct233"):
            m = lm._BACKUP_RE.match(lm._backup_name(name, "20260813_004500"))
            self.assertIsNotNone(m, name)
            self.assertEqual(m.group(1), name)

    def test_unrelated_files_are_not_backups(self):
        for junk in ("rootfs.tar.xz", "vzdump-lxc-.tar.gz", "notes.txt",
                     "vzdump-lxc-ct1-bogus.tar.gz",
                     "vzdump-lxc-ct1-20260813_004500.tar.gz.part"):
            self.assertIsNone(lm._BACKUP_RE.match(junk), junk)

    def test_backup_delete_rejects_traversal_and_junk(self):
        self.assertEqual(lm.backup_delete("../../etc/passwd")["code"], "bad_name")
        self.assertEqual(lm.backup_delete("")["code"], "bad_name")
        self.assertEqual(lm.backup_delete("notes.txt")["code"], "bad_name")

    def test_backup_rejects_bad_stamp(self):
        err = lm.backup_container("ct1", "not-a-stamp")[1]
        self.assertEqual(err["code"], "bad_name")

    def test_restore_rejects_bad_archive_name(self):
        for junk in ("", "notes.txt", "../x.tar.gz"):
            self.assertEqual(lm.restore_backup(junk)[1]["code"], "bad_name")

    def test_backup_dir_is_not_a_container(self):
        self.assertFalse(lm.NAME_RE.match(lm.BACKUP_DIRNAME))
        self.assertFalse(lm.NAME_RE.match(".pcat-restore-ct1"))


class TestAriaProgressParsing(unittest.TestCase):
    """aria2's summary line is the only source of live speed, so parsing it
    is load-bearing for the UI's rate display."""

    LINE = "[#77b7d1 224KiB/3.2MiB(6%) CN:1 DL:77KiB ETA:39s]"

    def test_speed_is_parsed_in_bytes_per_second(self):
        m = lm._ARIA_DL_RE.search(self.LINE)
        self.assertIsNotNone(m)
        self.assertEqual(lm._parse_unit(m.group(1), m.group(2)), 77 * 1024)

    def test_megabyte_rate(self):
        m = lm._ARIA_DL_RE.search("[#a 1B/2B(0%) CN:4 DL:1.5MiB ETA:2s]")
        self.assertEqual(lm._parse_unit(m.group(1), m.group(2)),
                         int(1.5 * 1024 * 1024))

    def test_eta_forms(self):
        self.assertEqual(lm._parse_aria_eta(self.LINE), 39)
        self.assertEqual(lm._parse_aria_eta("... ETA:1m17s]"), 77)
        self.assertEqual(lm._parse_aria_eta("... ETA:1h2m3s]"), 3723)
        self.assertIsNone(lm._parse_aria_eta("[#a 0B/0B CN:1 DL:0B]"))

    def test_line_without_speed_is_not_matched(self):
        self.assertIsNone(lm._ARIA_DL_RE.search("NOTICE Downloading 1 item(s)"))

    def test_transferred_bytes_come_from_the_line_not_the_file(self):
        # aria2 preallocates, so the on-disk size is the FINAL size from the
        # first second; only this parse reflects what actually transferred.
        m = lm._ARIA_DONE_RE.search("[#d5573c 1.4MiB/119MiB(1%) CN:4 DL:23KiB]")
        self.assertIsNotNone(m)
        self.assertEqual(lm._parse_unit(m.group(1), m.group(2)),
                         int(1.4 * 1024 * 1024))
        self.assertEqual(lm._parse_unit(m.group(3), m.group(4)),
                         119 * 1024 * 1024)

    def test_done_parse_handles_bytes_and_zero(self):
        m = lm._ARIA_DONE_RE.search("[#a 0B/0B CN:1 DL:0B]")
        self.assertIsNotNone(m)
        self.assertEqual(lm._parse_unit(m.group(1), m.group(2)), 0)

    def test_rate_formatting(self):
        self.assertEqual(lm._fmt_rate(0), "0 B/s")
        self.assertEqual(lm._fmt_rate(2048), "2 KiB/s")
        self.assertEqual(lm._fmt_rate(3 * 1024 * 1024), "3.0 MiB/s")


class TestPlaceholderSubstitution(unittest.TestCase):
    """Images ship /etc/hostname containing the literal LXC_NAME; without
    this pass every container calls itself "LXC_NAME"."""

    def _rootfs(self, files):
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "etc"), exist_ok=True)
        for rel, body in files.items():
            with open(os.path.join(d, rel), "w") as f:
                f.write(body)
        return d

    def test_hostname_is_replaced(self):
        d = self._rootfs({"etc/hostname": "LXC_NAME\n"})
        lm._substitute_rootfs_placeholders(d, "ccc32")
        with open(os.path.join(d, "etc/hostname")) as f:
            self.assertEqual(f.read().strip(), "ccc32")

    def test_hosts_entries_replaced_everywhere(self):
        d = self._rootfs({
            "etc/hosts": "127.0.0.1 localhost\n127.0.1.1 LXC_NAME LXC_NAME\n"})
        lm._substitute_rootfs_placeholders(d, "web1")
        with open(os.path.join(d, "etc/hosts")) as f:
            body = f.read()
        self.assertNotIn("LXC_NAME", body)
        self.assertEqual(body.count("web1"), 2)

    def test_files_without_placeholder_are_untouched(self):
        d = self._rootfs({"etc/hostname": "already-set\n"})
        lm._substitute_rootfs_placeholders(d, "ccc32")
        with open(os.path.join(d, "etc/hostname")) as f:
            self.assertEqual(f.read().strip(), "already-set")

    def test_missing_files_are_not_an_error(self):
        import tempfile
        lm._substitute_rootfs_placeholders(tempfile.mkdtemp(), "ccc32")


class TestImagePool(unittest.TestCase):

    PATH = "/images/ubuntu/noble/arm64/default/20260811_13:00/"

    def test_key_includes_build_stamp(self):
        key = lm._pool_key("ubuntu", "noble", "arm64", self.PATH)
        self.assertEqual(key, "ubuntu_noble_arm64_20260811_1300.tar.xz")

    def test_key_is_filesystem_safe_and_distinct_per_build(self):
        newer = lm._pool_key("ubuntu", "noble", "arm64",
                             "/images/ubuntu/noble/arm64/default/20260812_13:00/")
        self.assertNotEqual(lm._pool_key("ubuntu", "noble", "arm64", self.PATH),
                            newer)
        for key in (newer, lm._pool_key("a/b", "c d", "e:f", "")):
            self.assertNotIn("/", key)
            self.assertNotIn(":", key)

    def test_arch_and_release_separate_entries(self):
        a = lm._pool_key("ubuntu", "noble", "arm64", self.PATH)
        b = lm._pool_key("ubuntu", "noble", "amd64", self.PATH)
        c = lm._pool_key("ubuntu", "jammy", "arm64", self.PATH)
        self.assertEqual(len({a, b, c}), 3)

    def test_hit_requires_verified_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tar = os.path.join(d, "img.tar.xz")
            with open(tar, "w") as f:
                f.write("data")
            self.assertFalse(lm._pool_hit(tar))      # no .ok yet
            lm._pool_mark_ok(tar)
            self.assertTrue(lm._pool_hit(tar))
            # an aria2 control file means the transfer never finished
            with open(tar + ".aria2", "w") as f:
                f.write("")
            self.assertFalse(lm._pool_hit(tar))

    def test_hit_is_false_for_missing_file(self):
        self.assertFalse(lm._pool_hit("/nonexistent/img.tar.xz"))

    def test_pool_delete_rejects_traversal(self):
        self.assertEqual(lm.pool_delete("../../etc/passwd")["code"], "bad_name")
        self.assertEqual(lm.pool_delete("")["code"], "bad_name")

    def test_pool_dir_is_excluded_from_container_listing(self):
        self.assertEqual(lm.POOL_DIRNAME, ".pcat-image-pool")


class TestUpgradeRecovery(unittest.TestCase):
    """Rebuilding lxcpath after a flash erased it.

    `sysupgrade -c` keeps only /etc, so /srv/lxc and every symlink in it is
    gone while the containers those links pointed at are still sitting on
    their disk. These cover the reconstruction: what gets relinked, what gets
    adopted, and — the part that would be dangerous to get wrong — what must
    never be touched.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # realpath: on macOS /var is a symlink to /private/var, and the code
        # under test resolves symlinks, so the fixture has to be resolved too.
        root = os.path.realpath(self.tmp.name)
        self.base = os.path.join(root, "srv", "lxc")
        self.vol = os.path.join(root, "mnt", "disk")
        self.store = os.path.join(self.vol, "lxc-storage")
        os.makedirs(self.base)
        os.makedirs(self.store)

        # Stub the device surface: lxcpath, the mount table, and the uci
        # bookkeeping that /etc/config/pcat_lxc would hold.
        self.sections = {}
        self._patch(lm, "lxcpath", lambda: self.base)
        self._patch(lm, "_uci_container_sections", lambda: self.sections)
        self._patch(lm, "_autostart_map", lambda: {
            o["name"]: o.get("autostart") == "1"
            for o in self.sections.values()})
        self._patch(lm, "_ensure_uci_file", lambda: True)
        self._patch(lm, "_read_file", self._fake_read_file)
        self._patch(lm, "fs_can_host_containers", lambda p: True)
        self._patch(lm, "MOUNT_ROOT", os.path.join(root, "mnt") + os.sep)
        self._patch(lm, "on_overlay",
                    lambda p: p.startswith(os.path.join(root, "srv")))
        self._patch(lm, "vmid_in_use", lambda v: False)
        self.vmid_calls = []
        self._patch(lm, "ensure_vmids",
                    lambda names, prefer=None: self.vmid_calls.append(
                        (sorted(names), prefer or {})))
        self.autostart_calls = []
        self._patch(lm, "set_autostart",
                    lambda n, e, prune=False:
                    self.autostart_calls.append((n, e)))
        self.uci_writes = []
        self._patch(lm, "uci_set", lambda s: self.uci_writes.append(s))
        self._patch(lm, "uci_delete", lambda s: self.uci_writes.append("-" + s))
        self._patch(lm, "uci_commit", lambda *a, **k: None)

    def _patch(self, mod, name, value):
        old = getattr(mod, name)
        setattr(mod, name, value)
        self.addCleanup(setattr, mod, name, old)

    def _fake_read_file(self, path):
        if path == "/proc/mounts":
            return "/dev/sda1 {} ext4 rw 0 0\n".format(self.vol)
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return ""

    def _make_container(self, parent, name):
        cdir = os.path.join(parent, name)
        os.makedirs(os.path.join(cdir, "rootfs"))
        with open(os.path.join(cdir, "config"), "w") as f:
            f.write(BASE_CONFIG)
        return cdir

    def test_relinks_from_the_uci_record(self):
        cdir = self._make_container(self.store, "ct1")
        self.sections = {"@container[0]": {"name": "ct1", "vmid": "100",
                                           "storage": cdir}}
        rec = lm.recover_containers()
        self.assertEqual(rec["relinked"], ["ct1"])
        self.assertEqual(os.path.realpath(os.path.join(self.base, "ct1")), cdir)

    def test_adopts_a_container_with_no_record_at_all(self):
        # sysupgrade -n: even /etc/config/pcat_lxc is gone, so the only thing
        # left saying this container exists is the disk itself.
        cdir = self._make_container(self.store, "ct1")
        lm._write_manifest(self.store,
                           {"ct1": {"vmid": 1111, "autostart": True}})
        rec = lm.recover_containers()
        self.assertEqual(rec["adopted"], ["ct1"])
        self.assertEqual(os.path.realpath(os.path.join(self.base, "ct1")), cdir)
        # the ct id and autostart come back with it
        self.assertEqual(self.vmid_calls[0][1], {"ct1": 1111})
        self.assertIn(("ct1", True), self.autostart_calls)

    def test_adoption_never_steals_an_id_in_use(self):
        self._make_container(self.store, "ct1")
        lm._write_manifest(self.store, {"ct1": {"vmid": 1111}})
        self._patch(lm, "vmid_in_use", lambda v: int(v) == 1111)
        lm.recover_containers()
        # no preference passed -> ensure_vmids hands out the lowest free id
        self.assertEqual(self.vmid_calls[0][1], {})

    def test_never_replaces_what_is_already_in_lxcpath(self):
        live = self._make_container(self.base, "ct1")
        stale = self._make_container(self.store, "ct1")
        rec = lm.recover_containers()
        self.assertEqual(rec["adopted"], [])
        self.assertEqual(os.path.realpath(os.path.join(self.base, "ct1")), live)
        self.assertTrue(os.path.isdir(stale))    # left alone, not deleted

    def test_reports_a_container_whose_volume_is_absent(self):
        self.sections = {"@container[0]": {
            "name": "ct1", "vmid": "100",
            "storage": os.path.join(os.path.realpath(self.tmp.name), "mnt", "gone", "ct1")}}
        rec = lm.recover_containers()
        self.assertEqual(rec["missing"], ["ct1"])
        self.assertFalse(os.path.lexists(os.path.join(self.base, "ct1")))

    def test_recovery_is_idempotent(self):
        cdir = self._make_container(self.store, "ct1")
        self.sections = {"@container[0]": {"name": "ct1", "vmid": "100",
                                           "storage": cdir}}
        lm.recover_containers()
        again = lm.recover_containers()
        self.assertEqual(again["relinked"], [])
        self.assertEqual(again["conflicts"], [])

    def test_sync_records_writes_uci_and_manifest(self):
        cdir = self._make_container(self.store, "ct1")
        os.symlink(cdir, os.path.join(self.base, "ct1"))
        self.sections = {"@container[0]": {"name": "ct1", "vmid": "101",
                                           "autostart": "1"}}
        lm.sync_records()
        self.assertIn("pcat_lxc.@container[0].storage=" + cdir, self.uci_writes)
        self.assertEqual(lm._read_manifest(self.store),
                         {"ct1": {"vmid": 101, "autostart": True}})

    def test_sync_records_clears_a_stale_record_after_a_move_home(self):
        # container moved back into lxcpath: the old off-path record must go,
        # or the next boot relinks it to a directory that no longer exists.
        self._make_container(self.base, "ct1")
        self.sections = {"@container[0]": {"name": "ct1", "vmid": "101",
                                           "storage": "/mnt/old/ct1"}}
        lm.sync_records()
        self.assertIn("-pcat_lxc.@container[0].storage", self.uci_writes)

    def test_no_manifest_is_written_on_the_overlay(self):
        # It would be erased by the same flash it is meant to survive.
        self._make_container(self.base, "ct1")
        self.sections = {"@container[0]": {"name": "ct1", "vmid": "101"}}
        lm.sync_records()
        self.assertFalse(
            os.path.exists(os.path.join(self.base, lm.MANIFEST_NAME)))

    def test_manifest_survives_a_damaged_file(self):
        with open(os.path.join(self.store, lm.MANIFEST_NAME), "w") as f:
            f.write("{not json")
        self.assertEqual(lm._read_manifest(self.store), {})

    def test_dot_directories_are_never_adopted(self):
        # the image pool and backup dirs live next to the containers
        self._make_container(self.store, ".pcat-image-pool")
        rec = lm.recover_containers()
        self.assertEqual(rec["adopted"], [])

    def test_backups_are_written_off_the_overlay(self):
        # A backup on the overlay is erased by the very upgrade it exists to
        # protect against, so it must not be written there when a durable
        # volume is available.
        self._patch(lm, "default_storage", lambda: os.path.join(self.vol, "lxc"))
        self.assertEqual(lm.backup_dir(),
                         os.path.join(self.vol, "lxc", lm.BACKUP_DIRNAME))

    def test_backups_stay_in_lxcpath_when_it_is_already_durable(self):
        self._patch(lm, "on_overlay", lambda p: False)
        self.assertEqual(lm.backup_dir(),
                         os.path.join(self.base, lm.BACKUP_DIRNAME))

    def test_backups_fall_back_when_there_is_no_durable_volume(self):
        self._patch(lm, "default_storage", lambda: None)
        self.assertEqual(lm.backup_dir(),
                         os.path.join(self.base, lm.BACKUP_DIRNAME))

    def test_old_backups_still_list_after_the_location_moved(self):
        self._patch(lm, "default_storage", lambda: os.path.join(self.vol, "lxc"))
        legacy = os.path.join(self.base, lm.BACKUP_DIRNAME)
        os.makedirs(legacy)
        old = "vzdump-lxc-ct1-20260101_010101.tar.gz"
        with open(os.path.join(legacy, old), "w") as f:
            f.write("x")
        self.assertEqual([r["file"] for r in lm.backup_list()], [old])
        self.assertEqual(lm._find_backup(old), os.path.join(legacy, old))


if __name__ == "__main__":
    unittest.main()
