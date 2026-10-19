#!/usr/bin/env python3
"""Serves misc/browser_audio_probe.html and collects the result it posts back.

Two steps, deliberately: launching Chrome as a child of this process did not
work reliably here (it fetched the page but never completed the probe), so the
browser is launched separately.

  1) python3 misc/browser_audio_probe_serve.py
  2) in another shell:
       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
         --headless=new --disable-gpu --no-sandbox \
         --user-data-dir=/tmp/browser-audio-probe/prof \
         --autoplay-policy=no-user-gesture-required \
         --use-fake-device-for-media-stream --use-fake-ui-for-media-stream \
         http://127.0.0.1:8899/

Results land in /tmp/browser-audio-probe/result.txt and are printed here.
http://127.0.0.1 counts as a secure origin, so getUserMedia and AudioWorklet
work with no certificate; Chrome's fake device stands in for a microphone.
"""
import http.server
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = "/tmp/browser-audio-probe"
PORT = 8899


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WORK, **k)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace")
        with open(os.path.join(WORK, "result.txt"), "w") as f:
            f.write(body)
        self.send_response(204)
        self.end_headers()
        print("\n=== result ===\n" + body, flush=True)

    def log_message(self, *a):
        pass


def main():
    os.makedirs(os.path.join(WORK, "static"), exist_ok=True)
    shutil.copy(os.path.join(HERE, "browser_audio_probe.html"),
                os.path.join(WORK, "index.html"))
    worklet = os.path.join(HERE, "..", "app", "static", "mic-worklet.js")
    if os.path.exists(worklet):
        shutil.copy(worklet, os.path.join(WORK, "static", "mic-worklet.js"))
    if os.path.exists(os.path.join(WORK, "result.txt")):
        os.remove(os.path.join(WORK, "result.txt"))
    print("serving %s on http://127.0.0.1:%d/ -- see this file's docstring for "
          "the Chrome command" % (WORK, PORT), flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
