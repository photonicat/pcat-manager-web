#!/usr/bin/env python3
"""Advisory LLM review of hil/ checks for one specific defect class.

Five bugs in this harness were the same mistake: reading the ABSENCE or
NOT-YET-CONFIGURED state of something as a NEGATIVE FACT about it. A missing
`modem_installed` read as "no modem" on a board registered to 5G; a 404 read
as "no auth check"; a missing `theme_accent` read as "cannot test
persistence"; an unconfigured LXC read as a broken LXC; a missing
`modem_valid` read as "modem not answering".

`tests/test_hil_absence.py` catches these mechanically by replaying real
device responses with fields removed. That is the gate. This is the
complement: the fuzzer can only mutate fields that appear in the captured
fixture, so it is blind to a field the device has never sent and to intent-
level mistakes in code paths the replay cannot reach. A reviewer reading the
source can see those.

Deliberately advisory, never a gate. Measured on the real lxc_health bug:
qwen3.6:35b and gemma4:31b both identified it exactly; qwen3-coder:30b named
the wrong condition and asserted the buggy behaviour was correct. A reviewer
that is confidently wrong a third of the time is useful for finding
candidates and useless as a pass/fail.

    python3 tools/review_checks.py                    # every check module
    python3 tools/review_checks.py hil/checks/nas.py  # just one
    OLLAMA_HOST=http://box:11434 python3 tools/review_checks.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://192.168.150.150:11434")
# Verified on the known bug; qwen3-coder:30b was wrong on it, so it is not the
# default despite being the "coder" model.
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.6:35b")
TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))

PROMPT = """You review test-harness code for ONE defect class, and report nothing else.

THE DEFECT: treating the ABSENCE of a field, endpoint or feature — or a
NOT-YET-CONFIGURED state — as a NEGATIVE FACT about it. A missing field means
UNKNOWN, not "no". An endpoint that 404s means this firmware build lacks the
feature, not that the feature is broken. A subsystem that has never been set
up is not a broken subsystem.

Correct handling: report SKIP (with a reason) when something is absent or
unconfigured. Reserve FAIL for a thing that is present and genuinely broken.

Known real examples from this codebase:
- `if not basic.get("modem_installed")` -> skip. The field is absent on older
  firmware, so a working 5G modem was reported as "no modem".
- `result.expect(bool(entry.get("ok")), ...)` where a missing `ok` key, or an
  `ok: false` carrying `fixable: true`, was reported as a FAILURE — but that
  is a factory-fresh board awaiting one-click setup.

Below is one Python module of checks. For EACH function, decide whether it can
turn an absence or unconfigured state into a FAIL or a misleading SKIP.

Reply as a compact list. One line per finding:
  FUNCTION | LINE-ISH | what absence is misread | what it should do
If a function is fine, do not mention it. If nothing is wrong, reply exactly:
NO FINDINGS

MODULE %s:
```python
%s
```"""


def review(path, source):
    body = json.dumps({
        "model": MODEL,
        "prompt": PROMPT % (os.path.basename(path), source),
        "stream": False,
        # These are thinking models: without this the reasoning block eats the
        # token budget and the visible answer comes back empty.
        "think": False,
        "options": {"temperature": 0.1, "num_predict": 1600},
    }).encode()
    request = urllib.request.Request(OLLAMA + "/api/generate", body,
                                     {"Content-Type": "application/json"})
    try:
        payload = json.load(urllib.request.urlopen(request, timeout=TIMEOUT))
    except urllib.error.URLError as exc:
        return "UNREACHABLE: %s (%s)" % (exc, OLLAMA)
    except Exception as exc:
        return "ERROR: %s" % exc

    answer = (payload.get("response") or "").strip()
    if "</think>" in answer:
        answer = answer.split("</think>")[-1].strip()
    return answer or "(empty response)"


def main(argv):
    targets = argv[1:]
    if not targets:
        root = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "hil", "checks")
        targets = sorted(os.path.join(root, name) for name in os.listdir(root)
                         if name.endswith(".py") and name not in
                         ("__init__.py", "base.py"))

    print("reviewing %d module(s) with %s at %s\n" % (len(targets), MODEL, OLLAMA))
    findings = 0
    for path in targets:
        with open(path) as handle:
            source = handle.read()
        print("=" * 70)
        print(path)
        print("=" * 70)
        answer = review(path, source)
        print(answer)
        print()
        if "NO FINDINGS" not in answer.upper() and not answer.startswith(
                ("UNREACHABLE", "ERROR")):
            findings += 1

    print("-" * 70)
    print("%d module(s) with candidate findings. ADVISORY ONLY — verify each "
          "against the device before changing a check; this reviewer has been "
          "confidently wrong." % findings)
    # Always 0: this must never gate CI.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
