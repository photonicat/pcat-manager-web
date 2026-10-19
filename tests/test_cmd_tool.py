#!/usr/bin/env python3
"""Unit tests for app/cmd_tool.py -- the fire-and-forget command queue thread.

CmdTool is used to batch system commands (uci commits, service restarts) and
run them off the request thread. The properties that matter operationally:
commands run in order, one failing command must not abort the rest of the
batch (a failed `wifi reload` must still let the following `service restart`
run), and an empty queue must be a no-op. subprocess is always patched --
these tests must never run real system commands on the host.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_cmd_tool`.
"""

import os
import sys
import unittest
from unittest.mock import patch, call

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

import cmd_tool  # noqa: E402


class PrepareTests(unittest.TestCase):
    def test_new_instance_has_empty_queue(self):
        t = cmd_tool.CmdTool()
        self.assertEqual(t.commands_length(), 0)

    def test_prepare_appends_in_order(self):
        # Order is the whole point: `uci commit` must precede the reload that
        # consumes it.
        t = cmd_tool.CmdTool()
        t.prepare(["uci", "commit", "wireless"])
        t.prepare(["wifi", "reload"])
        self.assertEqual(t.commands_length(), 2)
        self.assertEqual(t.commands,
                         [["uci", "commit", "wireless"], ["wifi", "reload"]])

    def test_prepare_ignores_falsy_commands(self):
        # Callers build command arrays conditionally; a None/[] slipping in
        # must not enqueue a bogus subprocess.run(None) that would raise later.
        t = cmd_tool.CmdTool()
        t.prepare(None)
        t.prepare([])
        t.prepare("")
        self.assertEqual(t.commands_length(), 0)


class ExecuteTests(unittest.TestCase):
    def test_empty_queue_is_a_noop(self):
        t = cmd_tool.CmdTool()
        with patch.object(cmd_tool.subprocess, "run") as run:
            t.execute()
        run.assert_not_called()

    def test_runs_each_command_in_order(self):
        t = cmd_tool.CmdTool()
        t.prepare(["uci", "commit"])
        t.prepare(["wifi", "reload"])
        # time.sleep is patched: 0.2s per command is deliberate pacing on the
        # device but pure dead time in a test run.
        with patch.object(cmd_tool.subprocess, "run") as run, \
             patch.object(cmd_tool.time, "sleep") as sleep:
            t.execute()
        self.assertEqual(run.call_args_list,
                         [call(["uci", "commit"]), call(["wifi", "reload"])])
        self.assertEqual(sleep.call_count, 2)

    def test_failed_command_does_not_abort_the_batch(self):
        # The consequence being guarded: if `wifi reload` explodes, the
        # follow-up service restart must still run or the device is left in a
        # half-applied config until reboot.
        t = cmd_tool.CmdTool()
        t.prepare(["bad-cmd"])
        t.prepare(["good-cmd"])
        with patch.object(cmd_tool.subprocess, "run",
                          side_effect=[OSError("no such file"), None]) as run, \
             patch.object(cmd_tool.time, "sleep"):
            t.execute()  # must not raise
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[-1], call(["good-cmd"]))


class RunTests(unittest.TestCase):
    def test_run_executes_the_queue(self):
        # Thread#start dispatches to run(); calling run() directly keeps the
        # test single-threaded while proving start() would drain the queue.
        t = cmd_tool.CmdTool()
        t.prepare(["echo", "hi"])
        with patch.object(cmd_tool.subprocess, "run") as run, \
             patch.object(cmd_tool.time, "sleep"):
            t.run()
        run.assert_called_once_with(["echo", "hi"])


if __name__ == "__main__":
    unittest.main()
