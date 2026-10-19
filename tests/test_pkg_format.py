#!/usr/bin/env python3
"""IPK vs APK package handling on the /update page.

photonicatWrt 26.04 images install .ipk with opkg; master images install .apk
with apk-tools. A device has exactly one of the two, so everything on the
update path -- the upload and URL endpoints, the version lists and
opkg-install.py -- must accept that one format and refuse the other with a
message naming the right one. Package downloads skip HTTPS certificate
verification unless the user opts in.

    python3 -m unittest tests.test_pkg_format -v
"""

import io
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO_ROOT, "app")
sys.path.insert(0, APP)

for _absent in ("pam", "serial", "serial.tools", "serial.tools.list_ports"):
    if _absent not in sys.modules:
        _mod = types.ModuleType(_absent)
        _mod.__getattr__ = lambda name: types.SimpleNamespace()
        sys.modules[_absent] = _mod

import pcat_util  # noqa: E402

if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_pkg_format")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

from pcat_util import pkg_format  # noqa: E402
import pcat_manager_web as web  # noqa: E402


CSRF = "test-csrf-token"
HEADERS = {"X-CSRF-Token": CSRF}


def visible(html):
    """The page minus HTML and // comments, which users never see."""
    import re
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    return re.sub(r"^\s*//.*$", "", html, flags=re.M)


class _FormatCase(unittest.TestCase):
    """Pin the detected format for the duration of a test."""

    FORMAT = pkg_format.IPK

    def setUp(self):
        self._pin = patch.object(pkg_format, "_cached", self.FORMAT)
        self._pin.start()

    def tearDown(self):
        self._pin.stop()


class DetectTests(unittest.TestCase):

    def detect(self, apk_bin, apk_db):
        with patch.object(pkg_format, "_cached", None), \
             patch.object(pkg_format.shutil, "which",
                          lambda b: "/usr/bin/apk" if (b == "apk" and apk_bin) else None), \
             patch.object(pkg_format.os.path, "exists", lambda p: apk_db):
            return pkg_format.detect()

    def test_apk_needs_both_the_tool_and_its_database(self):
        self.assertEqual(self.detect(True, True), pkg_format.APK)
        self.assertEqual(self.detect(True, False), pkg_format.IPK)
        self.assertEqual(self.detect(False, True), pkg_format.IPK)

    def test_opkg_is_the_fallback(self):
        self.assertEqual(self.detect(False, False), pkg_format.IPK)


class ParseTests(unittest.TestCase):

    def test_opkg_list_installed(self):
        out = "pcat-manager - 2.0.0-63\nbase-files - 1655-r7922\n\n"
        self.assertEqual(pkg_format.parse_installed(out, pkg_format.IPK),
                         [("pcat-manager", "2.0.0-63"), ("base-files", "1655-r7922")])

    def test_apk_list_installed(self):
        # Captured from a photonicat on r7893.
        out = ("pcat-manager-2.0.0-r63 aarch64_generic {feeds/base/pcat/pcat-manager} (GPL-3.0) [installed]\n"
               "pcat-manager-web-2.2.1-r277 aarch64_generic {feeds/pcat_addons/net/pcat-manager-web} (GPL-3.0) [installed]\n"
               "libjson-c5-0.18-r1 aarch64_generic {feeds/base/libs/json-c} (MIT) [installed]\n")
        self.assertEqual(pkg_format.parse_installed(out, pkg_format.APK),
                         [("pcat-manager", "2.0.0-r63"),
                          ("pcat-manager-web", "2.2.1-r277"),
                          ("libjson-c5", "0.18-r1")])

    def test_package_file_names(self):
        self.assertEqual(pkg_format.parse_package_file_name(
            "pcat-manager_1.0.2-0_aarch64_cortex-a53.ipk", pkg_format.IPK),
            ("pcat-manager", "1.0.2-0"))
        self.assertEqual(pkg_format.parse_package_file_name(
            "pcat-manager-web-2.2.1-r278.apk", pkg_format.APK),
            ("pcat-manager-web", "2.2.1-r278"))

    def test_other_format_file_names_are_not_packages(self):
        self.assertIsNone(pkg_format.parse_package_file_name(
            "pcat-manager-2.0.0-r63.apk", pkg_format.IPK))
        self.assertIsNone(pkg_format.parse_package_file_name(
            "pcat-manager_1.0.2-0_aarch64.ipk", pkg_format.APK))


class InstallCommandTests(unittest.TestCase):

    def test_ipk_uses_opkg(self):
        self.assertEqual(pkg_format.install_command(["/t/a.ipk"], pkg_format.IPK)[:2],
                         ["opkg", "install"])

    def test_apk_allows_unsigned_local_packages(self):
        cmd = pkg_format.install_command(["/t/a.apk"], pkg_format.APK)
        self.assertEqual(cmd[:2], ["apk", "add"])
        self.assertIn("--allow-untrusted", cmd)
        self.assertEqual(cmd[-1], "/t/a.apk")


class HttpGetTests(unittest.TestCase):

    def test_certificates_are_not_verified_by_default(self):
        with patch("requests.get") as get:
            pkg_format.http_get("https://x/a.ipk", timeout=5)
        self.assertIs(get.call_args.kwargs["verify"], False)

    def test_verification_is_opt_in(self):
        with patch("requests.get") as get:
            pkg_format.http_get("https://x/a.ipk", verify_tls=True, timeout=5)
        self.assertIs(get.call_args.kwargs["verify"], True)


class _RouteCase(_FormatCase):

    def setUp(self):
        super().setUp()
        web.flask_app.config["TESTING"] = True
        self.client = web.flask_app.test_client()
        with self.client.session_transaction() as sess:
            sess["username"] = "admin"
            sess["_csrf_token"] = CSRF

    def upload(self, name):
        return self.client.post("/pkg_upload", data={"package": (io.BytesIO(b"x"), name)},
                                content_type="multipart/form-data",
                                headers=HEADERS).get_json()

    def download(self, url, **extra):
        return self.client.post("/api/v1/download_ipk", json=dict(url=url, **extra),
                                headers=HEADERS).get_json()


class IpkDeviceRouteTests(_RouteCase):
    FORMAT = pkg_format.IPK

    def test_upload_refuses_apk_and_names_the_right_format(self):
        resp = self.upload("pcat-manager-2.0.0-r63.apk")
        self.assertEqual(resp["status"], "failed")
        self.assertIn("installs IPK packages", resp["message"])

    def test_download_refuses_apk(self):
        resp = self.download("http://x/pcat-manager-2.0.0-r63.apk")
        self.assertEqual(resp["status"], "failed")
        self.assertIn("installs IPK packages", resp["message"])

    def test_upload_file_name_cannot_escape_the_temp_dir(self):
        with patch("werkzeug.datastructures.FileStorage.save") as save:
            resp = self.upload("../../etc/evil.ipk")
        self.assertEqual(resp["status"], "ok")
        saved = str(save.call_args.args[0])
        self.assertTrue(saved.endswith(os.path.join("temp", "evil.ipk")), saved)

    def test_download_skips_certificate_checks_unless_asked(self):
        for extra, expect in (({}, False), ({"verify_tls": True}, True)):
            rsp = MagicMock(headers={}, iter_content=lambda chunk_size: [b"pkg"])
            with patch.object(web.pkg_format, "http_get", return_value=rsp) as get, \
                 tempfile.TemporaryDirectory() as tmp, \
                 patch.object(web.pathlib.Path, "resolve",
                              lambda self: web.pathlib.Path(tmp)):
                resp = self.download("https://x/pcat-foo_1.0-1_all.ipk", **extra)
            self.assertEqual(resp["status"], "ok", resp)
            self.assertIs(get.call_args.kwargs["verify_tls"], expect)


class ApkDeviceRouteTests(_RouteCase):
    FORMAT = pkg_format.APK

    def test_upload_refuses_ipk(self):
        resp = self.upload("pcat-manager_2.0.0-63_aarch64.ipk")
        self.assertEqual(resp["status"], "failed")
        self.assertIn("installs APK packages", resp["message"])

    def test_page_names_apk_and_never_ipk(self):
        html = self.client.get("/update").get_data(as_text=True)
        self.assertIn("APK Package Management", html)
        self.assertIn('accept=".apk,.tar,.gz,.xz,.bz2"', html)
        self.assertNotIn("IPK", visible(html).replace("_ipk", ""))
        self.assertNotIn("{fmt}", html)

    def test_remote_list_keeps_only_apk_lines(self):
        body = (b"pcat-manager_2.0.0-63_aarch64_generic.ipk\n"
                b"pcat-manager-2.0.0-r64.apk\n"
                b"somethingelse-1.0-r1.apk\n")
        import app as core
        with patch.object(core.pkg_format, "http_get",
                          return_value=MagicMock(content=body)):
            self.assertEqual(core.remote_pcat_package_version_info(),
                             [{"name": "pcat-manager", "full_name": "pcat-manager-2.0.0-r64.apk",
                               "version": "2.0.0-r64"}])


class InstallScriptTests(unittest.TestCase):
    """Run opkg-install.py for real, with fake opkg/apk on PATH."""

    def run_script(self, fmt, target):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "bin")
            os.mkdir(bindir)
            for tool in ("opkg", "apk"):
                path = os.path.join(bindir, tool)
                with open(path, "w") as fh:
                    fh.write('#!/bin/sh\necho "%s $@"\n' % tool)
                os.chmod(path, 0o755)
            # Force detection: sitecustomize-free, by pinning the cache.
            boot = ("import sys; sys.path.insert(0, %r); "
                    "from pcat_util import pkg_format; pkg_format._cached = %r; "
                    "import runpy; sys.argv = ['opkg-install.py', 'install', %r]; "
                    "runpy.run_path(%r, run_name='__main__')"
                    % (APP, fmt, target(tmp), os.path.join(APP, "opkg-install.py")))
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
                       PCAT_PKG_WORKSPACE=os.path.join(tmp, "ws"))
            return subprocess.run([sys.executable, "-c", boot], env=env,
                                  capture_output=True, text=True).stdout

    @staticmethod
    def archive(members):
        def make(tmp):
            path = os.path.join(tmp, "bundle.tar.gz")
            with tarfile.open(path, "w:gz") as tar:
                for name in members:
                    data = b"pkg"
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            return path
        return make

    @staticmethod
    def single(name):
        def make(tmp):
            path = os.path.join(tmp, name)
            open(path, "wb").write(b"pkg")
            return path
        return make

    def test_apk_device_installs_with_apk(self):
        out = self.run_script(pkg_format.APK, self.single("foo-1.0-r1.apk"))
        self.assertIn("apk add --allow-untrusted", out)
        self.assertIn("Package installed successfully.", out)

    def test_ipk_device_installs_with_opkg(self):
        out = self.run_script(pkg_format.IPK, self.single("foo_1.0-1_all.ipk"))
        self.assertIn("opkg install", out)
        self.assertIn("Package installed successfully.", out)

    def test_archive_installs_only_the_devices_format(self):
        out = self.run_script(pkg_format.APK, self.archive(
            ["pkgs/a-1.0-r1.apk", "pkgs/b-2.0-r1.apk", "pkgs/c_1.0-1_all.ipk"]))
        self.assertIn("Found 2 APK files", out)
        self.assertNotIn("c_1.0-1_all.ipk", out.split("Found")[1])
        self.assertIn("Package installed successfully.", out)

    def test_other_format_is_refused(self):
        out = self.run_script(pkg_format.IPK, self.single("foo-1.0-r1.apk"))
        self.assertIn("This firmware installs IPK packages; APK files are not supported.", out)
        self.assertIn("installation failed", out)
        self.assertNotIn("installed successfully", out)

    def test_archive_without_the_devices_format_fails(self):
        out = self.run_script(pkg_format.APK, self.archive(["x_1.0-1_all.ipk"]))
        self.assertIn("No APK files found in archive", out)
        self.assertIn("installation failed", out)


if __name__ == "__main__":
    unittest.main()
