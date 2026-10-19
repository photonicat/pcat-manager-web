#!/usr/bin/env python3
"""Analyse a calat.py capture pair.

Delay per marker = (wall time the far end's chunked stream delivered the burst
onset) - (wall time the near end handed that sample to the uplink). Because both
ends measure the other's tone, the two directions sum to a clock-offset-free
round trip.

  calat_analyze.py A=/tmp/a.json,/tmp/a.raw B=/tmp/b.json,/tmp/b.raw
"""
import json
import math
import struct
import sys
from array import array

FRAME_MS = 10


def goertzel(samples, start, n, freq, rate):
    w = 2 * math.pi * freq / rate
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for i in range(start, start + n):
        s0 = samples[i] + coeff * s1 - s2
        s2, s1 = s1, s0
    return math.sqrt(s1 * s1 + s2 * s2 - coeff * s1 * s2) / n


def load(jpath, rpath):
    with open(jpath) as f:
        meta = json.load(f)
    with open(rpath, "rb") as f:
        raw = f.read()
    pcm = array("h")
    pcm.frombytes(raw[:len(raw) - len(raw) % 2])
    return meta, pcm


def arrival_times(index, n_samples):
    """Map sample -> wall time of the socket read that delivered it."""
    marks = []          # (first_sample_of_read, t)
    for t, off, ln in index:
        marks.append((off // 2, t))
    return marks


def t_of_sample(marks, s):
    lo, hi = 0, len(marks) - 1
    if not marks or s < marks[0][0]:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if marks[mid][0] <= s:
            lo = mid
        else:
            hi = mid - 1
    return marks[lo][1]


def onsets(pcm, marks, freq, rate=16000):
    n = rate * FRAME_MS // 1000
    nf = len(pcm) // n
    mags = [goertzel(pcm, i * n, n, freq, rate) for i in range(nf)]
    if not mags:
        return [], []
    peak = max(mags)
    if peak <= 0:
        return [], mags
    smags = sorted(mags)
    floor = smags[len(smags) // 2]
    thr = max(peak * 0.35, floor * 6 + 1e-9)
    res = []
    i = 0
    while i < nf:
        if mags[i] >= thr:
            # onset frame; refine to the first frame of the rising edge
            j = i
            while j > 0 and mags[j - 1] >= thr * 0.5:
                j -= 1
            t = t_of_sample(marks, j * n)
            width = 0
            k = i
            while k < nf and mags[k] >= thr * 0.5:
                width += 1
                k += 1
            if t is not None:
                res.append({"frame": j, "t": t, "width_ms": width * FRAME_MS,
                            "mag": mags[i]})
            i = k + int(500 / FRAME_MS)          # skip half a second
        else:
            i += 1
    return res, mags


def stats(xs):
    if not xs:
        return {}
    s = sorted(xs)
    return {"n": len(s), "min": s[0], "median": s[len(s) // 2],
            "p90": s[min(len(s) - 1, int(len(s) * 0.9))], "max": s[-1],
            "mean": sum(s) / len(s)}


def fmt(d, unit="ms"):
    if not d:
        return "no data"
    return ("n=%d min=%.0f med=%.0f p90=%.0f max=%.0f" %
            (d["n"], d["min"], d["median"], d["p90"], d["max"])) + unit


def direction(src, dst, name):
    """Delay from src's uplink markers to dst's downlink arrivals."""
    freq = src[0]["role"]["tx_freq"]
    det, mags = onsets(dst[1], arrival_times(dst[0]["rx"]["index"], len(dst[1])), freq)
    sent = src[0]["tx"]["markers"]
    out = []
    for d in det:
        # match to the most recent marker sent no later than arrival
        best = None
        for m in sent:
            if m["sched"] <= d["t"] and (best is None or m["sched"] > best["sched"]):
                best = m
        if best is None:
            continue
        out.append({"marker": best["marker"],
                    "sched": best["sched"],
                    "arrived": d["t"],
                    "delay_ms": (d["t"] - best["sched"]) * 1000,
                    "post_ms": (best.get("post_end", best["sched"]) - best["sched"]) * 1000,
                    "width_ms": d["width_ms"]})
    # drop duplicates that matched the same marker (echo of a previous burst)
    seen = {}
    for o in out:
        if o["marker"] not in seen or o["delay_ms"] < seen[o["marker"]]["delay_ms"]:
            seen[o["marker"]] = o
    out = [seen[k] for k in sorted(seen)]
    print("\n== %s  (tone %.0f Hz, %d bursts sent, %d detected)"
          % (name, freq, len(sent), len(out)))
    for o in out:
        print("   marker %2d  delay %7.1f ms   burst %3d ms  post+%.0fms"
              % (o["marker"], o["delay_ms"], o["width_ms"], o["post_ms"]))
    st = stats([o["delay_ms"] for o in out])
    print("   delay: " + fmt(st))
    return st, out


def rate_report(meta, name):
    rx = meta.get("rx", {})
    idx = rx.get("index", [])
    if len(idx) < 2:
        print("%s: no downlink data" % name)
        return
    dur = idx[-1][0] - idx[0][0]
    got = (rx["bytes"] - idx[0][1]) / 2 / 16000
    gaps = [(idx[i + 1][0] - idx[i][0]) * 1000 for i in range(len(idx) - 1)]
    posts = meta.get("tx", {}).get("posts", [])
    pms = [p[1] for p in posts]                 # whole POST, wall ms
    cms = [p[2] for p in posts if p[2] > 0]     # TCP+TLS connect share
    blk = [p[3] / 2 / 16000 * 1000 for p in posts]   # audio ms per POST
    bad = [p for p in posts if p[4] != 200]
    print("\n-- %s pipeline" % name)
    print("   downlink: %.2fs audio in %.2fs wall (%.3fx), %d reads, "
          "gap %s" % (got, dur, got / dur if dur else 0, len(idx), fmt(stats(gaps))))
    print("   uplink:   %d posts (keepalive=%s), %d non-200"
          % (len(posts), meta.get("tx", {}).get("keepalive"), len(bad)))
    print("             post wall %s" % fmt(stats(pms)))
    print("             of which connect+TLS %s" % fmt(stats(cms)))
    print("             audio carried per post %s" % fmt(stats(blk)))
    if pms:
        print("             uplink post rate %.1f/s (browser aims for 25/s)"
              % (len(posts) / (posts[-1][0] - posts[0][0] + 0.04)))


def main():
    args = {}
    for a in sys.argv[1:]:
        k, v = a.split("=", 1)
        j, r = v.split(",")
        args[k] = load(j, r)
    if len(args) != 2:
        print(__doc__)
        return 2
    (na, A), (nb, B) = list(args.items())
    for n, m in ((na, A), (nb, B)):
        rate_report(m[0], n)
    sa, _ = direction(A, B, "%s -> %s uplink-to-downlink" % (na, nb))
    sb, _ = direction(B, A, "%s -> %s uplink-to-downlink" % (nb, na))
    if sa and sb:
        rt = sa["median"] + sb["median"]
        print("\n== round trip (clock-offset free): %.0f ms" % rt)
        print("   symmetric one-way estimate:      %.0f ms" % (rt / 2))
        print("   apparent clock skew / asymmetry: %.0f ms"
              % ((sa["median"] - sb["median"]) / 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
