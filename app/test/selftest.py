#!/usr/bin/env python3

"""Run everything that can be checked without a device, and say what could not.

    python3 app/test/selftest.py          # everything
    python3 app/test/selftest.py -v       # full output from each test module

Two halves, because they catch different things:

* a sweep over every app source for problems that only surface when the file
  is compiled on the *device*. That matters because the device runs Python
  3.13 while dev machines are usually on 3.11/3.12, the deploy ships .pyc, and
  the init script daemonises and swallows the traceback — so a file that only
  warns here can be the reason the web UI never comes up there.
* the unittest modules in this directory, covering the pure logic that is
  risky and device-independent (config surgery, storage recovery, daemon
  reply parsing).

Each test module runs in its own subprocess. That is not ceremony: several of
them stub device-only peers by writing into sys.modules, so importing two of
them into one interpreter lets one module's stubs answer another module's
imports. Discovery in-process fails for exactly that reason.

Modules that are device tools rather than tests (they talk to
/tmp/pcat-manager.sock) are skipped by name, and reported — never silently.

Exits non-zero if anything fails, so it works as a pre-deploy gate.
"""

import pathlib
import subprocess
import sys
import warnings

TEST_DIR = pathlib.Path(__file__).parent.resolve()
APP_DIR = TEST_DIR.parent

# Not ours and shipped as-is.
SKIP_DIRS = ("pysms_tool",)


def compile_sweep():
    """Compile every app source. Returns (checked, [problem, ...])."""
    problems, checked = [], 0
    for path in sorted(APP_DIR.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        checked += 1
        rel = path.relative_to(APP_DIR)
        try:
            source = path.read_text()
        except OSError as e:
            problems.append("{}: unreadable ({})".format(rel, e))
            continue
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(source, str(path), "exec")
            except SyntaxError as e:
                problems.append("{}:{}: SyntaxError: {}".format(
                    rel, e.lineno, e.msg))
                continue
            for w in caught:
                # An invalid escape sequence is a warning today and a
                # SyntaxError in a future release; on the device's newer
                # Python it is already noisy at every compile.
                if "escape sequence" in str(w.message):
                    problems.append("{}:{}: {}".format(rel, w.lineno, w.message))
    return checked, problems


def is_unit_test(path):
    """A test module drives unittest; anything else in here is a hand-run
    device tool (needs the pcat-manager socket) and cannot pass off-device."""
    try:
        return "import unittest" in path.read_text()
    except OSError:
        return False


def run_tests(verbose):
    passed, failed, skipped = [], [], []
    for path in sorted(TEST_DIR.glob("test_*.py")):
        if not is_unit_test(path):
            skipped.append(path.name)
            continue
        proc = subprocess.run([sys.executable, str(path)],
                              capture_output=True, text=True)
        # unittest writes its summary to stderr
        tail = [ln for ln in (proc.stderr or "").splitlines()
                if ln.startswith("Ran ")]
        count = tail[-1] if tail else "?"
        if proc.returncode == 0:
            passed.append((path.name, count))
            print("  ok    {:<40} {}".format(path.name, count))
        else:
            failed.append(path.name)
            print("  FAIL  {:<40} {}".format(path.name, count))
        if verbose or proc.returncode != 0:
            for line in (proc.stdout or "").splitlines():
                print("        " + line)
            for line in (proc.stderr or "").splitlines():
                print("        " + line)
    return passed, failed, skipped


def main():
    verbose = "-v" in sys.argv

    print("== source sweep ==")
    checked, problems = compile_sweep()
    for p in problems:
        print("  FAIL  " + p)
    print("  {} file(s) compiled, {} problem(s)".format(checked, len(problems)))

    print("\n== unit tests ==")
    passed, failed, skipped = run_tests(verbose)
    for name in skipped:
        print("  skip  {:<40} device tool, needs /tmp/pcat-manager.sock"
              .format(name))

    ok = not failed and not problems
    print("\n== {} == {} module(s) passed, {} failed, {} skipped".format(
        "PASS" if ok else "FAIL", len(passed), len(failed), len(skipped)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
