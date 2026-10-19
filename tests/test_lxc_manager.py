#!/usr/bin/env python3
"""Unit tests for the LXC manager (pcat_util/lxc_manager).

Pins the pure config-rewrite helpers (lxcpath parse/rewrite, rootfs-path
rewrite on storage moves), the strict name/target validation every
shell-adjacent path depends on, the uci autostart schema, the migration
target probe parsing, and the fix_bridge UCI writes.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_lxc_manager`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

# pcat_logger opens its log file at import time, which fails in sandboxed
# test runs — mock it (and keep the patch active for the module lifetime)
# before anything imports it, same idiom as test_nas_samba_uci.
_logger_patch = patch.dict('sys.modules', {
    'pcat_logger': MagicMock(),
    'pcat_util.pcat_logger': MagicMock(),
})
_logger_patch.start()

from pcat_util import lxc_manager as lm


class LxcpathConfTests(unittest.TestCase):

    def test_parse_default_when_absent(self):
        self.assertEqual(lm.parse_lxcpath(""), "/srv/lxc")
        self.assertEqual(lm.parse_lxcpath("# comment only\n"), "/srv/lxc")

    def test_parse_value(self):
        self.assertEqual(
            lm.parse_lxcpath("lxc.lxcpath = /mnt/disk/lxc\n"),
            "/mnt/disk/lxc")
        self.assertEqual(
            lm.parse_lxcpath("  lxc.lxcpath=/a/b  \n"), "/a/b")

    def test_rewrite_replaces_in_place(self):
        out = lm.rewrite_lxcpath(
            "# hdr\nlxc.lxcpath = /srv/lxc\nother = x\n", "/mnt/new")
        self.assertIn("lxc.lxcpath = /mnt/new", out)
        self.assertNotIn("/srv/lxc", out)
        self.assertIn("other = x", out)

    def test_rewrite_appends_when_missing(self):
        out = lm.rewrite_lxcpath("# empty\n", "/mnt/new")
        self.assertIn("lxc.lxcpath = /mnt/new", out)

    def test_rootfs_rewrite_only_touches_base_prefix(self):
        cfg = ("lxc.rootfs.path = dir:/srv/lxc/jjj/rootfs\n"
               "lxc.mount.entry = /srv/lxc/jjj/shared shared none bind\n"
               "# unrelated /srv/lxcfoo stays\n"
               "lxc.hook.pre-start = /srv/lxcfoo/hook\n")
        out = lm.rewrite_rootfs_paths(cfg, "/srv/lxc", "/mnt/big/lxc")
        self.assertIn("dir:/mnt/big/lxc/jjj/rootfs", out)
        self.assertIn("/mnt/big/lxc/jjj/shared", out)
        # /srv/lxcfoo/ must NOT be rewritten (prefix ends with /)
        self.assertIn("/srv/lxcfoo/hook", out)


class ValidationTests(unittest.TestCase):

    def test_container_names(self):
        good = ["jjj", "web-1", "a", "CT_2", "d.e-f", "x" * 64]
        bad = ["", "-lead", ".lead", "has space", "a/b", "a;b", "x" * 65,
               "$(boom)", "a`b`"]
        for n in good:
            self.assertTrue(lm.NAME_RE.match(n), n)
        for n in bad:
            self.assertFalse(lm.NAME_RE.match(n), n)

    def test_bad_names_rejected_before_any_subprocess(self):
        with patch.object(lm, '_run') as run, \
                patch.object(lm, '_ubus') as ubus:
            self.assertEqual(
                lm.container_action("start", "bad name")["code"], "bad_name")
            self.assertEqual(
                lm.container_action("nope", "jjj")["code"], "bad_action")
            run.assert_not_called()
            ubus.assert_not_called()

    def test_target_validation(self):
        self.assertIsNone(lm._valid_target("host;rm", 22, "root"))
        self.assertIsNone(lm._valid_target("h.example.com", "abc", "root"))
        self.assertIsNone(lm._valid_target("h.example.com", 0, "root"))
        self.assertIsNone(lm._valid_target("h", 22, "Root User"))
        self.assertEqual(
            lm._valid_target("172.16.10.188", "22", "root"),
            ("172.16.10.188", 22, "root"))

    def test_migrate_arg_gates(self):
        _, err = lm.migrate("bad name", "h", 22, "root")
        self.assertEqual(err["code"], "bad_name")
        _, err = lm.migrate("jjj", "h;x", 22, "root")
        self.assertEqual(err["code"], "bad_target")
        with patch.object(lm, 'ssh_key_ready', return_value=True), \
                patch.object(lm, 'lxcpath', return_value="/srv/lxc"), \
                patch.object(os.path, 'isdir', return_value=True):
            _, err = lm.migrate("jjj", "h", 22, "root", mode="teleport")
            self.assertEqual(err["code"], "bad_mode")
            _, err = lm.migrate("jjj", "h", 22, "root",
                                remote_path="../etc")
            self.assertEqual(err["code"], "bad_path")

    def test_migrate_requires_key_and_container(self):
        with patch.object(lm, 'ssh_key_ready', return_value=False):
            _, err = lm.migrate("jjj", "h", 22, "root")
            self.assertEqual(err["code"], "no_key")
        with patch.object(lm, 'ssh_key_ready', return_value=True), \
                patch.object(lm, 'lxcpath', return_value="/srv/lxc"), \
                patch.object(os.path, 'isdir', return_value=False):
            _, err = lm.migrate("ghost", "h", 22, "root")
            self.assertEqual(err["code"], "not_found")

    def test_create_gates(self):
        _, err = lm.create_container("bad name", "debian", "trixie")
        self.assertEqual(err["code"], "bad_name")
        _, err = lm.create_container("ok", "debian; rm", "trixie")
        self.assertEqual(err["code"], "bad_template")
        _, err = lm.create_container("ok", "debian", "trixie",
                                     arch="arm64; x")
        self.assertEqual(err["code"], "bad_template")


class CloneConfigTests(unittest.TestCase):

    CFG = ("# Template used: --dist debian --release trixie\n"
           "lxc.rootfs.path = dir:/srv/lxc/jjj/rootfs\n"
           "lxc.uts.name = jjj\n"
           "lxc.net.0.type = veth\n"
           "lxc.net.0.hwaddr = 00:16:3e:dc:37:6a\n")

    def test_clone_rewrite(self):
        out = lm.rewrite_clone_config(self.CFG, "/srv/lxc", "jjj", "jjj2")
        self.assertIn("dir:/srv/lxc/jjj2/rootfs", out)
        self.assertIn("lxc.uts.name = jjj2", out)
        self.assertNotIn("00:16:3e:dc:37:6a", out)
        self.assertIn("lxc.net.0.hwaddr = 00:16:3e:", out)
        # Deterministic MAC: same dst name, same MAC.
        again = lm.rewrite_clone_config(self.CFG, "/srv/lxc", "jjj", "jjj2")
        self.assertEqual(out, again)
        # Different name, different MAC.
        other = lm.rewrite_clone_config(self.CFG, "/srv/lxc", "jjj", "web")
        mac = [l for l in out.splitlines() if "hwaddr" in l]
        mac_other = [l for l in other.splitlines() if "hwaddr" in l]
        self.assertNotEqual(mac, mac_other)

    def test_clone_gates(self):
        _, err = lm.clone_container("bad name", "ok")
        self.assertEqual(err["code"], "bad_name")
        _, err = lm.clone_container("same", "same")
        self.assertEqual(err["code"], "bad_name")
        with patch.object(lm, 'lxcpath', return_value="/srv/lxc"), \
                patch.object(os.path, 'isdir', return_value=False):
            _, err = lm.clone_container("ghost", "new")
            self.assertEqual(err["code"], "not_found")
        with patch.object(lm, 'lxcpath', return_value="/srv/lxc"), \
                patch.object(os.path, 'isdir', return_value=True), \
                patch.object(os.path, 'exists', return_value=True):
            _, err = lm.clone_container("jjj", "jjj2")
            self.assertEqual(err["code"], "exists")
        with patch.object(lm, 'lxcpath', return_value="/srv/lxc"), \
                patch.object(os.path, 'isdir', return_value=True), \
                patch.object(os.path, 'exists', return_value=False), \
                patch.object(lm, 'container_states',
                             return_value={"jjj": "RUNNING"}):
            _, err = lm.clone_container("jjj", "jjj2")
            self.assertEqual(err["code"], "running")


class NetworkModeTests(unittest.TestCase):

    CFG = ("lxc.rootfs.path = dir:/srv/lxc/jjj/rootfs\n"
           "lxc.net.0.type = veth\n"
           "lxc.net.0.link = lxcbr0\n"
           "lxc.net.0.flags = up\n"
           "lxc.net.0.hwaddr = 00:16:3e:dc:37:6a\n")

    def test_parse_modes(self):
        self.assertEqual(lm.parse_network_mode(self.CFG), "nat")
        self.assertEqual(lm.parse_network_mode(
            self.CFG.replace("lxcbr0", "br-lan")), "bridge")
        self.assertEqual(lm.parse_network_mode(
            self.CFG.replace("lxcbr0", "docker0")), "custom:docker0")
        self.assertEqual(lm.parse_network_mode("# no net\n"), "")

    def test_rewrite_to_bridge_and_back(self):
        out = lm.rewrite_network_mode(self.CFG, "bridge")
        self.assertIn("lxc.net.0.link = br-lan", out)
        self.assertNotIn("lxcbr0", out)
        # everything else untouched
        self.assertIn("lxc.net.0.hwaddr = 00:16:3e:dc:37:6a", out)
        back = lm.rewrite_network_mode(out, "nat")
        self.assertIn("lxc.net.0.link = lxcbr0", back)

    def test_rewrite_appends_net_block_when_missing(self):
        out = lm.rewrite_network_mode("lxc.uts.name = x\n", "nat")
        self.assertIn("lxc.net.0.type = veth", out)
        self.assertIn("lxc.net.0.link = lxcbr0", out)
        self.assertIn("lxc.net.0.flags = up", out)

    def test_set_network_gates(self):
        self.assertEqual(lm.set_network("bad name", "nat")["code"],
                         "bad_name")
        self.assertEqual(lm.set_network("jjj", "teleport")["code"],
                         "bad_mode")
        with patch.object(lm, '_read_file', return_value=""):
            self.assertEqual(lm.set_network("jjj", "nat")["code"],
                             "not_found")


class LimitsAndConfigTests(unittest.TestCase):

    def test_parse_limits(self):
        cfg = ("lxc.cgroup2.memory.max = 536870912\n"
               "lxc.cgroup2.cpu.max = 150000 100000\n")
        self.assertEqual(lm.parse_config_limits(cfg),
                         {"memory_mb": 512, "cpu_cores": 1.5})
        self.assertEqual(lm.parse_config_limits(""),
                         {"memory_mb": 0, "cpu_cores": 0.0})
        # "max" (unlimited) value is ignored, not crashed on
        self.assertEqual(
            lm.parse_config_limits("lxc.cgroup2.memory.max = max\n"),
            {"memory_mb": 0, "cpu_cores": 0.0})

    def test_rewrite_limits_roundtrip(self):
        base = "lxc.uts.name = jjj\n"
        out = lm.rewrite_limits(base, 512, 1.5)
        self.assertIn("lxc.cgroup2.memory.max = 536870912", out)
        self.assertIn("lxc.cgroup2.cpu.max = 150000 100000", out)
        self.assertEqual(lm.parse_config_limits(out),
                         {"memory_mb": 512, "cpu_cores": 1.5})
        # back to unlimited removes the lines entirely
        cleared = lm.rewrite_limits(out, 0, 0)
        self.assertNotIn("cgroup2", cleared)
        self.assertIn("lxc.uts.name = jjj", cleared)

    def test_rewrite_limits_replaces_not_duplicates(self):
        once = lm.rewrite_limits("x = y\n", 256, 1)
        twice = lm.rewrite_limits(once, 512, 2)
        self.assertEqual(twice.count("memory.max"), 1)
        self.assertIn("536870912", twice)

    def test_save_config_gates(self):
        self.assertEqual(lm.save_config("bad name", "x")["code"],
                         "bad_name")
        self.assertEqual(
            lm.save_config("jjj", "x" * (lm.CONFIG_MAX_BYTES + 1))["code"],
            "too_big")
        with patch.object(os.path, 'isfile', return_value=True):
            res = lm.save_config("jjj", "not an assignment line\n")
            self.assertEqual(res["code"], "bad_syntax")
            # comments and blanks are fine syntactically
            with patch("builtins.open", side_effect=OSError("ro")):
                res = lm.save_config("jjj", "# comment\n\nlxc.a = b\n")
                self.assertEqual(res["code"], "io_error")


class AutostartUciTests(unittest.TestCase):

    CFG = {
        "pcat_lxc.@container[0]": "container",
        "pcat_lxc.@container[0].name": "jjj",
        "pcat_lxc.@container[0].autostart": "1",
        "pcat_lxc.@container[1]": "container",
        "pcat_lxc.@container[1].name": "web",
        "pcat_lxc.@container[1].autostart": "0",
    }

    def test_autostart_map(self):
        with patch.object(lm, 'uci_show_dict', return_value=dict(self.CFG)):
            m = lm._autostart_map()
        self.assertEqual(m, {"jjj": True, "web": False})

    def test_section_lookup(self):
        with patch.object(lm, 'uci_show_dict', return_value=dict(self.CFG)):
            self.assertEqual(lm._autostart_section_for("web"),
                             "@container[1]")
            self.assertIsNone(lm._autostart_section_for("ghost"))


class VmidTests(unittest.TestCase):

    CFG = {
        "pcat_lxc.@container[0]": "container",
        "pcat_lxc.@container[0].name": "jjj",
        "pcat_lxc.@container[0].vmid": "100",
        "pcat_lxc.@container[1]": "container",
        "pcat_lxc.@container[1].name": "web",
    }

    def test_existing_ids_kept_new_get_lowest_free(self):
        sets, commits = [], []
        with patch.object(lm, '_ensure_uci_file', return_value=True), \
                patch.object(lm, 'uci_show_dict',
                             return_value=dict(self.CFG)), \
                patch.object(lm, 'uci_set', side_effect=sets.append), \
                patch.object(lm, 'uci_commit', side_effect=commits.append), \
                patch.object(lm, '_run',
                             return_value=(0, "@container[2]\n", "")):
            out = lm.ensure_vmids(["jjj", "web", "alpine1"])
        self.assertEqual(out["jjj"], 100)      # kept, not reassigned
        self.assertEqual(out["alpine1"], 101)  # 'alpine1' < 'web' in order
        self.assertEqual(out["web"], 102)
        # web had a section (no new name write); alpine1 needed one
        self.assertIn("pcat_lxc.@container[2].name=alpine1", sets)
        self.assertIn("pcat_lxc.@container[1].vmid=102", sets)
        self.assertEqual(commits, ["pcat_lxc"])

    def test_no_writes_when_all_assigned(self):
        cfg = dict(self.CFG)
        cfg["pcat_lxc.@container[1].vmid"] = "101"
        sets = []
        with patch.object(lm, '_ensure_uci_file', return_value=True), \
                patch.object(lm, 'uci_show_dict', return_value=cfg), \
                patch.object(lm, 'uci_set', side_effect=sets.append), \
                patch.object(lm, 'uci_commit') as commit:
            out = lm.ensure_vmids(["jjj", "web"])
        self.assertEqual(out, {"jjj": 100, "web": 101})
        self.assertEqual(sets, [])
        commit.assert_not_called()


class StorageCreateTests(unittest.TestCase):

    MOUNTS = ("overlayfs:/overlay / overlay rw 0 0\n"
              "/dev/mmcblk1p1 /mnt/mmcblk1p1 exfat rw 0 0\n"
              "/dev/nvme0n1p1 /mnt/nvme ext4 rw 0 0\n")

    def test_bad_storage_path_rejected(self):
        with patch.object(lm, 'lxcpath', return_value="/srv/lxc"):
            _, err = lm.create_container("ok", "debian", "trixie",
                                         storage="relative/path")
            self.assertEqual(err["code"], "bad_path")
            _, err = lm.create_container("ok", "debian", "trixie",
                                         storage="/mnt/../etc")
            self.assertEqual(err["code"], "bad_path")

    def test_fstype_longest_prefix(self):
        with patch.object(lm, '_read_file', return_value=self.MOUNTS):
            self.assertEqual(lm._fstype_for("/mnt/mmcblk1p1/lxc"), "exfat")
            self.assertEqual(lm._fstype_for("/mnt/nvme/x"), "ext4")
            self.assertEqual(lm._fstype_for("/srv/lxc"), "overlay")
            self.assertTrue(lm.fs_can_host_containers("/srv/lxc"))
            self.assertFalse(
                lm.fs_can_host_containers("/mnt/mmcblk1p1/lxc"))

    def test_exfat_storage_refused(self):
        with patch.object(lm, 'lxcpath', return_value="/srv/lxc"), \
                patch.object(lm, '_read_file', return_value=self.MOUNTS):
            _, err = lm.create_container(
                "ok", "debian", "trixie",
                storage="/mnt/mmcblk1p1/lxc-storage")
            self.assertEqual(err["code"], "bad_fs")
            self.assertEqual(err["detail"], "exfat")


class TargetProbeTests(unittest.TestCase):

    def _probe(self, rc, out, err=""):
        with patch.object(lm, 'ssh_key_ready', return_value=True), \
                patch.object(lm, '_run', return_value=(rc, out, err)):
            return lm.test_target("172.16.10.188", 22, "root")

    def test_full_openwrt_target(self):
        out = ("PCAT_OK\nHAS_TAR\nHAS_UBUS_LXC\n"
               "lxc.lxcpath = /mnt/big/lxc\n")
        res = self._probe(0, out)
        self.assertEqual(res["code"], "ok")
        self.assertTrue(res["tar"])
        self.assertTrue(res["ubus_lxc"])
        self.assertFalse(res["lxc_start"])
        self.assertEqual(res["remote_lxcpath"], "/mnt/big/lxc")

    def test_generic_linux_target_defaults_path(self):
        res = self._probe(0, "PCAT_OK\nHAS_TAR\nHAS_LXC_START\n")
        self.assertEqual(res["code"], "ok")
        self.assertEqual(res["remote_lxcpath"], "/srv/lxc")
        self.assertTrue(res["lxc_start"])

    def test_unreachable(self):
        res = self._probe(255, "", "Connection refused")
        self.assertEqual(res["code"], "unreachable")

    def test_no_key_short_circuits(self):
        with patch.object(lm, 'ssh_key_ready', return_value=False):
            self.assertEqual(
                lm.test_target("h", 22, "root")["code"], "no_key")


class FixBridgeTests(unittest.TestCase):

    def test_uci_writes_and_zone_membership(self):
        sets, lists, commits = [], [], []
        with patch.object(lm, 'uci_set', side_effect=sets.append), \
                patch.object(lm, 'uci_add_list', side_effect=lists.append), \
                patch.object(lm, 'uci_commit', side_effect=commits.append), \
                patch.object(lm, 'uci_get', side_effect=lambda k:
                             "lan" if "@zone[0].name" in k else
                             "lan wifi" if "@zone[0].network" in k else ""), \
                patch.object(lm, '_run', return_value=(0, "", "")), \
                patch.object(lm, '_bridge_runtime_up', return_value=True):
            res = lm.fix_bridge()
        self.assertEqual(res["code"], "ok")
        joined = "\n".join(sets)
        self.assertIn("network.lxcbr0dev=device", joined)
        self.assertIn("network.lxcbr0dev.bridge_empty=1", joined)
        self.assertIn("network.lxc.proto=static", joined)
        self.assertIn("network.lxc.ipaddr=10.0.3.1", joined)
        self.assertIn("dhcp.lxc=dhcp", joined)
        self.assertEqual(lists, ["firewall.@zone[0].network=lxc"])
        for cfg in ("network", "dhcp", "firewall"):
            self.assertIn(cfg, commits)

    def test_zone_add_skipped_when_present(self):
        lists = []
        with patch.object(lm, 'uci_set'), \
                patch.object(lm, 'uci_add_list', side_effect=lists.append), \
                patch.object(lm, 'uci_commit'), \
                patch.object(lm, 'uci_get', side_effect=lambda k:
                             "lan" if "@zone[0].name" in k else
                             "lan lxc" if "@zone[0].network" in k else ""), \
                patch.object(lm, '_run', return_value=(0, "", "")), \
                patch.object(lm, '_bridge_runtime_up', return_value=True):
            lm.fix_bridge()
        self.assertEqual(lists, [])


class ApplyFixTests(unittest.TestCase):

    def test_bad_key(self):
        self.assertEqual(lm.apply_fix("rm -rf")["code"], "bad_fix")

    def test_all_aggregates(self):
        with patch.object(lm, 'fix_bridge', return_value={"code": "ok"}), \
                patch.object(lm, 'fix_cgroup',
                             return_value={"code": "reboot_required"}), \
                patch.object(lm, 'fix_autostart',
                             return_value={"code": "ok"}), \
                patch.object(lm, 'fix_sshkey', return_value={"code": "ok"}):
            res = lm.apply_fix("all")
        self.assertEqual(res["code"], "partial")
        self.assertEqual(res["results"]["cgroup"]["code"], "reboot_required")


class Cgroup2GuardTests(unittest.TestCase):
    """The START=02 guard is what makes LXC work without user intervention.

    Two facts measured on a photonicat2 shape it, and both are pinned here
    because neither is visible from the code alone:

    * A live swap after boot cannot work -- 110s in, with no sub-cgroups,
      umount of the cpu controller returns EBUSY -- so the correction must
      happen at START=02, right after the shim and before anything populates
      the hierarchy.
    * `cgroupfs-mount disable` does not survive a firmware upgrade. sysupgrade
      -c carries the overlay across as a tar of files: an added file returns, a
      deleted rc.d symlink does not. So the fix has to be an added file.
    """

    def test_the_guard_script_runs_after_the_shim(self):
        """START=01 is cgroupfs-mount; anything later than 02 is too late."""
        self.assertIn("START=02", lm.CGROUP2_INIT_SCRIPT)

    def test_the_guard_is_a_noop_when_already_unified(self):
        """procd's own cgroup2 survived: it must not tear that down."""
        self.assertIn("/sys/fs/cgroup/cgroup.controllers", lm.CGROUP2_INIT_SCRIPT)
        self.assertIn("return 0", lm.CGROUP2_INIT_SCRIPT)

    def test_the_guard_unmounts_deepest_first_then_mounts_cgroup2(self):
        """The tmpfs root cannot go before the controllers sitting on it."""
        self.assertIn("sort -r", lm.CGROUP2_INIT_SCRIPT)
        self.assertIn("mount -t cgroup2 cgroup2 /sys/fs/cgroup",
                      lm.CGROUP2_INIT_SCRIPT)
        self.assertLess(lm.CGROUP2_INIT_SCRIPT.index("umount"),
                        lm.CGROUP2_INIT_SCRIPT.index("mount -t cgroup2"))

    def test_installed_means_present_and_enabled(self):
        with patch.object(lm.os.path, 'exists', return_value=True), \
                patch.object(lm.os, 'listdir', return_value=["S02pcat-cgroup2"]):
            self.assertTrue(lm.cgroup2_guard_installed())
        # Present but not enabled -- no rc.d symlink -- does not count.
        with patch.object(lm.os.path, 'exists', return_value=True), \
                patch.object(lm.os, 'listdir', return_value=["S01cgroupfs-mount"]):
            self.assertFalse(lm.cgroup2_guard_installed())

    def test_install_writes_and_enables(self):
        writes = {}

        def fake_open(path, mode="r"):
            import io
            buf = io.StringIO()
            buf.close = lambda: writes.__setitem__(path, buf.getvalue())
            return buf

        with patch("builtins.open", fake_open), \
                patch.object(lm.os, 'chmod') as chmod, \
                patch.object(lm, '_run', return_value=(0, "", "")) as run:
            self.assertEqual(lm.install_cgroup2_guard()["code"], "ok")
        self.assertIn("START=02", writes[lm.CGROUP2_INIT])
        chmod.assert_called_once_with(lm.CGROUP2_INIT, 0o755)
        run.assert_called_once_with([lm.CGROUP2_INIT, "enable"])

    def test_fix_cgroup_installs_the_guard_before_anything_else(self):
        """The guard is the only part that outlives a flash."""
        with patch.object(lm, 'install_cgroup2_guard',
                          return_value={"code": "ok"}) as install, \
                patch.object(lm.os.path, 'exists', return_value=False), \
                patch.object(lm, 'cgroup_mode', return_value="unified"):
            self.assertEqual(lm.fix_cgroup()["code"], "ok")
        install.assert_called_once()


class Cgroup2StartupTests(unittest.TestCase):
    """The guard half of startup(). Storage recovery runs first and is stubbed
    out throughout: left real it walks the host's /srv, so these tests reported
    the dev machine's filesystem rather than the guard they are about."""

    def test_noop_without_the_lxc_runtime(self):
        """The guard changes system-wide cgroup layout; do not do that on an
        image that cannot run containers anyway."""
        with patch.object(lm, '_which', return_value=None), \
                patch.object(lm, 'install_cgroup2_guard') as install:
            self.assertIsNone(lm.startup())
        install.assert_not_called()

    def test_noop_when_already_installed(self):
        with patch.object(lm, '_which', return_value="/usr/bin/lxc-create"), \
                patch.object(lm, 'recover_containers', return_value={}), \
                patch.object(lm, 'cgroup2_guard_installed', return_value=True), \
                patch.object(lm, 'install_cgroup2_guard') as install:
            self.assertIsNone(lm.startup())
        install.assert_not_called()

    def test_installs_when_runtime_present_and_guard_missing(self):
        with patch.object(lm, '_which', return_value="/usr/bin/lxc-create"), \
                patch.object(lm, 'recover_containers', return_value={}), \
                patch.object(lm, 'cgroup2_guard_installed', return_value=False), \
                patch.object(lm, 'cgroup_mode', return_value="legacy"), \
                patch.object(lm, 'install_cgroup2_guard',
                             return_value={"code": "ok"}) as install:
            msg = lm.startup()
        install.assert_called_once()
        self.assertIn("guard installed", msg)

    def test_never_reboots_the_device(self):
        """Rebooting because the web app started would be its own bug: the
        guard takes effect on the next boot, whenever that happens to be."""
        with patch.object(lm, '_which', return_value="/usr/bin/lxc-create"), \
                patch.object(lm, 'recover_containers', return_value={}), \
                patch.object(lm, 'cgroup2_guard_installed', return_value=False), \
                patch.object(lm, 'cgroup_mode', return_value="legacy"), \
                patch.object(lm, '_run', return_value=(0, "", "")) as run, \
                patch("builtins.open", MagicMock()), \
                patch.object(lm.os, 'chmod'):
            lm.startup()
        for call in run.call_args_list:
            argv = call.args[0] if call.args else []
            self.assertNotIn("reboot", " ".join(str(a) for a in argv))


class CgroupPreflightTests(unittest.TestCase):

    def _cgroup_check(self):
        with patch.object(lm, 'container_states', return_value={}), \
                patch.object(lm, 'lxcpath', return_value="/srv/lxc"):
            return lm.preflight()["checks"]["cgroup"]

    def test_unified_with_guard_is_ok_even_if_the_shim_is_enabled(self):
        """The guard runs after the shim and undoes it. Reporting this as a
        failure told the user 'not set up' about a working cgroup2 device."""
        with patch.object(lm, 'cgroup_mode', return_value="unified"), \
                patch.object(lm, '_cgroupfs_mount_enabled', return_value=True), \
                patch.object(lm, 'cgroup2_guard_installed', return_value=True):
            check = self._cgroup_check()
        self.assertTrue(check["ok"])
        self.assertTrue(check["guard_installed"])

    def test_legacy_is_never_ok(self):
        with patch.object(lm, 'cgroup_mode', return_value="legacy"), \
                patch.object(lm, '_cgroupfs_mount_enabled', return_value=False), \
                patch.object(lm, 'cgroup2_guard_installed', return_value=True):
            self.assertFalse(self._cgroup_check()["ok"])

    def test_unified_without_guard_but_shim_enabled_is_not_ok(self):
        """Unified now, but the next boot puts the v1 forest back."""
        with patch.object(lm, 'cgroup_mode', return_value="unified"), \
                patch.object(lm, '_cgroupfs_mount_enabled', return_value=True), \
                patch.object(lm, 'cgroup2_guard_installed', return_value=False):
            self.assertFalse(self._cgroup_check()["ok"])

    def test_unified_with_shim_disabled_is_ok(self):
        with patch.object(lm, 'cgroup_mode', return_value="unified"), \
                patch.object(lm, '_cgroupfs_mount_enabled', return_value=False), \
                patch.object(lm, 'cgroup2_guard_installed', return_value=False):
            self.assertTrue(self._cgroup_check()["ok"])


class StartSelfHealTests(unittest.TestCase):
    """A fresh device has no lxcbr0 until fix_bridge() has run; a Start click
    used to surface rpcd's bare "Invalid argument". Pins the self-heal
    (bridge recreated from the Start itself) and the failure diagnosis
    (actionable codes instead of raw ubus stderr)."""

    NAT_CFG = "lxc.net.0.type = veth\nlxc.net.0.link = lxcbr0\n"

    def test_missing_lxcbr0_runs_fix_bridge_then_starts(self):
        with patch.object(lm, '_read_file', return_value=self.NAT_CFG), \
                patch.object(lm, '_link_up', side_effect=[False, True]), \
                patch.object(lm, 'fix_bridge') as fix, \
                patch.object(lm, '_ubus', return_value=(True, {})) as ubus, \
                patch.object(lm, 'container_states',
                             return_value={"jjj": "RUNNING"}), \
                patch.object(lm.time, 'sleep'):
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "ok")
        fix.assert_called_once_with()
        ubus.assert_called_once_with("start", {"name": "jjj"}, timeout=40)

    def test_bridge_still_missing_blocks_with_code_not_raw_stderr(self):
        with patch.object(lm, '_read_file', return_value=self.NAT_CFG), \
                patch.object(lm, '_link_up', return_value=False), \
                patch.object(lm, 'fix_bridge'), \
                patch.object(lm, '_ubus') as ubus:
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "bridge_missing")
        ubus.assert_not_called()

    def test_bridge_up_is_zero_overhead_no_fix_calls(self):
        with patch.object(lm, '_read_file', return_value=self.NAT_CFG), \
                patch.object(lm, '_link_up', return_value=True), \
                patch.object(lm, 'fix_bridge') as fix, \
                patch.object(lm, '_ubus', return_value=(True, {})), \
                patch.object(lm, 'container_states',
                             return_value={"jjj": "RUNNING"}), \
                patch.object(lm.time, 'sleep'):
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "ok")
        fix.assert_not_called()

    def test_ubus_failure_on_legacy_cgroup_says_reboot(self):
        with patch.object(lm, '_read_file', return_value=self.NAT_CFG), \
                patch.object(lm, '_link_up', return_value=True), \
                patch.object(lm, '_ubus', return_value=(
                    False, {"error": "Command failed: Invalid argument"})), \
                patch.object(lm, 'cgroup_mode', return_value="legacy"), \
                patch.object(lm, 'install_cgroup2_guard') as guard:
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "cgroup_legacy")
        guard.assert_called_once_with()

    def test_unknown_ubus_failure_keeps_the_raw_detail(self):
        with patch.object(lm, '_read_file', return_value=self.NAT_CFG), \
                patch.object(lm, '_link_up', return_value=True), \
                patch.object(lm, '_ubus', return_value=(
                    False, {"error": "Command failed: Invalid argument"})), \
                patch.object(lm, 'cgroup_mode', return_value="unified"):
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "ubus_error")
        self.assertIn("Invalid argument", res["detail"])

    def test_immediate_exit_on_legacy_cgroup_says_reboot(self):
        """systemd >= 256 guest on a not-yet-rebooted v1 host: ubus start
        succeeds, the container exits within the settle window."""
        with patch.object(lm, '_read_file', return_value=self.NAT_CFG), \
                patch.object(lm, '_link_up', return_value=True), \
                patch.object(lm, '_ubus', return_value=(True, {})), \
                patch.object(lm, 'container_states',
                             return_value={"jjj": "STOPPED"}), \
                patch.object(lm, 'cgroup_mode', return_value="legacy"), \
                patch.object(lm, 'install_cgroup2_guard'), \
                patch.object(lm.time, 'sleep'):
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "cgroup_legacy")

    def test_no_net_section_starts_without_heal(self):
        with patch.object(lm, '_read_file', return_value="# no net\n"), \
                patch.object(lm, '_link_up') as up, \
                patch.object(lm, '_ubus', return_value=(True, {})), \
                patch.object(lm, 'container_states',
                             return_value={"jjj": "RUNNING"}), \
                patch.object(lm.time, 'sleep'):
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "ok")
        up.assert_not_called()

    def test_custom_bridge_reloads_network_not_fix_bridge(self):
        cfg = "lxc.net.0.link = br-iot\n"
        devs = {"lxcnet_iot": {"name": "br-iot", "type": "bridge"}}
        runs = []
        with patch.object(lm, '_read_file', return_value=cfg), \
                patch.object(lm, '_link_up', side_effect=[False, True, True]), \
                patch.object(lm, '_uci_typed_sections', return_value=devs), \
                patch.object(lm, 'fix_bridge') as fix, \
                patch.object(lm, '_run',
                             side_effect=lambda a, **k: runs.append(a) or (0, "", "")), \
                patch.object(lm, '_ubus', return_value=(True, {})), \
                patch.object(lm, 'container_states',
                             return_value={"jjj": "RUNNING"}), \
                patch.object(lm.time, 'sleep'):
            res = lm.container_action("start", "jjj")
        self.assertEqual(res["code"], "ok")
        fix.assert_not_called()
        self.assertIn(["/etc/init.d/network", "reload"], runs)


if __name__ == "__main__":
    unittest.main()
