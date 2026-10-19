#!/usr/bin/env python3
"""Regression tests for the firmware-upgrade integrity path.

Both cases here were live defects found while investigating an OTA that
failed and left the device back on its old build:

* A SHA256 mismatch raised a plain Exception inside a block whose own
  `except Exception` downgraded it to a warning and carried on -- so the
  checksum could not fail an upgrade and a corrupt image was flashed anyway.
* The "Removing previous download" step removed nothing, while the downloader
  runs with --continue. A stale part-file was resumed rather than replaced,
  and on a dev server whose builds are re-cut under the same URL that splices
  two different images together.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_firmware_integrity`.
"""

import ast
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO_ROOT, "app", "pcat_manager_web.py")
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))


def _function_source(name):
    """The source of a top-level function, without importing the module.

    pcat_manager_web pulls in the whole hardware stack at import; these are
    structural assertions about one function, so parse rather than import.
    """
    with open(APP) as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("function %s not found in %s" % (name, APP))


class ChecksumMustAbortTests(unittest.TestCase):
    """A fetched checksum that does not match must stop the flash."""

    def setUp(self):
        self.fn = _function_source("background_firmware_upgrade_thread")

    def test_mismatch_raises_a_dedicated_type(self):
        """Not a bare Exception -- the surrounding handlers swallow those."""
        raises = [n for n in ast.walk(self.fn)
                  if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                  and getattr(n.exc.func, "id", "") == "FirmwareIntegrityError"]
        self.assertTrue(raises, "checksum mismatch must raise FirmwareIntegrityError")

    def test_no_handler_swallows_it_and_still_reaches_the_flash(self):
        """`except Exception` around the verification must not catch it.

        The distinction that matters is not "does a handler catch it" but
        "does catching it still leave the flash reachable". The function's
        outermost try wraps the sysupgrade call too, so catching there aborts
        the upgrade and is correct. The verification block does not: anything
        it swallows continues on to flash the image it just failed to verify.

        So: for every try that can see the raise but does NOT contain the
        flash, a broad handler must be preceded by a FirmwareIntegrityError
        one.
        """
        offenders = []
        for node in ast.walk(self.fn):
            if not isinstance(node, ast.Try):
                continue
            body_src = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "FirmwareIntegrityError" not in body_src:
                continue          # cannot see the raise; irrelevant
            if "sysupgrade" in body_src:
                continue          # wraps the flash, so catching aborts it
            names = []
            for handler in node.handlers:
                if handler.type is None:
                    names.append("bare")
                elif isinstance(handler.type, ast.Name):
                    names.append(handler.type.id)
                else:
                    names.append(ast.dump(handler.type))
            for i, name in enumerate(names):
                if (name in ("Exception", "BaseException", "bare")
                        and "FirmwareIntegrityError" not in names[:i]):
                    offenders.append((node.lineno, name))
        self.assertEqual(
            offenders, [],
            "these handlers swallow FirmwareIntegrityError and let a corrupt "
            "image flash: %r" % (offenders,))

    def test_the_exception_type_exists(self):
        with open(APP) as fh:
            tree = ast.parse(fh.read())
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        self.assertIn("FirmwareIntegrityError", classes)


class StaleDownloadTests(unittest.TestCase):
    def test_the_previous_download_is_actually_removed(self):
        """The status message promised a removal that never happened."""
        fn = _function_source("background_firmware_upgrade_thread")
        removes = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", "") == "remove"
                   and getattr(getattr(n.func, "value", None), "id", "") == "os"]
        self.assertTrue(removes,
                        "a stale part-file must be deleted, not resumed onto")


class PostMortemSurvivesTheRebootTests(unittest.TestCase):
    def test_the_flash_log_is_not_on_tmpfs(self):
        """A sysupgrade ends in a reboot; /tmp does not survive one."""
        with open(APP) as fh:
            src = fh.read()
        self.assertNotIn("log_file = '/tmp/sysupgrade.log'", src,
                         "the flash transcript must outlive the flash")
        self.assertIn("SYSUPGRADE_LOG", src)

    def test_the_log_path_is_persistent(self):
        import re
        with open(APP) as fh:
            src = fh.read()
        match = re.search(r"SYSUPGRADE_LOG = '([^']+)'", src)
        self.assertIsNotNone(match)
        self.assertFalse(match.group(1).startswith("/tmp/"),
                         "%s is tmpfs" % match.group(1))


if __name__ == "__main__":
    unittest.main()
