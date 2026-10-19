#!/usr/bin/env python3
"""Runs tests/pollbus_test.js as part of the Python suite.

app/static/pollbus.js is the client half of the socket.io replacement (issue
#113) and carries the fiddly part of the protocol -- cursor tracking, the
rewind when a room is joined, and de-duplication of anything that rewind
replays. That logic deserves tests, but the repo has no JS test tooling and
adding a framework for one file is not worth it, so the JS test is a plain
node script and this wrapper surfaces its result.

Skips (rather than fails) where node is unavailable, matching the convention
in hil/: a missing tool is "not fitted", not "broken".
"""

import os
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "tests", "pollbus_test.js")


class PollBusJsTests(unittest.TestCase):
    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_pollbus_js_suite_passes(self):
        result = subprocess.run(
            ["node", SCRIPT], cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=120)
        if result.returncode != 0:
            self.fail("pollbus.js tests failed:\n%s\n%s"
                      % (result.stdout, result.stderr))


if __name__ == "__main__":
    unittest.main()
