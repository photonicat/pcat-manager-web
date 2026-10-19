#!/usr/bin/env python3
"""Call-audio latency probe -- runs on the device, next to pcat-call-audio.

Models exactly what the phone page does (40ms uplink POSTs, one in flight;
chunked downlink read) so the numbers it reports are the numbers a browser
would see, minus the browser's own mic/playout buffers.

Emits a marker every MARKER_PERIOD s: a tone burst at --tx-freq on top of a
continuous low-level noise floor. The noise floor matters -- without it the
codec's DTX/VAD gates the channel and the first burst after silence arrives
late, which would be measured as pipeline delay it is not.

Run it on BOTH ends of a live call with different --tx-freq. Each end detects
the other's tone, so:

    delay_AB + delay_BA  = true round trip, free of any clock offset
    delay_AB - delay_BA  = 2 x clock offset (+ real path asymmetry)

  calat.py --tx-freq 1004 --rx-freq 1504 --dur 45 --out /tmp/calat-a
"""
import argparse
import http.client
import json
import math
import os
import random
import ssl
import struct
import sys
import threading
import time

RATE = 16000
CHUNK_MS = 40
CHUNK_SAMPLES = RATE * CHUNK_MS // 1000        # 640, what flushTx() posts
BURST_MS = 120
MARKER_PERIOD_S = 2.0
NOISE_AMP = 250                                # ~-42 dBFS, keeps VAD open
TONE_AMP = 9000                                # ~-11 dBFS
STREAM_PATH = "/api/v1/voice_call/audio/stream"
TX_PATH = "/api/v1/voice_call/audio/tx"


def log(*a):
    print("[%.3f]" % time.time(), *a, file=sys.stderr, flush=True)


def get_token():
    c = http.client.HTTPSConnection("127.0.0.1", 9443,
                                    context=ssl._create_unverified_context(),
                                    timeout=10)
    c.request("GET", "/api/v1/voice_call/audio/token.json")
    d = json.loads(c.getresponse().read())
    c.close()
    if not d.get("token"):
        raise SystemExit("no audio token: %r" % d)
    return d["token"], d.get("port", 9444)


# --------------------------------------------------------------- signal

def make_chunks(n_chunks, freq):
    """Pre-render the whole test signal so generation cost never affects pacing.
    Returns (list of bytes, {marker_index: chunk_index}).

    Built as one flat array and sliced, not per-sample per-chunk: on this CPU a
    per-sample nested loop over the full 45s took longer than the test itself.
    """
    from array import array
    total = n_chunks * CHUNK_SAMPLES
    burst_samples = RATE * BURST_MS // 1000
    period_chunks = int(MARKER_PERIOD_S * 1000 / CHUNK_MS)

    rnd = random.Random(1234)
    sig = array("h", bytes(2 * total))
    for i in range(total):                       # uniform noise floor
        sig[i] = int((rnd.random() - 0.5) * 2 * NOISE_AMP)

    markers = {}
    m = 0
    ramp = RATE * 2 // 1000                      # 2ms ramps, no click
    while True:
        k = period_chunks * (m + 1)
        if (k * CHUNK_SAMPLES + burst_samples) >= total - CHUNK_SAMPLES:
            break
        markers[m] = k
        if freq:
            base = k * CHUNK_SAMPLES
            w = 2 * math.pi * freq / RATE
            for off in range(burst_samples):
                env = 1.0
                if off < ramp:
                    env = off / ramp
                elif off > burst_samples - ramp:
                    env = (burst_samples - off) / ramp
                v = sig[base + off] + int(TONE_AMP * env * math.sin(w * off))
                sig[base + off] = max(-32768, min(32767, v))
        m += 1

    raw = sig.tobytes()
    step = CHUNK_SAMPLES * 2
    chunks = [raw[i * step:(i + 1) * step] for i in range(n_chunks)]
    return chunks, markers


# ------------------------------------------------------------------ tx

def tx_thread(chunks, markers, token, port, out, keepalive, done):
    """Mirrors phone.html flushTx(): the mic node appends 40ms frames to a queue
    and a 40ms timer merges everything queued into ONE POST, with a single
    request in flight. A skipped tick therefore does not fall behind -- the next
    POST carries a bigger block -- so the uplink shows up as a fixed latency
    floor rather than an ever-growing backlog."""
    path = TX_PATH + "?token=" + token
    ctx = ssl._create_unverified_context()
    lock = threading.Lock()
    pending = []                 # [(chunk_index, body, sched_wall)]
    sent = {}                    # chunk_index -> marker record
    posts = []                   # (t_start, total_ms, connect_ms, bytes, status)
    stop = threading.Event()
    marker_chunks = {k: m for m, k in markers.items()}

    def producer():
        t0 = time.time()
        m0 = time.monotonic()
        out["tx_t0"] = t0
        for k, body in enumerate(chunks):
            dt = m0 + k * CHUNK_MS / 1000.0 - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            sched = t0 + k * CHUNK_MS / 1000.0
            with lock:
                if k in marker_chunks:
                    sent[k] = {"marker": marker_chunks[k], "sched": sched}
                pending.append((k, body, sched))
        stop.set()

    pt = threading.Thread(target=producer, daemon=True)
    pt.start()
    while "tx_t0" not in out:
        time.sleep(0.005)

    conn = None
    while not (stop.is_set() and not pending):
        tick = time.monotonic() + CHUNK_MS / 1000.0
        with lock:
            batch, pending[:] = list(pending), []
        if batch:
            body = b"".join(b for _, b, _ in batch)
            t_start = time.time()
            status, connect_ms = -1, 0.0
            try:
                if conn is None:
                    tc = time.monotonic()
                    conn = http.client.HTTPSConnection("127.0.0.1", port,
                                                       context=ctx, timeout=20)
                    conn.connect()
                    connect_ms = (time.monotonic() - tc) * 1000
                conn.request("POST", path, body=body,
                             headers={"Content-Type": "text/plain"})
                r = conn.getresponse()
                r.read()
                status = r.status
                # Reuse only when the peer agrees. The daemon answers
                # "Connection: close", so without the keep-alive fix this drops
                # the connection and the next POST pays another full handshake
                # -- exactly what the browser pays today.
                if not keepalive or r.will_close:
                    conn.close()
                    conn = None
            except Exception:
                try:
                    if conn:
                        conn.close()
                except Exception:
                    pass
                conn = None
            t_end = time.time()
            posts.append((t_start, (t_end - t_start) * 1000, connect_ms,
                          len(body), status))
            with lock:
                for k, _, _ in batch:
                    if k in sent:
                        sent[k].update(post_start=t_start, post_end=t_end,
                                       batch_bytes=len(body),
                                       batch_ms=len(body) / 2 / RATE * 1000)
        dt = tick - time.monotonic()
        if dt > 0:
            time.sleep(dt)

    if conn:
        try:
            conn.close()
        except Exception:
            pass
    out["tx"] = {
        "t0": out.get("tx_t0"),
        "keepalive": bool(keepalive),
        "markers": [sent[k] for k in sorted(sent)],
        "posts": posts,
        "chunks": len(chunks),
    }
    done.set()


# ------------------------------------------------------------------ rx

def rx_thread(token, port, out, rawpath, ready, stop):
    ctx = ssl._create_unverified_context()
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)
    c.request("GET", STREAM_PATH + "?token=" + token)
    r = c.getresponse()
    log("rx stream status", r.status)
    if r.status != 200:
        out["rx"] = {"error": r.status, "body": r.read().decode(errors="replace")}
        ready.set()
        return
    ready.set()
    idx = []          # (wall_time, byte_offset, nbytes)
    off = 0
    t_first = None
    with open(rawpath, "wb") as f:
        while not stop.is_set():
            try:
                b = r.read1(8192)
            except Exception as e:
                log("rx read error", e)
                break
            if not b:
                break
            t = time.time()
            if t_first is None:
                t_first = t
            f.write(b)
            idx.append((t, off, len(b)))
            off += len(b)
    try:
        c.close()
    except Exception:
        pass
    out["rx"] = {"t_first": t_first, "bytes": off, "index": idx}


# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tx-freq", type=float, default=0)
    ap.add_argument("--rx-freq", type=float, default=0)
    ap.add_argument("--dur", type=float, default=45)
    ap.add_argument("--out", default="/tmp/calat")
    ap.add_argument("--no-tx", action="store_true")
    ap.add_argument("--chunk-ms", type=int, default=40,
                    help="uplink POST cadence, i.e. phone.html's flushTx timer")
    ap.add_argument("--keepalive", action="store_true",
                    help="reuse one TLS connection for the uplink (needs the "
                         "daemon-side keep-alive fix to have any effect)")
    a = ap.parse_args()

    global CHUNK_MS, CHUNK_SAMPLES
    CHUNK_MS = a.chunk_ms
    CHUNK_SAMPLES = RATE * CHUNK_MS // 1000

    token, port = get_token()
    n_chunks = int(a.dur * 1000 / CHUNK_MS)
    chunks, markers = ([], {}) if a.no_tx else make_chunks(n_chunks, a.tx_freq)
    log("rendered", len(chunks), "chunks,", len(markers), "markers @",
        a.tx_freq, "Hz")

    out = {"role": {"tx_freq": a.tx_freq, "rx_freq": a.rx_freq,
                    "dur": a.dur, "rate": RATE, "chunk_ms": CHUNK_MS,
                    "keepalive": a.keepalive,
                    "burst_ms": BURST_MS, "marker_period": MARKER_PERIOD_S},
           "host_t0": time.time()}
    rx_ready = threading.Event()
    rx_stop = threading.Event()
    tx_done = threading.Event()
    rt = threading.Thread(target=rx_thread,
                          args=(token, port, out, a.out + ".raw", rx_ready, rx_stop),
                          daemon=True)
    rt.start()
    rx_ready.wait(timeout=15)
    time.sleep(0.5)                     # let the capture settle before marking
    if not a.no_tx:
        tt = threading.Thread(target=tx_thread,
                              args=(chunks, markers, token, port, out,
                                    a.keepalive, tx_done),
                              daemon=True)
        tt.start()
        tx_done.wait(timeout=a.dur + 30)
    else:
        time.sleep(a.dur)
    time.sleep(1.0)                     # drain the tail of the downlink
    rx_stop.set()
    rt.join(timeout=5)
    with open(a.out + ".json", "w") as f:
        json.dump(out, f)
    rx = out.get("rx", {})
    log("done: rx bytes", rx.get("bytes"), "reads", len(rx.get("index", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
