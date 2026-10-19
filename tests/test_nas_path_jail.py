#!/usr/bin/env python3
"""Unit tests for the NAS path jail (pcat_util/nas_files.resolve_jailed).

The jail is the single security boundary between browser-supplied paths and
the router's filesystem: every file-manager endpoint resolves through it.
These tests pin the containment semantics — realpath collapse of "..",
symlink escapes (built with real symlinks in a tempdir), the /mnt/a vs
/mnt/ab prefix trap, NUL/length rejection, and the empty-roots lockdown.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_path_jail`.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

from pcat_util.nas_files import (  # noqa: E402
    PathJailError,
    resolve_jailed,
    safe_relpath,
    valid_name,
)


class ResolveJailedTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # realpath the root itself: on macOS /var -> /private/var, and the
        # jail compares canonical paths.
        self.root = os.path.realpath(self.tmp.name)
        self.roots = [self.root]
        os.makedirs(os.path.join(self.root, "sub", "deeper"))
        with open(os.path.join(self.root, "sub", "f.txt"), "w") as f:
            f.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_plain_path_inside_root(self):
        p = resolve_jailed(os.path.join(self.root, "sub", "f.txt"), self.roots)
        self.assertEqual(p, os.path.join(self.root, "sub", "f.txt"))

    def test_root_itself_is_allowed(self):
        self.assertEqual(resolve_jailed(self.root, self.roots), self.root)

    def test_dotdot_collapse_stays_inside(self):
        p = resolve_jailed(
            os.path.join(self.root, "sub", "deeper", "..", "f.txt"), self.roots)
        self.assertEqual(p, os.path.join(self.root, "sub", "f.txt"))

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(PathJailError):
            resolve_jailed(os.path.join(self.root, "..", "etc", "passwd"),
                           self.roots)

    def test_absolute_path_outside_rejected(self):
        with self.assertRaises(PathJailError):
            resolve_jailed("/etc/passwd", self.roots)

    def test_symlink_out_of_root_rejected(self):
        outside = tempfile.mkdtemp()
        try:
            link = os.path.join(self.root, "escape")
            os.symlink(outside, link)
            with self.assertRaises(PathJailError):
                resolve_jailed(os.path.join(link, "x"), self.roots)
            with self.assertRaises(PathJailError):
                resolve_jailed(link, self.roots)
        finally:
            os.rmdir(outside)

    def test_symlink_within_root_allowed(self):
        os.symlink(os.path.join(self.root, "sub"),
                   os.path.join(self.root, "alias"))
        p = resolve_jailed(os.path.join(self.root, "alias", "f.txt"),
                           self.roots)
        self.assertEqual(p, os.path.join(self.root, "sub", "f.txt"))

    def test_prefix_sibling_trap(self):
        # /mnt/a must not admit /mnt/ab — the classic startswith bug.
        sibling = self.root + "b"
        os.mkdir(sibling)
        try:
            with self.assertRaises(PathJailError):
                resolve_jailed(sibling, self.roots)
            with self.assertRaises(PathJailError):
                resolve_jailed(os.path.join(sibling, "f"), self.roots)
        finally:
            os.rmdir(sibling)

    def test_nul_and_length_and_type(self):
        with self.assertRaises(PathJailError):
            resolve_jailed(self.root + "/f\x00oo", self.roots)
        with self.assertRaises(PathJailError):
            resolve_jailed(self.root + "/x" * 5000, self.roots)
        with self.assertRaises(PathJailError):
            resolve_jailed(None, self.roots)

    def test_empty_roots_reject_everything(self):
        with self.assertRaises(PathJailError):
            resolve_jailed(self.root, [])
        with self.assertRaises(PathJailError):
            resolve_jailed("/", None)

    def test_multiple_roots(self):
        other = tempfile.mkdtemp()
        try:
            real_other = os.path.realpath(other)
            roots = [self.root, real_other]
            self.assertEqual(resolve_jailed(real_other, roots), real_other)
        finally:
            os.rmdir(other)


class RelpathAndNameTests(unittest.TestCase):

    def test_safe_relpath_plain(self):
        self.assertEqual(safe_relpath("a.jpg"), ["a.jpg"])

    def test_safe_relpath_nested(self):
        self.assertEqual(safe_relpath("photos/2026/a.jpg"),
                         ["photos", "2026", "a.jpg"])

    def test_safe_relpath_rejects_traversal(self):
        for bad in ("../a", "a/../../b", "/abs/path", "", "a\x00b", "..",
                    "photos/../../a"):
            with self.assertRaises(PathJailError, msg=repr(bad)):
                safe_relpath(bad)

    def test_safe_relpath_collapses_empty_segments(self):
        self.assertEqual(safe_relpath("a//b/./c.txt"), ["a", "b", "c.txt"])

    def test_valid_name(self):
        self.assertTrue(valid_name("hello.txt"))
        self.assertTrue(valid_name("中文名.txt"))
        for bad in ("", ".", "..", "a/b", "a\x00b", ".pcat-upload-x.part",
                    "x" * 256, None):
            self.assertFalse(valid_name(bad), repr(bad))


if __name__ == '__main__':
    unittest.main()
