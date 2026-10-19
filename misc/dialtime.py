#!/usr/bin/env python3
"""Times call setup as the phone page experiences it, on-device.

Phases, matching what phone.html actually does in makeCall():
  dial   POST /api/v1/voice_call/make -- the page awaits this with the button
         spinning, so its whole duration is dead time the user sees
  seen   the 1Hz status poll first reporting the call
  active status reporting stat=0 (connected), which is when startAudio() fires

Also samples bare /status latency first, which is the AT port's lock contention
showing through: every status poll takes the same serial_lock a dial needs.

  dialtime.py <number> [--repeat N] [--hangup-after S]
"""
import argparse
import json
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, "/tmp")
sys.path.insert(0, "/usr/share/pcat-manager-web")
import callctl                                          # noqa: E402

CTX = ssl._create_unverified_context()


def get(path):
    t = time.monotonic()
    with urllib.request.urlopen("https://127.0.0.1:9443" + path,
                                context=CTX, timeout=90) as r:
        d = json.loads(r.read())
    return d, (time.monotonic() - t) * 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("number")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--hangup-after", type=float, default=3.0)
    a = ap.parse_args()

    print("idle /status latency (= AT lock contention):")
    for i in range(5):
        _, ms = get("/api/v1/voice_call/status")
        print("   %6.0f ms" % ms)
        time.sleep(1)

    for n in range(a.repeat):
        print("\n=== dial %d -> %s" % (n + 1, a.number))
        t0 = time.monotonic()
        st, body = callctl.call("POST", "/api/v1/voice_call/make",
                                {"number": a.number})
        t_dial = time.monotonic() - t0
        print("   dial POST returned after %6.2f s  (%s)" % (t_dial, st))

        seen = active = None
        while time.monotonic() - t0 < 45:
            d, ms = get("/api/v1/voice_call/status")
            calls = d.get("calls", [])
            el = time.monotonic() - t0
            if calls and seen is None:
                seen = el
                print("   call visible in status after %6.2f s  (status=%s, "
                      "poll took %.0f ms)" % (el, calls[0].get("status"), ms))
            if calls and calls[0].get("status") == "active" and active is None:
                active = el
                print("   status=active after           %6.2f s" % el)
                break
            time.sleep(1)                    # the page's poll cadence

        if active is not None:
            time.sleep(a.hangup_after)
        print("   hangup:", callctl.call("POST", "/api/v1/voice_call/hangup", {})[0])
        print("   ---- dial %.2fs | visible %s | active %s"
              % (t_dial,
                 "%.2fs" % seen if seen else "never",
                 "%.2fs" % active if active else "never"))
        if n + 1 < a.repeat:
            time.sleep(8)


if __name__ == "__main__":
    sys.exit(main())
