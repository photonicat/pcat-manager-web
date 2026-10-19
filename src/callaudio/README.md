# pcat-call-audio

The in-call audio sample path, in C. Replaces the `arecord`/`aplay`
subprocesses that `app/pc_call_audio.py` drove over pipes.

## Why

The RM500Q call audio had ~5s of delay. The cause was not slowness anywhere —
it was that every stage between the modem and the browser is a FIFO that never
discards anything:

- `arecord` inherited the USB card's default capture ring (deep enough to hold
  seconds on this module's UAC endpoint)
- a 64KB kernel pipe = 2s of 16k mono S16
- Python's `BufferedReader` read-ahead on top of that pipe
- a browser scheduler that trimmed to 1s and stayed there

Because nothing dropped stale audio, a momentary stall became *permanent*
added delay. Fixing the Python version (commit prior to this one) capped the
buffers and added backlog shedding at both ends. This daemon removes the
remaining structural sources: no subprocesses, no pipes, no interpreter
between ALSA and the socket, and explicit `snd_pcm_*` period control.

## What it serves

Two endpoints on **:9444**, TLS, the same paths the phone page already used:

    GET  /api/v1/voice_call/audio/stream   chunked raw S16_LE 16k mono
    POST /api/v1/voice_call/audio/tx       raw S16_LE 16k mono
    GET  /api/v1/voice_call/audio/health   liveness + card presence

Everything else about a call — dialling, control, history, recordings,
voicemail — stays on Flask at :9443. TLS uses the **same cert** the init
script already generates with px5g for the Flask HTTPS listener; no new PKI.

Responses are **keep-alive**; only the downlink stream (terminal by nature) and
error replies close. This is not a micro-optimisation. That cert is RSA-2048
and a full handshake measured **125ms** on a photonicat2, against 2.6ms for the
request itself — so a connection-per-POST uplink could not physically reach the
page's 25 posts/s. It degraded to ~6/s carrying 160ms blocks, and on the slower
box the backlog grew without bound (one-way delay climbed 438 → 980ms over a
45s call). Idle connections are reaped after `CA_TLS_IDLE_MS`.

## Auth

:9444 is a different origin from :9443, so the daemon cannot see the Flask
session cookie. Rather than reimplement itsdangerous' signed-cookie format in
C, Flask (which has already authenticated the user) mints a short-lived token:

    <expiry_unix>.<hex hmac-sha256(secret, expiry)>

Shared secret at `/etc/pcat-manager-web/audio_token.key` (0600), created by
`app/pc_audio_token.py` at web-app startup. The expiry is inside the MAC, so a
client cannot extend its own token. The daemon **refuses to start** without a
secret rather than serve call audio unauthenticated.

## Build

Native (on the device, or any Linux box with `libasound2-dev` and
`libmbedtls-dev`):

    make

Cross for the aarch64 OpenWrt target:

    make CROSS=aarch64-openwrt-linux- SYSROOT=/path/to/staging_dir/target-...

From a Mac with no OpenWrt staging dir (the usual case here — and the device
libraries are sstripped, so ld.lld cannot link against them directly):

    misc/build-callaudio-zig.sh

Syntax-only check on a dev box without the libraries:

    make check

`misc/make.py` ships `src/callaudio/pcat-call-audio` into the package if it
has been built. If it is absent the package still works — Flask reports the
daemon unavailable and the phone page falls back to the Python bridge.

## Install

    cp pcat-call-audio /usr/share/pcat-manager-web/
    cp misc/pcat-call-audio.init /etc/init.d/pcat-call-audio
    chmod +x /etc/init.d/pcat-call-audio
    /etc/init.d/pcat-call-audio enable
    /etc/init.d/pcat-call-audio start

## Latency budget

| Stage | Before | Now |
|---|---|---|
| capture ring | card default (deep) | 80ms, 20ms periods (measures 0ms queued) |
| capture pipe + Python read-ahead | up to ~2.25s | gone |
| playback ring | 200ms (aplay default thresholds) | 120ms ring, held ≤80ms queued |
| uplink TLS handshake | 125ms per POST | once per call (keep-alive) |
| uplink post rate | ~6/s, 160ms blocks | 22/s, 40ms blocks |
| browser schedule | settled at 1s | 40–180ms, hard resync |
| mic ScriptProcessor | 4096 fr (~85ms @48k) | 1024 fr (~21ms) |
| POST cadence | 100ms | 40ms |

Backlog shedding is what makes this hold, in **both** directions, and for the
same reason: every stage is a FIFO clocked by the modem, so a backlog that
forms once is delay carried for the rest of the call.

- downlink: `ca_capture_lead()` tracks audio drained faster than the wire
  clock; past `CA_MAX_LAG_SECS` (250ms) it is dropped rather than delivered.
- uplink: queued audio is held under `CA_MAX_UPLINK_DELAY_MS` (80ms) — the
  excess is dropped from the *old* end, since the newest mic audio is the only
  part still worth hearing. Without this the ring settled wherever the call's
  first POST left it (117ms on one module, 135ms on the other) and stayed.

## Measuring it

`misc/calat.py` runs on the device and models what the phone page does (40ms
merged uplink POSTs, one in flight; chunked downlink read). It emits a tone
burst every 2s over a continuous low noise floor — the noise matters, or the
codec's DTX gates the channel and the first burst's own onset is measured as
pipeline delay.

Run it on **both** ends of a live call with different `--tx-freq`; each end
detects the other's tone, so the two directions sum to a round trip that needs
no clock synchronisation at all:

    # on A                                    # on B
    calat.py --tx-freq 1004 --dur 45 \         calat.py --tx-freq 1504 --dur 45 \
             --keepalive --out /tmp/a                   --keepalive --out /tmp/b

    misc/calat_analyze.py A=/tmp/a.json,/tmp/a.raw B=/tmp/b.json,/tmp/b.raw

Cross-tested RM500Q-GL ↔ RM500U-CNV over live VoLTE, 22 bursts per direction:

| | round trip | A→B | B→A |
|---|---|---|---|
| connection-per-POST | 1282ms | 520ms | 761ms, drifting to 980ms |
| keep-alive | 639ms | 336ms | 303ms |
| keep-alive + uplink ceiling | **499ms** | **277ms** | **223ms** |

What is left is mostly the cellular path itself: the downlink reports 0ms
queued in the card and 20ms read spacing, and the uplink holds ~60ms.

## Fallback

If the daemon is not running, `/api/v1/voice_call/audio/token.json` reports
`available: false` and the phone page uses the Flask endpoints unchanged. The
Python bridge in `pc_call_audio.py` is still fully functional and still owns
call recording and voicemail, which tap the same streams.
