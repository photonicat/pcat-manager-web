#!/usr/bin/env python3
"""Measures downlink arrival jitter over the real network, from a client.

The on-device probe (calat.py) reads the daemon over loopback, so it cannot see
what WiFi does to the 20ms chunk cadence -- and that is exactly what sizes the
browser's playout cushion in phone.html (FLOOR/CEIL in playChunk). Run this from
a laptop on the same WiFi as the device.

Needs no call on a UAC-card box (RM500Q-GL): the card clocks whenever it is
open, so the stream delivers silence with real timing. On a PCM/SAI box the
modem only clocks during a call, so put one up first.

  wifi_jitter.py <host> <token> [--secs 30]
"""
import argparse
import http.client
import ssl
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("token")
    ap.add_argument("--port", type=int, default=9444)
    ap.add_argument("--secs", type=float, default=30)
    a = ap.parse_args()

    c = http.client.HTTPSConnection(a.host, a.port,
                                    context=ssl._create_unverified_context(),
                                    timeout=30)
    c.request("GET", "/api/v1/voice_call/audio/stream?token=" + a.token)
    r = c.getresponse()
    print("stream ->", r.status)
    if r.status != 200:
        print(r.read()[:200])
        return 1

    gaps = []
    total = 0
    t_prev = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.secs:
        b = r.read1(8192)
        if not b:
            break
        t = time.monotonic()
        total += len(b)
        if t_prev is not None:
            gaps.append((t - t_prev) * 1000)
        t_prev = t
    c.close()

    if not gaps:
        print("no data")
        return 1
    g = sorted(gaps)
    n = len(g)
    dur = time.monotonic() - t0
    print("bytes %d = %.2fs of audio in %.2fs wall (%.4fx)"
          % (total, total / 2 / 16000, dur, total / 2 / 16000 / dur))
    print("reads %d, inter-arrival gap ms:" % n)
    for label, v in (("min", g[0]), ("p50", g[n // 2]), ("p90", g[int(n * .9)]),
                     ("p99", g[min(n - 1, int(n * .99))]), ("max", g[-1])):
        print("   %-4s %7.1f" % (label, v))
    # What the browser cushion has to absorb: how far a chunk can arrive behind
    # the steady 20ms cadence.
    late = [x - 20.0 for x in gaps if x > 20.0]
    if late:
        late.sort()
        print("excess over the 20ms cadence: p90 %.1f ms, p99 %.1f ms, max %.1f ms"
              % (late[int(len(late) * .9)],
                 late[min(len(late) - 1, int(len(late) * .99))], late[-1]))
    burst = sum(1 for x in gaps if x > 60)
    print("gaps > 60ms: %d (%.2f%% of reads)" % (burst, 100.0 * burst / n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
