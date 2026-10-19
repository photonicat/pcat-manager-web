#!/usr/bin/env python3
"""Regression tests for the r7901 OTA failure.

The web upgrade path used to launch sysupgrade detached and then run a fixed
~6s sequence of cosmetic progress updates before calling reboot()
unconditionally, described in the source as a "fallback". A real flash takes
30-90s, so that timer always expired while sysupgrade stage 1 was still
running. The reboot tore down ubus while stage 1 was handing off to procd:

    upgrade: Commencing upgrade. Closing all shell sessions.
    Command failed: ubus call system sysupgrade {...} (Connection failed)
    sysupgrade completed with exit code: 246

and the device came back up 9 seconds later on its old firmware. It was only
recoverable because nothing had been written to flash yet.

These tests pin the three properties that make the flash survivable:
  1. neither upgrade thread reboots the device itself,
  2. the flash is reparented away from the web service so restarting the web
     app cannot SIGKILL a flash in progress,
  3. the image is not deleted while sysupgrade is still reading it.
"""

import ast
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

SOURCE_PATH = os.path.join(
    os.path.dirname(__file__), '..', 'app', 'pcat_manager_web.py')

# The two functions that drive a flash from the web UI.
UPGRADE_FUNCS = (
    'background_firmware_upgrade_thread',
    'background_firmware_install_thread',
)


def _load_source():
    with open(SOURCE_PATH, encoding='utf-8') as f:
        return f.read()


def _function_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name}() not found in pcat_manager_web.py')


def _reboot_calls(node):
    """Every subprocess call in `node` whose argv starts with a reboot binary."""
    found = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        name = getattr(func, 'attr', None) or getattr(func, 'id', None)
        if name not in ('run', 'call', 'Popen', 'check_call', 'check_output'):
            continue
        if not sub.args:
            continue
        argv = sub.args[0]
        if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
            continue
        first = argv.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if os.path.basename(first.value) == 'reboot':
                found.append(first.value)
    return found


class TestNoPrematureReboot(unittest.TestCase):
    """The upgrade threads must never reboot the device themselves."""

    @classmethod
    def setUpClass(cls):
        cls.source = _load_source()
        cls.tree = ast.parse(cls.source)

    def test_upgrade_threads_do_not_call_reboot(self):
        # sysupgrade stage 2 reboots on success. Any reboot the web app issues
        # is therefore either redundant or -- as in the r7901 failure -- fired
        # while the flash was still running.
        for func_name in UPGRADE_FUNCS:
            node = _function_node(self.tree, func_name)
            calls = _reboot_calls(node)
            self.assertEqual(
                calls, [],
                f'{func_name}() calls reboot {calls}. sysupgrade owns the '
                f'reboot; a timer-driven reboot here killed the r7901 flash '
                f'mid-handoff (ubus "Connection failed", exit 246).')

    def test_detached_script_does_not_reboot_or_pkill(self):
        # The detached shell used to end with `pkill -f pcat-manager-web-main.py`
        # and `/sbin/reboot`. The pkill matched nothing (the app runs under
        # granian as pcat_wsgi:flask_app), and the reboot raced the flash.
        # Strip comment lines first: the explanatory notes left behind quote
        # the old `/sbin/reboot` / `pkill` text on purpose, and matching those
        # would make this test pass or fail on prose rather than on code.
        code = '\n'.join(
            line for line in self.source.splitlines()
            if not line.lstrip().startswith('#'))
        self.assertNotIn(
            '/sbin/reboot', code,
            'The detached sysupgrade script still reboots the device.')
        self.assertNotIn(
            'pkill -f "pcat-manager-web-main.py"', code,
            'Stale pkill pattern: the app is no longer started as '
            'pcat-manager-web-main.py, so this matches nothing.')

    def test_waiter_is_used_by_both_paths(self):
        for func_name in UPGRADE_FUNCS:
            node = _function_node(self.tree, func_name)
            names = {
                getattr(c.func, 'id', None)
                for c in ast.walk(node) if isinstance(c, ast.Call)
            }
            self.assertIn(
                '_wait_for_sysupgrade', names,
                f'{func_name}() must wait on the real flash rather than '
                f'running a fixed timer.')


class TestFlashSurvivesWebRestart(unittest.TestCase):
    """The flash must outlive the service that launched it."""

    @classmethod
    def setUpClass(cls):
        cls.source = _load_source()

    def test_launched_via_start_stop_daemon(self):
        # /etc/init.d/pcat-manager-web stop() walks /proc/*/stat for children
        # of the pidfile PID and SIGKILLs all of them. setsid() alone does not
        # break that parent link -- only reparenting to init does.
        self.assertIn(
            "'start-stop-daemon'", self.source,
            'sysupgrade must be launched with start-stop-daemon -b so it is '
            'reparented to init; otherwise the init script\'s stop() SIGKILLs '
            'the running flash as a child of the web server.')
        self.assertIn(
            "'-b'", self.source,
            'start-stop-daemon needs -b to background and reparent the flash.')
        # -n is required for -S to actually start: without it, -x /bin/sh
        # matches any running shell and busybox refuses with "already running".
        self.assertIn(
            "'-n', 'pcatflash'", self.source,
            "start-stop-daemon -S -x /bin/sh matches any running shell and "
            "refuses to launch; -n must narrow the match.")

    def test_launch_failure_is_detected(self):
        # A refused launch must raise, not be reported as a successful flash.
        tree = ast.parse(self.source)
        node = _function_node(tree, 'background_firmware_upgrade_thread')
        src = ast.get_source_segment(self.source, node) or ''
        self.assertIn(
            'launch_rc', src,
            'The start-stop-daemon exit status must be checked, otherwise a '
            'flash that never started is reported as success.')

    def test_no_bare_setsid_launch(self):
        # preexec_fn=os.setsid was the old detach mechanism and is not enough.
        self.assertNotIn(
            'preexec_fn=os.setsid', self.source,
            'preexec_fn=os.setsid leaves the flash a PID-child of the web '
            'service, where init stop() will kill it.')


class TestImageNotDeletedDuringFlash(unittest.TestCase):
    """The firmware image must stay put until the flash is done."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(_load_source())

    def test_install_thread_does_not_unlink_image(self):
        # background_firmware_install_thread() used to os.remove(file_path)
        # immediately after launching the detached flash -- while sysupgrade
        # was still reading that image. Deleting it is only acceptable behind
        # a _sysupgrade_running() guard, i.e. once the flash is known to be
        # over (the error path).
        node = _function_node(self.tree, 'background_firmware_install_thread')

        def _guarded(unlink_call):
            """True if this unlink sits under an `if _sysupgrade_running(...)`."""
            for outer in ast.walk(node):
                if not isinstance(outer, ast.If):
                    continue
                guards = {
                    getattr(c.func, 'id', None)
                    for c in ast.walk(outer.test) if isinstance(c, ast.Call)
                }
                if '_sysupgrade_running' not in guards:
                    continue
                for stmt in ast.walk(outer):
                    if stmt is unlink_call:
                        return True
            return False

        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if getattr(sub.func, 'attr', None) not in ('remove', 'unlink'):
                continue
            target = sub.args[0] if sub.args else None
            if getattr(target, 'id', None) != 'file_path':
                continue
            self.assertTrue(
                _guarded(sub),
                'The firmware image is deleted without first checking that '
                'sysupgrade has finished. /tmp is tmpfs and is cleared by the '
                'reboot anyway.')


class TestWaiterBehaviour(unittest.TestCase):
    """_wait_for_sysupgrade() reports failure instead of rebooting."""

    def setUp(self):
        self.mock_modules = {
            'pcat_logger': MagicMock(),
            'pcat_config': MagicMock(),
            'pcat_util.user_auth': MagicMock(),
            'pcat_util.pcat_logger': MagicMock(),
            'pcat_util.command_parser': MagicMock(),
            'pcat_util.hardware_util': MagicMock(),
            'pcat_util.isp_dict': MagicMock(),
            'pc_socket_client': MagicMock(),
            'pc_modem_client': MagicMock(),
            'pc_sms_client': MagicMock(),
            'pc_screen_client': MagicMock(),
            'app': MagicMock(),
            'flask': MagicMock(),
        }
        self.module_patch = patch.dict('sys.modules', self.mock_modules)
        self.module_patch.start()
        import pcat_manager_web
        self.module = pcat_manager_web

    def tearDown(self):
        self.module_patch.stop()

    def _write(self, tmpdir, name, text):
        path = os.path.join(tmpdir, name)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_reports_error_on_failed_flash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            status = self._write(
                tmp, 'status.json',
                '{"stage": "error", "progress": 0, "exit_code": 246}')
            pid = os.path.join(tmp, 'nonexistent.pid')
            with patch.object(self.module, 'update_firmware_upgrade_status') as upd:
                result = self.module._wait_for_sysupgrade(status, pid, timeout=10)
            self.assertFalse(result, 'A failed flash must report failure.')
            stages = [c.args[0] for c in upd.call_args_list if c.args]
            self.assertIn('error', stages)

    def test_never_reboots(self):
        """The waiter must not reboot on any path, including timeout."""
        node = _function_node(ast.parse(_load_source()), '_wait_for_sysupgrade')
        self.assertEqual(
            _reboot_calls(node), [],
            '_wait_for_sysupgrade() must never reboot: if the flash did not '
            'finish, the running firmware is the intact one.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
