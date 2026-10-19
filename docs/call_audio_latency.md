# Call Audio Latency

How in-call audio delay is measured on a photonicat2, what was optimised, what
was tried and rejected, and what is left. All numbers here come from live
cellular calls between two devices, not from a model.

- Code: `src/callaudio/` (the `pcat-call-audio` daemon), `app/templates/phone.html` (browser side)
- Harness: `misc/calat.py`, `misc/calat_analyze.py`, `misc/callctl.py`
- Result: round-trip **1282 ms → 499 ms**

## 1. How it is measured

The hard part is that "delay" spans two devices, so any single-clock measurement
is really a clock-offset measurement. The trick used here removes that entirely:

**Both ends transmit, each at a different tone frequency, during the same call.**
Each end detects the *other's* tone, so

    delay(A→B) + delay(B→A) = true round trip     ← no clock sync needed
    delay(A→B) − delay(B→A) = 2 × clock offset    ← reports the skew instead

`misc/calat.py` runs on the device and imitates the phone page rather than
approximating it: 40 ms uplink POSTs merged exactly like `flushTx()` with one
request in flight, and a chunked downlink read that timestamps every socket
read. So its numbers are what a browser would see, minus the browser's own
buffers (see §2).

Two details that are easy to get wrong:

- **The tone rides on a continuous low noise floor.** Without it, the codec's
  DTX/VAD gates the channel between bursts and the *first* burst's own onset is
  delayed by VAD resume — which would be measured as pipeline delay it is not.
- **Timestamps come from socket-read arrival**, not from sample position, since
  the question is when audio reached the consumer.

Running it:

```bash
# on device A                                on device B
calat.py --tx-freq 1004 --dur 45 \           calat.py --tx-freq 1504 --dur 45 \
         --keepalive --out /tmp/a                     --keepalive --out /tmp/b

# then, anywhere
misc/calat_analyze.py A=/tmp/a.json,/tmp/a.raw B=/tmp/b.json,/tmp/b.raw
```

**Resolution: ±20 ms per run** (10 ms Goertzel frames, 20 ms chunk arrival
granularity). Do not read anything into a 20 ms difference between two runs.
Independently corroborated by the measured clock skew, which came out at
17–27 ms across runs — consistent with the noise floor, on ntp-synced boxes.

### Test rig

| Box | Module | Audio path | Number |
|---|---|---|---|
| 192.168.37.146 | RM500Q-GL | USB audio (UAC card `RM500QGL`) | +8613020202705 |
| 192.168.21.114 | RM500U-CNV | PCM/SAI (card `modem`) | +8613800138003 |

Two rig hazards that will silently ruin a run:

- **`.114` has voicemail enabled** (`phone_voicemail_enabled`, answer_secs 5).
  It auto-answers before you can, and its `arecord ... /root/call_recordings/vm_*`
  holds the SAI capture — the daemon then fails `open plughw:CARD=modem:
  Resource busy` and the downlink yields zero bytes. Disable it for the test and
  **restore it afterwards**. `phone_auto_record` does the same thing.
- Call-control calls can take **>30 s** while the AT port is busy dialling. An
  empty reply is a timeout, not a failure.

## 2. Where the delay actually is

One-way, measured (median of both directions, best configuration):

| Stage | ms | How known |
|---|---|---|
| uplink queue wait (40 ms batch) | ~20 | probe, average of 0–40 |
| uplink card ring | ~60 | `snd_pcm_delay`, logged every 5 s |
| modem encode + VoLTE + far modem decode/jitter buffer | **~158** | residual |
| far-end capture ring + period quantisation | ~10 | daemon logs `0 ms queued` |
| far-end TLS chunk write | ~2 | loopback |
| **measured total** | **~250** | tone correlation |

A human on the phone page experiences this *plus* the parts the on-device probe
cannot see, because it runs over loopback:

| Additional stage | was | now | Source |
|---|---|---|---|
| mic capture node | **64** | **8** | measured, §6 |
| WiFi RTT (POST out, stream in) | ~15 | ~15 | not measurable from here, §6 |
| browser playout schedule (`FLOOR`) | 40 | 40 | `phone.html` `playChunk()` |
| **perceived one-way** | **~369** | **~313** | |

The mic figure was wrong in this table for as long as it existed, and wrong in
the code comment it came from: it read "1024 fr (~85ms at 48k versus 21ms
here)", but `phone.html` asks for a **16kHz** context and Chrome grants it, so
1024 frames was **64ms**, not 21. Fixed in §6.

For reference, ITU-T G.114 calls <150 ms one-way excellent and <400 ms still
acceptable. So the ~150 ms cellular leg alone rules out "excellent", and the
device stack is now a minority of the total.

## 3. What was optimised

### 3.1 The uplink connection (the big one)

The daemon answered `Connection: close`, so **every 40 ms uplink POST paid a
full TCP+TLS handshake**. On this SoC, with the px5g RSA-2048 cert that is
shared with the Flask listener:

    TLS handshake   125 ms
    the request      2.6 ms

25 posts/s was therefore never physically possible. What actually happened was
a silent degradation, not an error: the browser's `flushTx()` skips a tick while
a POST is in flight and merges the backlog into the next one, so the uplink
settled at **~6 posts/s carrying 160 ms blocks** — 160 ms of mic audio queued
before anything was even sent. On the busier box it never reached equilibrium at
all and one-way delay climbed **438 → 980 ms over a 45 s call**.

Fix: serve requests until the peer goes away (`main.c:serve()`), with an idle
reap after `CA_TLS_IDLE_MS` so a browser that vanished mid-call cannot pin a
thread. The downlink stream stays terminal, and error replies still close.

Care required in the request loop: leftover bytes after a request can be raw
PCM containing NULs, so the head scan is byte-bounded rather than `strstr`.
Pipelined POSTs are handled (tested).

### 3.2 The uplink card ring

Keep-alive alone left a second, subtler problem visible. The playback ring
drains at exactly the wire clock while the browser produces at exactly real
time — so **any backlog that forms once is carried for the rest of the call**.
Nothing in a pure FIFO ever gives it back. The call's first POST reliably forms
one, because it carries whatever piled up during connection setup.

Measured resting occupancy with no bound: **117 ms** on one module, **135 ms**
on the other — every millisecond of it audible delay, and dependent on nothing
but how the call happened to start.

Fix: `CA_MAX_UPLINK_DELAY_MS` (80 ms) is a hard ceiling on queued uplink audio.
Excess is dropped **from the old end** — the newest mic audio is the only part
still worth hearing. This is the policy the downlink already had
(`ca_capture_lead()` / `CA_MAX_LAG_SECS`), applied to the direction that lacked
it.

### 3.3 Telemetry

`snd_pcm_delay`-based logging every 5 s (uplink) and 10 s (downlink):

```
uplink: 112 posts in 5.0s (22.3/s), 45ms audio per post, card ring 63ms avg, 784 reused requests
downlink: 0 ms queued in card, 6 ms lead, 0 chunks shed so far
```

This exists because **byte counts cannot distinguish a healthy uplink from one
running 300 ms late** — all the audio arrives, just late. That is exactly how
the problem above hid for so long. Related trap already on record: a *clocking*
card streaming pure silence also looks perfectly healthy by byte count.

### 3.4 Results

Live VoLTE, RM500Q-GL ↔ RM500U-CNV, 22 bursts per direction per run:

| Configuration | Round trip | Q→U | U→Q |
|---|---|---|---|
| connection-per-POST (before) | 1282 ms | 520 ms | 761 ms, drifting to 980 ms |
| keep-alive | 639 ms | 336 ms | 303 ms |
| keep-alive + 80 ms uplink ceiling | **499 ms** | **277 ms** | **223 ms** |

Supporting evidence that the pipeline is now clean:

- uplink 22.3 posts/s, 40 ms per post, **1 TLS handshake per call** (was 285)
- jitter collapsed: Q→U spread 276–297 ms (was 479–600 ms and drifting)
- downlink `0 ms queued in card`, 20 ms read spacing (max gap 26 ms), 0 chunks shed
- 16006 Hz measured against 16000 nominal, both boxes

## 4. Tried and rejected

Negative results, kept because they are the expensive part to rediscover.

### 4.1 Tightening the ceiling to 40 ms — actively harmful

The ring did shrink as asked (63 → 44 ms and 59 → 42 ms). It cost **6832 ms of
dropped mic audio in a 40 s call on one box (17%)** and 5453 ms (14%) on the
other, for a round-trip change of 519 ms vs 499 ms — i.e. *below* the ±20 ms
noise floor, and in the wrong direction.

The reason is structural, and it generalises:

> **The ceiling must exceed one batch plus one period.** The uplink arrives in
> ~45 ms batches; a 40 ms ceiling clips *every single POST* by 5–18 ms, which is
> continuous mutilation rather than occasional backlog shedding.

80 ms leaves room for a 45 ms batch on top of a ~35 ms residual, which is why
its skip rate is only 152 ms per 45 s call (0.3%, and that is clock-drift
correction, not backlog).

### 4.2 Halving the POST cadence to 20 ms — no effect

Round trip 519 ms, unchanged. The batches stayed at a 40 ms median regardless,
because **the loop is ALSA-bound, not timer-bound**: the blocking `snd_pcm_writei`
paces the uplink, so a faster timer just finds the same amount of audio waiting.
No reason to double the request rate for it.

## 5. Can we optimise further?

Device-side, we are essentially at the floor: the downlink is at literal zero
queued, and §4.1 shows the uplink ring cannot be squeezed without paying in
dropped audio. The ~158 ms cellular leg is not ours. What genuinely remains:

| # | Change | Expected | Risk / blocker |
|---|---|---|---|
| 1 | **Pre-warm the uplink connection** when the call view opens, so the first audio POST does not carry a 125 ms handshake | removes the startup transient at its source; the ceiling then never has to trim | browser-side only, cheap; relies on fetch pooling reusing the socket |
| 2 | mic `ScriptProcessor` 1024 → **`AudioWorklet`** (128-frame quantum) | 15–20 ms | `ScriptProcessor` is deprecated anyway; needs a worklet module |
| 3 | playout `FLOOR` 40 → 20–30 ms | 10–20 ms | glitches on WiFi jitter — **needs a browser-side harness first** |
| 4 | capture period 20 → 10 ms | ~5 ms | doubles TLS record + syscall rate; marginal |
| 5 | Track the card's real rate (16006 Hz vs 16000 nominal) | 0 ms latency, fewer audible resyncs | 0.04% drift walks the browser schedule ~24 ms/min into a hard resync every few minutes |
| 6 | ECDSA cert or TLS session tickets | ~120 ms once per call, not steady-state | cert is shared with the Flask listener — wider blast radius |

Note that `CEIL` (180 ms) in `playChunk()` is a *resync trigger*, not steady-state
delay: on a clean stream the schedule sits at `FLOOR`. Lowering it only speeds
recovery after a stall.

The honest limiter on items 2–4 is that **this harness cannot see the browser**.
It runs on-device over loopback, so mic path, WiFi jitter and playout scheduling
are all outside it. Tuning them from the on-device numbers would be guessing —
and §4.1 is a fresh reminder of what a plausible-sounding, unmeasured
"optimisation" costs. A browser-side probe (an instrumented page logging
`AudioContext.currentTime` against arrival, driven headless) is the prerequisite
for that work.

## 6. Regression check

Any change to the sample path or the HTTP layer should be re-measured, since
none of these failures produce an error — they only produce delay.

1. Place a call between the two rig boxes (§1), disabling voicemail first.
2. Run `calat.py` on both ends, analyse, compare against §3.4.
3. Check `logread | grep call-audio` for:
   - `card ring` settling in the 55–70 ms band, not creeping upward
   - `reused requests` climbing — if it stays 0, keep-alive has regressed
   - `0 chunks shed` and `0 ms queued` on the downlink
   - skip totals of a few hundred ms per call, not thousands (§4.1)

## 7. Call setup time

A separate complaint from in-call delay: the seconds between tapping Call and
hearing the other person. Measured with `misc/dialtime.py` and
`misc/earlyaudio.py` on .146 (RM500Q-GL, China Unicom 46001):

| Phase | Time | Ours? |
|---|---|---|
| `POST /make` returns | 0.05–0.50 s | yes, and already fast |
| status reports `dialing` | 0.46 s | yes |
| status reports `alerting` | 2.64 s | **no** — see below |
| status reports `active` | 9.5–11 s | no — that is the callee answering |
| first audible downlink audio | 10.72 s | no |

Things this ruled out:

- **AT port contention is not a factor.** Idle `/status` answers in 40–52 ms,
  and the dial POST itself is 0.05–0.50 s. The `serial_lock` shared with the
  status/signal polling loop was the obvious suspect and is innocent.
- **The audio path is not built late.** `has_active_calls` is `len(calls) > 0`
  server-side, which is already true at `dialing`, so `startAudio()` fires
  within the first poll. Verified directly: the daemon's downlink opens
  `200 OK` in 141 ms and delivers bytes at 0.66 s, *during dialing* — the card
  clocks throughout setup. Token, TLS, ALSA open and `getUserMedia` are
  therefore all paid during setup already, not in the first second of speech.
- **The ~2.2 s `dialing → alerting` gap is the RAN, not us.** `AT+QCFG="ims"`
  reports `1,0` (IMS enabled) and the serving cell is `NR5G-SA`, so placing a
  voice call triggers **EPS fallback** from 5G-SA down to LTE before the INVITE
  goes out. That is inherent to the network; the only lever would be forcing
  LTE-only, which would cost 5G data throughput — a bad trade.

So call setup cannot be meaningfully shortened from here. What *was* wrong is
that the page misrepresented it, and filled it with silence:

- The panel showed **"In call" with a running timer ~0.5 s after tapping Call**,
  because it keyed off `hasActiveCalls` rather than the call's `status` field —
  while the far end rang for another ten seconds.
- The downlink carries **digital silence** during setup (RMS < 300 until
  10.72 s): this network sends no early media, so there was no ringback at all.
- The dialing panel had **no hangup button**, so a call in progress could not be
  cancelled until it connected.

Fixed in `phone.html`: `callState()` / `isConnected()` drive a real
dialing → ringing → connected progression off `AT+CLCC`'s status, a locally
generated ringback (450 Hz, 1 s on / 4 s off — the Chinese cadence, on its own
`AudioContext` so it cannot disturb the call path) plays while an outgoing call
is not yet connected, and the pre-connect panel gets a cancel button.

## 6. The browser half, measured

Everything above §5 was measured on the device, over loopback, which is why the
browser's own buffers kept being listed as "not measured" — and why one of them
was quietly wrong for months. `misc/browser_audio_probe.html` closes that gap:
it builds the real audio graph in headless Chrome with a fake mic and reports
what the browser actually gave it. Two steps, since driving Chrome as a child
process proved unreliable:

    python3 misc/browser_audio_probe_serve.py      # prints the Chrome command
    # then run that command in another shell; results print in the first

    ctx.sampleRate            16000     <- the 16k request IS granted
    ctx.baseLatency            8.0 ms
    scriptProcessor(1024)     64.0 ms   <- what the page was using
    scriptProcessor(256)      16.0 ms
    audioWorklet quantum       8.0 ms   (128 frames, fixed)
    mic track                 48000 Hz, latency 10 ms

A `ScriptProcessor` cannot emit until its buffer fills, so its size is added mic
delay one for one. At a 16kHz context, 1024 frames is 64ms — the code's own
comment assumed a 48kHz context and so under-reported it by 3x.

**Fix: `app/static/mic-worklet.js`,** an `AudioWorkletProcessor` whose render
quantum is fixed at 128 frames = **8ms** here. That is **56ms off the uplink**,
and it moves the mic off the main thread, so Alpine re-renders and status
polling can no longer jitter it. The box-average downsampler moves with it
(unchanged, and still a straight copy when the context is already at 16k), and
mute is applied on the audio thread as well as in the page, so a frame already
in flight is muted too.

Verified in Chrome against the real file: `addModule` ok, 125.9 messages/s of
128 samples (8.0 ms each), 16115 samples/s delivered (nothing dropped), audio
present (peak 0.948 from the fake device), and with mute asserted 126/126
messages fully silent. A `ScriptProcessor` fallback remains for browsers without
`AudioWorklet`, but sized from the context's real rate (~16ms) instead of a
hard-coded 1024 frames.

### Still not measurable from here

`misc/wifi_jitter.py` was written to measure real downlink arrival jitter from a
client, which is what should size `FLOOR`/`CEIL` in `playChunk()`. It cannot run
from this workstation: the dev Mac is on a different subnet from the test devices
and reaches them through their WAN side, where the firewall drops :9444 (:9443
answers, :9444 is refused). Run it from a laptop **on the device's own WiFi**:

    # token from the device; a UAC-card box clocks without a call
    wifi_jitter.py 192.168.x.x <token> --secs 30

Until that number exists, `FLOOR` (40 ms) stays where it is. Lowering a jitter
cushion using loopback numbers would be exactly the mistake §4.1 documents.

### Deliberately not done

- **10 ms ALSA periods** (`CA_PERIOD_FRAMES` 320 → 160). Worth ~5 ms of
  quantisation on each side, which is a quarter of this harness's ±20 ms
  resolution — unverifiable, against doubled TLS-record and syscall rates. Not
  worth shipping an unmeasurable change for.
- **20 ms uplink POST cadence.** Already tested and rejected in §4.2: the loop
  is ALSA-bound, so the batches stay at 40 ms regardless of the timer.

