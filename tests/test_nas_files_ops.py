#!/usr/bin/env python3
"""Unit tests for NAS file operations (pcat_util/nas_files).

write_stream is the upload core: it must stage to a .part file on the
destination filesystem, enforce the size cap, honor the three conflict modes,
and leave either the old file or the complete new one — never a truncation
and never an orphaned .part. zip_stream must produce a valid archive without
buffering whole files (chunked drain). delete/rename/mkdir carry the
file-manager semantics the UI depends on.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_files_ops`.
"""

import io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

from pcat_util import nas_files  # noqa: E402
from pcat_util.nas_files import FileOpError  # noqa: E402


class WriteStreamTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _no_parts_left(self):
        leftovers = [n for n in os.listdir(self.dir)
                     if n.startswith(".pcat-upload-")]
        self.assertEqual(leftovers, [])

    def test_basic_write(self):
        out = nas_files.write_stream(self.dir, "a.bin", io.BytesIO(b"hello"),
                                     max_bytes=100)
        self.assertEqual(out["size"], 5)
        self.assertIsNone(out["renamed_to"])
        with open(os.path.join(self.dir, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"hello")
        self._no_parts_left()

    def test_size_cap_aborts_and_cleans_part(self):
        with self.assertRaises(FileOpError) as ctx:
            nas_files.write_stream(self.dir, "big.bin",
                                   io.BytesIO(b"x" * 1000), max_bytes=10)
        self.assertEqual(ctx.exception.code, "too_large")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "big.bin")))
        self._no_parts_left()

    def test_conflict_fail(self):
        open(os.path.join(self.dir, "a.bin"), "wb").write(b"old")
        with self.assertRaises(FileOpError) as ctx:
            nas_files.write_stream(self.dir, "a.bin", io.BytesIO(b"new"),
                                   max_bytes=100, conflict="fail")
        self.assertEqual(ctx.exception.code, "exists")
        with open(os.path.join(self.dir, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"old")

    def test_conflict_overwrite_is_atomic_swap(self):
        open(os.path.join(self.dir, "a.bin"), "wb").write(b"old")
        out = nas_files.write_stream(self.dir, "a.bin", io.BytesIO(b"new"),
                                     max_bytes=100, conflict="overwrite")
        self.assertIsNone(out["renamed_to"])
        with open(os.path.join(self.dir, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"new")
        self._no_parts_left()

    def test_conflict_rename(self):
        open(os.path.join(self.dir, "a.bin"), "wb").write(b"old")
        out = nas_files.write_stream(self.dir, "a.bin", io.BytesIO(b"new"),
                                     max_bytes=100, conflict="rename")
        self.assertEqual(out["renamed_to"], "a (1).bin")
        with open(os.path.join(self.dir, "a (1).bin"), "rb") as f:
            self.assertEqual(f.read(), b"new")
        with open(os.path.join(self.dir, "a.bin"), "rb") as f:
            self.assertEqual(f.read(), b"old")

    def test_free_space_check(self):
        class FakeVfs:
            f_bavail = 1
            f_frsize = 512
            f_blocks = 100
        real = os.statvfs
        os.statvfs = lambda p: FakeVfs()
        try:
            with self.assertRaises(FileOpError) as ctx:
                nas_files.write_stream(self.dir, "a.bin", io.BytesIO(b"x"),
                                       max_bytes=100)
            self.assertEqual(ctx.exception.code, "no_space")
        finally:
            os.statvfs = real
        self._no_parts_left()

    def test_reader_exception_cleans_part(self):
        class Boom:
            def read(self, n):
                raise IOError("client disconnected")
        with self.assertRaises(IOError):
            nas_files.write_stream(self.dir, "a.bin", Boom(), max_bytes=100)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.bin")))
        self._no_parts_left()

    def test_invalid_names_rejected(self):
        for bad in ("..", "a/b", ".pcat-upload-zzz.part"):
            with self.assertRaises(FileOpError, msg=repr(bad)):
                nas_files.write_stream(self.dir, bad, io.BytesIO(b"x"),
                                       max_bytes=100)


class SimpleOpsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mkdir_and_exists(self):
        nas_files.make_dir(self.dir, "new")
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "new")))
        with self.assertRaises(FileOpError) as ctx:
            nas_files.make_dir(self.dir, "new")
        self.assertEqual(ctx.exception.code, "exists")

    def test_rename_same_dir_only_semantics(self):
        src = os.path.join(self.dir, "a.txt")
        open(src, "w").write("x")
        dst = nas_files.rename(src, "b.txt")
        self.assertEqual(os.path.basename(dst), "b.txt")
        open(os.path.join(self.dir, "c.txt"), "w").write("y")
        with self.assertRaises(FileOpError) as ctx:
            nas_files.rename(dst, "c.txt")
        self.assertEqual(ctx.exception.code, "exists")
        with self.assertRaises(FileOpError):
            nas_files.rename(dst, "sub/dir")

    def test_list_dir_hides_parts_and_sorts_dirs_first(self):
        os.mkdir(os.path.join(self.dir, "zdir"))
        open(os.path.join(self.dir, "afile.txt"), "w").write("x")
        open(os.path.join(self.dir, ".pcat-upload-abc.part"), "w").write("x")
        out = nas_files.list_dir(self.dir)
        names = [e["name"] for e in out["entries"]]
        self.assertEqual(names, ["zdir", "afile.txt"])
        self.assertEqual(out["total"], 2)
        self.assertFalse(out["truncated"])

    def test_list_dir_pagination(self):
        for i in range(5):
            open(os.path.join(self.dir, "f{}.txt".format(i)), "w").write("x")
        out = nas_files.list_dir(self.dir, offset=0, limit=2)
        self.assertEqual(len(out["entries"]), 2)
        self.assertTrue(out["truncated"])
        out = nas_files.list_dir(self.dir, offset=4, limit=2)
        self.assertEqual(len(out["entries"]), 1)
        self.assertFalse(out["truncated"])

    def test_delete_tree_counts_and_ignores_symlink_targets(self):
        outside = tempfile.mkdtemp()
        victim = os.path.join(outside, "keep.txt")
        open(victim, "w").write("survive")
        try:
            sub = os.path.join(self.dir, "t", "sub")
            os.makedirs(sub)
            open(os.path.join(sub, "f1"), "w").write("x")
            os.symlink(outside, os.path.join(sub, "link"))
            removed = nas_files.delete_tree(os.path.join(self.dir, "t"))
            self.assertGreaterEqual(removed, 4)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "t")))
            self.assertTrue(os.path.exists(victim))  # never followed the link
        finally:
            if os.path.exists(victim):
                os.unlink(victim)
            os.rmdir(outside)

    def test_tree_measure_early_stop(self):
        for i in range(30):
            open(os.path.join(self.dir, "f{}".format(i)), "w").write("x")
        items, _size = nas_files.tree_measure(self.dir, limit=10)
        self.assertGreater(items, 10)


class ZipStreamTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.dir, "d", "sub"))
        open(os.path.join(self.dir, "d", "a.txt"), "w").write("alpha")
        open(os.path.join(self.dir, "d", "sub", "b.txt"), "w").write("beta")
        os.makedirs(os.path.join(self.dir, "d", "empty"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_roundtrip(self):
        blob = b"".join(nas_files.zip_stream([os.path.join(self.dir, "d")]))
        zf = zipfile.ZipFile(io.BytesIO(blob))
        self.assertIsNone(zf.testzip())
        names = set(zf.namelist())
        self.assertIn("d/a.txt", names)
        self.assertIn("d/sub/b.txt", names)
        self.assertIn("d/empty/", names)
        self.assertEqual(zf.read("d/a.txt"), b"alpha")
        self.assertEqual(zf.read("d/sub/b.txt"), b"beta")

    def test_zip_single_file_and_multi_path(self):
        blob = b"".join(nas_files.zip_stream([
            os.path.join(self.dir, "d", "a.txt"),
            os.path.join(self.dir, "d", "sub"),
        ]))
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = set(zf.namelist())
        self.assertIn("a.txt", names)
        self.assertIn("sub/b.txt", names)

    def test_zip_skips_symlinks(self):
        os.symlink("/etc", os.path.join(self.dir, "d", "etclink"))
        blob = b"".join(nas_files.zip_stream([os.path.join(self.dir, "d")]))
        zf = zipfile.ZipFile(io.BytesIO(blob))
        self.assertFalse(any("etclink" in n for n in zf.namelist()))

    def test_zip_streams_incrementally(self):
        # A file bigger than the chunk size must surface bytes before the
        # archive is finished — the anti-OOM property.
        big = os.path.join(self.dir, "d", "big.bin")
        with open(big, "wb") as f:
            f.write(os.urandom(1024 * 1024))
        gen = nas_files.zip_stream([os.path.join(self.dir, "d")])
        first = next(gen)
        self.assertGreater(len(first), 0)
        rest = b"".join(gen)
        zf = zipfile.ZipFile(io.BytesIO(first + rest))
        self.assertIsNone(zf.testzip())


class OpenForReadTests(unittest.TestCase):

    def test_regular_file_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "f.txt")
            open(p, "w").write("data")
            fd, size, mtime = nas_files.open_for_read(p)
            try:
                self.assertEqual(size, 4)
            finally:
                os.close(fd)
            link = os.path.join(tmp, "l.txt")
            os.symlink(p, link)
            with self.assertRaises(FileOpError):
                nas_files.open_for_read(link)


if __name__ == '__main__':
    unittest.main()
