#!/usr/bin/env python3
"""Unit tests for the remaining nas_files/nas_jobs surface.

Covers what the first-pass NAS tests left uncovered: dedup_name's collision
walk, same-filesystem move semantics (and the cross-device refusal that
routes a move to a copy job), the copy_tree_runner used by every
cross-volume move/copy, volume_of, and the nas_jobs listing/count helpers.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_files_extra`.
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))

from pcat_util import nas_files  # noqa: E402
from pcat_util.nas_files import FileOpError  # noqa: E402


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def _read(path):
    with open(path) as f:
        return f.read()


class RecordingHandle:
    """Minimal nas_jobs.JobHandle stand-in for runner tests."""

    def __init__(self, cancel_after=None):
        self.progress = []
        self.cancel_after = cancel_after
        self.checks = 0

    def check_cancel(self):
        self.checks += 1
        if self.cancel_after is not None and self.checks > self.cancel_after:
            raise RuntimeError("cancelled")

    def set_progress(self, **kw):
        self.progress.append(kw)

    def log(self, line):
        pass


class DedupAndMoveTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.realpath(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedup_name_walks_to_first_free_slot(self):
        _write(os.path.join(self.dir, "a.jpg"), "x")
        self.assertEqual(nas_files.dedup_name(self.dir, "a.jpg"), "a (1).jpg")
        open(os.path.join(self.dir, "a (1).jpg"), "w").write("x")
        self.assertEqual(nas_files.dedup_name(self.dir, "a.jpg"), "a (2).jpg")

    def test_dedup_name_handles_no_extension(self):
        _write(os.path.join(self.dir, "README"), "x")
        self.assertEqual(nas_files.dedup_name(self.dir, "README"),
                         "README (1)")

    def test_same_filesystem(self):
        a = os.path.join(self.dir, "a")
        b = os.path.join(self.dir, "b")
        with open(a, "w") as f:
            f.write("x")
        with open(b, "w") as f:
            f.write("y")
        self.assertTrue(nas_files.same_filesystem(a, b))

    def test_move_entry_same_dir(self):
        src = os.path.join(self.dir, "f.txt")
        dst_dir = os.path.join(self.dir, "sub")
        os.mkdir(dst_dir)
        open(src, "w").write("data")
        out = nas_files.move_entry(src, dst_dir)
        self.assertEqual(out, os.path.join(dst_dir, "f.txt"))
        self.assertFalse(os.path.exists(src))

    def test_move_entry_refuses_existing_target(self):
        src = os.path.join(self.dir, "f.txt")
        dst_dir = os.path.join(self.dir, "sub")
        os.mkdir(dst_dir)
        open(src, "w").write("data")
        _write(os.path.join(dst_dir, "f.txt"), "other")
        with self.assertRaises(FileOpError) as ctx:
            nas_files.move_entry(src, dst_dir)
        self.assertEqual(ctx.exception.code, "exists")

    def test_move_entry_cross_device_is_flagged(self):
        # The route layer turns this code into a copy job instead of failing.
        src = os.path.join(self.dir, "f.txt")
        dst_dir = os.path.join(self.dir, "sub")
        os.mkdir(dst_dir)
        with open(src, "w") as f:
            f.write("data")
        err = OSError()
        err.errno = 18  # EXDEV
        with patch.object(nas_files.os, 'rename', side_effect=err):
            with self.assertRaises(FileOpError) as ctx:
                nas_files.move_entry(src, dst_dir)
        self.assertEqual(ctx.exception.code, "cross_device")

    def test_volume_of(self):
        vol = nas_files.volume_of(self.dir)
        self.assertIn("free_bytes", vol)
        self.assertIn("total_bytes", vol)
        self.assertGreater(vol["total_bytes"], 0)


class CopyRunnerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.src = os.path.join(self.root, "src")
        self.dst = os.path.join(self.root, "dst")
        os.makedirs(os.path.join(self.src, "tree", "sub"))
        os.makedirs(self.dst)
        _write(os.path.join(self.src, "tree", "a.txt"), "alpha")
        _write(os.path.join(self.src, "tree", "sub", "b.txt"), "beta")
        _write(os.path.join(self.src, "loose.txt"), "loose")

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_tree_preserves_structure(self):
        handle = RecordingHandle()
        nas_files.copy_tree_runner(
            handle, [os.path.join(self.src, "tree")], self.dst)
        self.assertEqual(
            _read(os.path.join(self.dst, "tree", "a.txt")), "alpha")
        self.assertEqual(
            _read(os.path.join(self.dst, "tree", "sub", "b.txt")),
            "beta")
        self.assertTrue(os.path.exists(os.path.join(self.src, "tree")))

    def test_copy_single_file(self):
        handle = RecordingHandle()
        nas_files.copy_tree_runner(
            handle, [os.path.join(self.src, "loose.txt")], self.dst)
        self.assertEqual(
            _read(os.path.join(self.dst, "loose.txt")), "loose")

    def test_move_deletes_sources(self):
        handle = RecordingHandle()
        nas_files.copy_tree_runner(
            handle, [os.path.join(self.src, "tree")], self.dst,
            delete_src=True)
        self.assertFalse(os.path.exists(os.path.join(self.src, "tree")))
        self.assertTrue(os.path.exists(os.path.join(self.dst, "tree",
                                                    "a.txt")))

    def test_progress_reports_totals(self):
        handle = RecordingHandle()
        nas_files.copy_tree_runner(
            handle, [os.path.join(self.src, "tree")], self.dst)
        first = handle.progress[0]
        self.assertEqual(first["items_total"], 2)
        self.assertEqual(first["bytes_total"], len("alpha") + len("beta"))
        self.assertEqual(handle.progress[-1]["pct"], 100)

    def test_cancel_stops_early(self):
        handle = RecordingHandle(cancel_after=1)
        with self.assertRaises(RuntimeError):
            nas_files.copy_tree_runner(
                handle, [os.path.join(self.src, "tree")], self.dst)

    def test_conflicting_names_are_renamed_not_clobbered(self):
        os.makedirs(os.path.join(self.dst, "tree"))
        _write(os.path.join(self.dst, "tree", "a.txt"), "original")
        handle = RecordingHandle()
        nas_files.copy_tree_runner(
            handle, [os.path.join(self.src, "tree")], self.dst)
        self.assertEqual(
            _read(os.path.join(self.dst, "tree", "a.txt")), "original")
        self.assertTrue(
            os.path.exists(os.path.join(self.dst, "tree", "a (1).txt")))


class JobHelperTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_jobs
        self.nj = nas_jobs
        self.tmpdir = tempfile.TemporaryDirectory()
        self.persist_patch = patch.object(
            nas_jobs, 'PERSIST_PATH',
            os.path.join(self.tmpdir.name, "jobs.json"))
        self.persist_patch.start()
        self.nj._jobs.clear()
        self.nj.set_emitter(None)

    def tearDown(self):
        self.nj._jobs.clear()
        self.persist_patch.stop()
        self.tmpdir.cleanup()
        self.module_patch.stop()

    def _wait(self, job_id, state, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.nj.get(job_id)
            if job and job["state"] == state:
                return True
            time.sleep(0.01)
        return False

    def test_list_jobs_newest_first(self):
        a, _ = self.nj.submit("copy", "A", "A", lambda h: None)
        self.assertTrue(self._wait(a, "done"))
        b, _ = self.nj.submit("copy", "B", "B", lambda h: None)
        self.assertTrue(self._wait(b, "done"))
        ids = [j["id"] for j in self.nj.list_jobs()]
        self.assertEqual(ids[0], b)

    def test_active_count(self):
        self.assertEqual(self.nj.active_count(), 0)
        import threading
        release = threading.Event()
        jid, _ = self.nj.submit("copy", "C", "C", lambda h: release.wait(5))
        self.assertTrue(self._wait(jid, "running"))
        self.assertEqual(self.nj.active_count(), 1)
        release.set()
        self.assertTrue(self._wait(jid, "done"))
        self.assertEqual(self.nj.active_count(), 0)

    def test_public_shape_has_every_contract_field(self):
        jid, _ = self.nj.submit("copy", "C", "复制", lambda h: None,
                                meta={"dev": "/dev/sda"})
        self.assertTrue(self._wait(jid, "done"))
        job = self.nj.get(jid)
        for field in ("id", "kind", "state", "pct", "label", "label_cn",
                      "message", "bytes_done", "bytes_total", "items_done",
                      "items_total", "meta", "log_tail", "created",
                      "started", "finished", "error", "cancellable"):
            self.assertIn(field, job, field)
        # Internals must never leak into the API shape.
        for internal in ("_cancel", "_proc", "_log", "_excl"):
            self.assertNotIn(internal, job)


if __name__ == '__main__':
    unittest.main()
