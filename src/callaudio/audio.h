/* ALSA capture/playback for RM500Q call audio.
 *
 * Replaces the arecord/aplay subprocesses that pc_call_audio.py drove over
 * pipes. Two buffers disappear with the processes: the 64KB kernel pipe on
 * each side, and Python's BufferedReader read-ahead on top of the capture
 * pipe. What remains is the ALSA ring itself, which we size explicitly here
 * rather than inheriting the card's default (that default is what put
 * seconds of delay into the downlink).
 *
 * Latency policy is unchanged from the Python version, because it was the
 * policy and not the language that fixed the 5s delay: everything between
 * the modem and the browser is a FIFO, so a stall becomes *permanent* added
 * delay unless something discards the backlog. capture_read() therefore
 * reports how far ahead of the wire clock it is running and the caller sheds
 * accordingly -- see rx_lead_secs().
 */
#ifndef CALLAUDIO_AUDIO_H
#define CALLAUDIO_AUDIO_H

#include <stddef.h>
#include <stdint.h>

/* Wire format of the RM500Q UAC endpoint: 16k mono S16_LE. */
#define CA_RATE        16000
#define CA_CHANNELS    1
#define CA_BYTES_PER_FRAME 2

/* One ALSA period = the streaming quantum, 20ms. Audio is forwarded as soon
 * as a period lands rather than pooled into a larger block. */
#define CA_PERIOD_FRAMES 320
#define CA_PERIOD_BYTES  (CA_PERIOD_FRAMES * CA_BYTES_PER_FRAME)

/* Ring sizes. Capture is the downlink delay floor; playback is both the
 * uplink floor and (because ALSA's stop_threshold is the buffer) the cost of
 * an underrun. Kept at the values the Python fix settled on so this is a
 * like-for-like swap: 80ms capture, 120ms playback. */
#define CA_CAPTURE_BUFFER_FRAMES  (CA_RATE * 80 / 1000)
#define CA_PLAYBACK_BUFFER_FRAMES (CA_RATE * 120 / 1000)

/* How far the downlink may run ahead of the wire clock before the excess is
 * treated as buffered backlog and dropped. */
#define CA_MAX_LAG_SECS 0.25

/* The uplink needs the same policy in the other direction, and for a reason
 * that is not obvious: the playback ring drains at exactly the wire clock and
 * the browser produces at exactly real time, so whatever backlog builds up
 * ONCE stays there for the whole call -- nothing in a pure FIFO ever gives it
 * back. The first POST of a call reliably builds one, because it carries
 * however much mic audio piled up during connection setup.
 *
 * So this is enforced as a hard ceiling on queued uplink audio, not just a
 * cleanup threshold: anything that would push the ring past it is dropped from
 * the OLD end, because the newest audio is the only part still worth hearing.
 * Measured both ways on two modules -- an unbounded ring settled at 117ms on
 * one box and 135ms on the other, and every millisecond of it was audible
 * delay on top of the cellular path. */
#define CA_MAX_UPLINK_DELAY_MS 80

/* How long capture tolerates consecutive -EIO reads before giving up, in
 * 100ms retry steps. On the PCM/SAI cards the modem only clocks the link
 * while a call is active, and the dialer side reaches "active" a beat after
 * the page opens the stream -- reading before the clock runs is EIO, not a
 * dead card. The phone page never reopens a stream that ends, so bailing on
 * the first EIO (what arecord did) killed downlink audio for the whole
 * call. 60s covers the longest plausible setup window while still reaping
 * the thread if the card is truly wedged. */
#define CA_EIO_MAX_RETRIES 600

/* Card selection. The module's own USB audio function wins when present:
 * RM500Q-GL cannot carry call audio over the board's PCM/SAI link at all,
 * but with UAC enabled in its USB composition it exposes this card. */
#define CA_UAC_CARD "RM500QGL"
#define CA_PCM_CARD "modem"

struct ca_capture;
struct ca_playback;

/* Returns a malloc'd ALSA device string ("plughw:CARD=...") for whichever
 * card is present, or NULL if neither is. Caller frees. Re-resolved per
 * stream, so a module re-enumeration only affects the next call. */
char *ca_device_name(void);

/* True when either card is enumerated. */
int ca_card_present(void);

/* ---- capture (downlink: modem -> browser) ---- */

struct ca_capture *ca_capture_open(const char *device, char *err, size_t errlen);
void ca_capture_close(struct ca_capture *c);

/* Reads exactly one period. Returns bytes read (CA_PERIOD_BYTES), 0 on a
 * recoverable xrun that was handled, or -1 on a fatal error. Blocks until
 * the wire clocks the data. */
int ca_capture_read(struct ca_capture *c, void *buf, char *err, size_t errlen);

/* Seconds of audio this stream has drained faster than real time -- i.e. how
 * much was sitting buffered ahead of us. The caller drops chunks while this
 * exceeds CA_MAX_LAG_SECS. Call ca_capture_drop() when it does, so the
 * credit is repaid. */
double ca_capture_lead(const struct ca_capture *c);
void ca_capture_drop(struct ca_capture *c, size_t bytes);

/* ---- playback (uplink: browser -> modem) ---- */

struct ca_playback *ca_playback_open(const char *device, char *err, size_t errlen);
void ca_playback_close(struct ca_playback *p);

/* Writes PCM, recovering from underruns. Returns 0 on success, -1 fatal. */
int ca_playback_write(struct ca_playback *p, const void *buf, size_t bytes,
                      char *err, size_t errlen);

/* ---- instrumentation ---- */

/* Milliseconds of audio currently sitting in the card's ring: for playback,
 * how long until the newest sample is heard; for capture, how long the oldest
 * unread sample has been waiting. This is the ring's own contribution to
 * one-way delay, which is otherwise invisible -- byte counts look identical
 * whether the ring holds 20ms or 200ms. -1 if the card cannot report it. */
int ca_playback_delay_ms(struct ca_playback *p);
int ca_capture_delay_ms(struct ca_capture *c);

/* Discards whatever is queued but not yet played, so the next write is heard
 * immediately. Returns the ms discarded, or -1. */
int ca_playback_trim(struct ca_playback *p);

#endif /* CALLAUDIO_AUDIO_H */
