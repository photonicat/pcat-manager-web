#!/usr/bin/env python3
"""Does the modem clock call audio before the call goes active?

phone.html only calls startAudio() once status reports "active", so during
setup the caller hears nothing -- no ringback, no early media -- and the whole
audio path (token, getUserMedia, TLS, ALSA open) is built at the worst moment.
If the module already clocks audio while alerting, all of that can move earlier.

Opens the daemon's downlink the instant the dial is placed and prints per-200ms
RMS alongside the call-state transitions, on one clock.

  earlyaudio.py <number>
"""
import json
import ssl
import sys
import threading
import time
import urllib.request
from array import array

sys.path.insert(0, "/tmp")
sys.path.insert(0, "/usr/share/pcat-manager-web")
import callctl                                          # noqa: E402
import http.client                                      # noqa: E402

CTX = ssl._create_unverified_context()
T0 = None
events = []


def note(what):
    events.append((time.monotonic() - T0, what))
    print("[%6.2fs] %s" % (time.monotonic() - T0, what), flush=True)


def token():
    with urllib.request.urlopen(
            "https://127.0.0.1:9443/api/v1/voice_call/audio/token.json",
            context=CTX, timeout=10) as r:
        d = json.loads(r.read())
    return d["token"], d.get("port", 9444)


def rms(b):
    a = array("h")
    a.frombytes(b[:len(b) - len(b) % 2])
    if not a:
        return 0.0
    return (sum(v * v for v in a) / len(a)) ** 0.5


def stream_reader(stop):
    tok, port = token()
    t = time.monotonic()
    c = http.client.HTTPSConnection("127.0.0.1", port, context=CTX, timeout=70)
    c.request("GET", "/api/v1/voice_call/audio/stream?token=" + tok)
    r = c.getresponse()
    note("audio stream opened in %.0f ms -> HTTP %d"
         % ((time.monotonic() - t) * 1000, r.status))
    if r.status != 200:
        return
    buf = b""
    first = True
    win_t = time.monotonic()
    while not stop.is_set():
        b = r.read1(8192)
        if not b:
            break
        if first:
            note("FIRST downlink bytes")
            first = False
        buf += b
        if time.monotonic() - win_t >= 0.2:
            level = rms(buf)
            bar = "#" * min(40, int(level / 40))
            print("[%6.2fs]   rms %6.0f %s" % (time.monotonic() - T0, level, bar),
                  flush=True)
            if level > 300:
                events.append((time.monotonic() - T0, "AUDIBLE rms=%.0f" % level))
            buf = b""
            win_t = time.monotonic()
    c.close()


def main():
    global T0
    number = sys.argv[1]
    stop = threading.Event()
    T0 = time.monotonic()

    st, _ = callctl.call("POST", "/api/v1/voice_call/make", {"number": number})
    note("dial POST returned (%s)" % st)

    rt = threading.Thread(target=stream_reader, args=(stop,), daemon=True)
    rt.start()

    last = None
    while time.monotonic() - T0 < 30:
        try:
            with urllib.request.urlopen(
                    "https://127.0.0.1:9443/api/v1/voice_call/status",
                    context=CTX, timeout=30) as r:
                calls = json.loads(r.read()).get("calls", [])
        except Exception as e:
            calls = []
        s = calls[0].get("status") if calls else "none"
        if s != last:
            note("call status -> %s" % s)
            last = s
        time.sleep(0.5)

    stop.set()
    time.sleep(0.5)
    callctl.call("POST", "/api/v1/voice_call/hangup", {})
    note("hung up")

    print("\n== timeline")
    audible = [e for e in events if e[1].startswith("AUDIBLE")]
    for t, w in events:
        if not w.startswith("AUDIBLE"):
            print("   %6.2fs  %s" % (t, w))
    if audible:
        print("   %6.2fs  first audible audio (%s)" % (audible[0][0], audible[0][1]))


if __name__ == "__main__":
    sys.exit(main())
