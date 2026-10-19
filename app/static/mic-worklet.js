/* Microphone capture for the phone page's uplink.
 *
 * Replaces createScriptProcessor(1024, ...). That node cannot emit until its
 * buffer fills, so its size is added mic delay one for one -- and because
 * phone.html asks for a 16kHz AudioContext (and Chrome grants it, measured),
 * 1024 frames was 64ms, not the ~21ms a 48kHz context would have cost. An
 * AudioWorklet's render quantum is fixed at 128 frames = 8ms here, so this is
 * 56ms straight off the uplink.
 *
 * It also runs on the audio thread rather than the main thread, so Alpine
 * re-renders and status polling can no longer jitter the mic path.
 *
 * Posts Int16 frames; the page batches them into its 40ms uplink POSTs.
 */

class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const target = (options && options.processorOptions &&
                    options.processorOptions.targetRate) || 16000;
    // sampleRate is the context's, provided by the worklet global scope. When
    // the context is already at the modem's rate this ratio is 1 and the loop
    // below is a straight copy.
    this.ratio = sampleRate / target;
    this.acc = 0;
    this.accN = 0;
    this.readPos = 0;
    this.muted = false;
    this.port.onmessage = (e) => {
      if (e.data && typeof e.data.muted === 'boolean') this.muted = e.data.muted;
    };
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || ch.length === 0) return true;

    const out = new Int16Array(Math.ceil(ch.length / this.ratio) + 2);
    let n = 0;
    let peak = 0;
    for (let i = 0; i < ch.length; i++) {
      const v = ch[i];
      const a = v < 0 ? -v : v;
      if (a > peak) peak = a;
      // Box-average across each output sample rather than picking one input
      // sample per ratio: nearest-neighbour decimation has no anti-aliasing,
      // and folding 48k speech to 16k that way is audibly garbled.
      this.acc += v;
      this.accN++;
      this.readPos += 1;
      if (this.readPos >= this.ratio) {
        const m = this.acc / this.accN;
        let s = Math.round(m * 32767);
        if (s > 32767) s = 32767;
        else if (s < -32768) s = -32768;
        out[n++] = this.muted ? 0 : s;
        this.acc = 0;
        this.accN = 0;
        this.readPos -= this.ratio;
      }
    }
    if (n > 0) {
      const frame = out.subarray(0, n);
      // Copy into a right-sized buffer so it can be transferred rather than
      // structured-cloned; this runs 125x a second.
      const buf = new Int16Array(frame).buffer;
      this.port.postMessage({ pcm: buf, peak: this.muted ? 0 : peak }, [buf]);
    }
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
