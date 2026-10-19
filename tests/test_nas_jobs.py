#!/usr/bin/env python3
"""Unit tests for the NAS job engine (pcat_util/nas_jobs).

The engine is the one table every long NAS operation runs through, so these
pin the lifecycle contract the UI depends on: state transitions, the
exclusive_key refusal (with WHICH job conflicts), destructive-vs-copy
concurrency caps, cancellation, the persisted reattach story, and the
interrupted-by-restart marking.

Run via `python3 tests/run_tests.py` or
`python3 -m unittest tests.test_nas_jobs`.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app'))


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class JobEngineTests(unittest.TestCase):

    def setUp(self):
        self.module_patch = patch.dict('sys.modules', {
            'pcat_logger': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
        })
        self.module_patch.start()
        from pcat_util import nas_jobs
        self.nj = nas_jobs
        self.tmpdir = tempfile.TemporaryDirectory()
        self.persist = os.path.join(self.tmpdir.name, "jobs.json")
        self.persist_patch = patch.object(nas_jobs, 'PERSIST_PATH',
                                          self.persist)
        self.persist_patch.start()
        self.nj._jobs.clear()
        self.emitted = []
        self.nj.set_emitter(self.emitted.append)

    def tearDown(self):
        self.nj._jobs.clear()
        self.nj.set_emitter(None)
        self.persist_patch.stop()
        self.tmpdir.cleanup()
        self.module_patch.stop()

    def test_success_lifecycle_and_emit(self):
        def runner(h):
            h.set_progress(pct=50, message="halfway")
            h.log("did a thing")
        jid, err = self.nj.submit("copy", "Copy stuff", "复制", runner)
        self.assertIsNone(err)
        self.assertTrue(wait_for(
            lambda: self.nj.get(jid)["state"] == "done"))
        job = self.nj.get(jid)
        self.assertEqual(job["pct"], 100)
        self.assertIn("did a thing", job["log_tail"])
        self.assertIsNotNone(job["finished"])
        states = [e["state"] for e in self.emitted if e["id"] == jid]
        self.assertIn("running", states)
        self.assertIn("done", states)

    def test_runner_exception_becomes_error(self):
        def runner(h):
            raise RuntimeError("disk on fire")
        jid, _ = self.nj.submit("format", "Format", "格式化", runner)
        self.assertTrue(wait_for(
            lambda: self.nj.get(jid)["state"] == "error"))
        self.assertEqual(self.nj.get(jid)["error"], "disk on fire")

    def test_exclusive_key_conflict_names_the_holder(self):
        release = threading.Event()

        def runner(h):
            release.wait(5)
        jid, err = self.nj.submit("format", "F", "F", runner,
                                  exclusive_key="disk:sda")
        self.assertIsNone(err)
        jid2, err2 = self.nj.submit("resize", "R", "R", lambda h: None,
                                    exclusive_key="disk:sda")
        self.assertIsNone(jid2)
        self.assertEqual(err2["code"], "busy")
        self.assertEqual(err2["conflict"], jid)
        # A different disk is fine but hits the destructive cap instead.
        jid3, err3 = self.nj.submit("format", "F2", "F2", lambda h: None,
                                    exclusive_key="disk:sdb")
        self.assertIsNone(jid3)
        self.assertEqual(err3["code"], "busy_destructive")
        release.set()
        self.assertTrue(wait_for(
            lambda: self.nj.get(jid)["state"] == "done"))

    def test_copy_class_cap(self):
        release = threading.Event()

        def runner(h):
            release.wait(5)
        a, _ = self.nj.submit("copy", "A", "A", runner)
        b, _ = self.nj.submit("copy", "B", "B", runner)
        c, err = self.nj.submit("copy", "C", "C", runner)
        self.assertIsNone(c)
        self.assertEqual(err["code"], "busy_queue")
        # The destructive lane is independent of the copy lane.
        d, errd = self.nj.submit("format", "D", "D", runner,
                                 exclusive_key="disk:x")
        self.assertIsNone(errd)
        release.set()
        for jid in (a, b, d):
            self.assertTrue(wait_for(
                lambda j=jid: self.nj.get(j)["state"] == "done"))

    def test_cancel_sets_flag_and_state(self):
        started = threading.Event()

        def runner(h):
            started.set()
            for _ in range(500):
                h.check_cancel()
                time.sleep(0.01)
        jid, _ = self.nj.submit("copy", "C", "C", runner)
        self.assertTrue(started.wait(5))
        out = self.nj.cancel(jid)
        self.assertEqual(out["code"], "ok")
        self.assertTrue(wait_for(
            lambda: self.nj.get(jid)["state"] == "cancelled"))
        # cancel on finished job
        self.assertEqual(self.nj.cancel(jid)["code"], "not_running")
        self.assertEqual(self.nj.cancel("j-nope")["code"], "not_found")

    def test_persistence_roundtrip_and_interrupted_marking(self):
        def runner(h):
            h.set_progress(pct=10)
        jid, _ = self.nj.submit("rsync", "Sync", "同步", runner)
        self.assertTrue(wait_for(
            lambda: self.nj.get(jid)["state"] == "done"))
        with open(self.persist) as f:
            snapshot = json.load(f)
        self.assertTrue(any(j["id"] == jid for j in snapshot))

        # Simulate a crash mid-job: hand-write a running job, then startup().
        snapshot.append({
            "id": "j-dead", "kind": "format", "state": "running",
            "pct": 40, "label": "Format X", "label_cn": "格式化 X",
            "message": "", "bytes_done": 0, "bytes_total": 0,
            "items_done": 0, "items_total": 0, "meta": {}, "log_tail": [],
            "created": time.time(), "started": time.time(),
            "finished": None, "error": None, "cancellable": True,
        })
        with open(self.persist, "w") as f:
            json.dump(snapshot, f)
        self.nj._jobs.clear()
        self.nj.startup()
        dead = self.nj.get("j-dead")
        self.assertEqual(dead["state"], "error")
        self.assertEqual(dead["error"], "interrupted by restart")
        self.assertEqual(self.nj.get(jid)["state"], "done")

    def test_dismiss(self):
        jid, _ = self.nj.submit("copy", "C", "C", lambda h: None)
        self.assertTrue(wait_for(
            lambda: self.nj.get(jid)["state"] == "done"))
        self.assertEqual(self.nj.dismiss(jid)["code"], "ok")
        self.assertIsNone(self.nj.get(jid))

    def test_watch_subprocess_streams_and_exits(self):
        lines = []

        def runner(h):
            rc = h.watch_subprocess(
                [sys.executable, "-c", "print('alpha'); print('beta')"],
                line_cb=lines.append)
            if rc != 0:
                raise RuntimeError("rc={}".format(rc))
        jid, _ = self.nj.submit("rsync", "S", "S", runner)
        self.assertTrue(wait_for(
            lambda: self.nj.get(jid)["state"] == "done"))
        self.assertEqual(lines, ["alpha", "beta"])
        self.assertIn("beta", self.nj.get(jid)["log_tail"])


if __name__ == '__main__':
    unittest.main()
