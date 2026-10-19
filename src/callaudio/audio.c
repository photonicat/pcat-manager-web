#include "audio.h"

#include <alsa/asoundlib.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

struct ca_capture {
    snd_pcm_t *pcm;
    double lead;            /* seconds drained faster than real time */
    double last;            /* monotonic timestamp of the previous read */
    int primed;
    int eio_streak;         /* consecutive -EIO reads, see CA_EIO_MAX_RETRIES */
};

struct ca_playback {
    snd_pcm_t *pcm;
};

static double now_secs(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void set_err(char *err, size_t errlen, const char *fmt, ...)
{
    if (!err || errlen == 0)
        return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, errlen, fmt, ap);
    va_end(ap);
}

/* ------------------------------------------------------------------ card */

static int card_exists(const char *name)
{
    char path[128];
    snprintf(path, sizeof(path), "/proc/asound/%s", name);
    return access(path, F_OK) == 0;
}

int ca_card_present(void)
{
    return card_exists(CA_UAC_CARD) || card_exists(CA_PCM_CARD);
}

char *ca_device_name(void)
{
    const char *card = NULL;
    if (card_exists(CA_UAC_CARD))
        card = CA_UAC_CARD;
    else if (card_exists(CA_PCM_CARD))
        card = CA_PCM_CARD;
    if (!card)
        return NULL;

    /* plughw, not hw: ALSA then inserts rate/format conversion if a future
     * module clocks the link at something other than 16k mono S16. Matches
     * what arecord/aplay were given, so this stays a like-for-like swap. */
    char *buf = malloc(64);
    if (!buf)
        return NULL;
    snprintf(buf, 64, "plughw:CARD=%s", card);
    return buf;
}

/* --------------------------------------------------------------- params */

/* Common hw/sw setup for both directions. `buffer_frames` sizes the ring;
 * period is always CA_PERIOD_FRAMES so the streaming quantum is 20ms.
 *
 * ALSA may not grant exactly what we ask -- it rounds to what the hardware
 * supports -- so the granted values are read back and returned. A card that
 * insists on a much deeper buffer than requested is exactly the failure we
 * are trying to detect, so the caller logs it rather than assuming.
 */
static int configure(snd_pcm_t *pcm, snd_pcm_uframes_t buffer_frames,
                     snd_pcm_uframes_t *got_buffer, snd_pcm_uframes_t *got_period,
                     char *err, size_t errlen)
{
    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);

    int rc = snd_pcm_hw_params_any(pcm, hw);
    if (rc < 0) {
        set_err(err, errlen, "hw_params_any: %s", snd_strerror(rc));
        return -1;
    }
    rc = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
    if (rc < 0) {
        set_err(err, errlen, "set_access: %s", snd_strerror(rc));
        return -1;
    }
    rc = snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S16_LE);
    if (rc < 0) {
        set_err(err, errlen, "set_format S16_LE: %s", snd_strerror(rc));
        return -1;
    }
    rc = snd_pcm_hw_params_set_channels(pcm, hw, CA_CHANNELS);
    if (rc < 0) {
        set_err(err, errlen, "set_channels: %s", snd_strerror(rc));
        return -1;
    }
    unsigned int rate = CA_RATE;
    rc = snd_pcm_hw_params_set_rate_near(pcm, hw, &rate, NULL);
    if (rc < 0) {
        set_err(err, errlen, "set_rate %d: %s", CA_RATE, snd_strerror(rc));
        return -1;
    }

    snd_pcm_uframes_t period = CA_PERIOD_FRAMES;
    rc = snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, NULL);
    if (rc < 0) {
        set_err(err, errlen, "set_period_size: %s", snd_strerror(rc));
        return -1;
    }
    snd_pcm_uframes_t buffer = buffer_frames;
    rc = snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer);
    if (rc < 0) {
        set_err(err, errlen, "set_buffer_size: %s", snd_strerror(rc));
        return -1;
    }
    rc = snd_pcm_hw_params(pcm, hw);
    if (rc < 0) {
        set_err(err, errlen, "hw_params: %s", snd_strerror(rc));
        return -1;
    }

    snd_pcm_hw_params_get_period_size(hw, &period, NULL);
    snd_pcm_hw_params_get_buffer_size(hw, &buffer);
    if (got_period)
        *got_period = period;
    if (got_buffer)
        *got_buffer = buffer;
    return 0;
}

/* -------------------------------------------------------------- capture */

struct ca_capture *ca_capture_open(const char *device, char *err, size_t errlen)
{
    struct ca_capture *c = calloc(1, sizeof(*c));
    if (!c) {
        set_err(err, errlen, "out of memory");
        return NULL;
    }
    int rc = snd_pcm_open(&c->pcm, device, SND_PCM_STREAM_CAPTURE, 0);
    if (rc < 0) {
        set_err(err, errlen, "open %s: %s", device, snd_strerror(rc));
        free(c);
        return NULL;
    }
    snd_pcm_uframes_t buffer = 0, period = 0;
    if (configure(c->pcm, CA_CAPTURE_BUFFER_FRAMES, &buffer, &period,
                  err, errlen) < 0) {
        snd_pcm_close(c->pcm);
        free(c);
        return NULL;
    }
    fprintf(stderr, "call-audio: capture %s buffer=%lu frames (%.0fms) "
            "period=%lu frames (%.0fms)\n", device,
            (unsigned long)buffer, buffer * 1000.0 / CA_RATE,
            (unsigned long)period, period * 1000.0 / CA_RATE);

    rc = snd_pcm_prepare(c->pcm);
    if (rc < 0) {
        set_err(err, errlen, "prepare: %s", snd_strerror(rc));
        snd_pcm_close(c->pcm);
        free(c);
        return NULL;
    }
    return c;
}

void ca_capture_close(struct ca_capture *c)
{
    if (!c)
        return;
    if (c->pcm) {
        snd_pcm_drop(c->pcm);
        snd_pcm_close(c->pcm);
    }
    free(c);
}

int ca_capture_read(struct ca_capture *c, void *buf, char *err, size_t errlen)
{
    snd_pcm_sframes_t n = snd_pcm_readi(c->pcm, buf, CA_PERIOD_FRAMES);
    if (n < 0) {
        if (n == -EPIPE) {
            /* Overrun: the ring filled because we were not reading. The
             * audio in it is stale by definition, so recovering (which
             * discards it) is exactly right for a live call. */
            snd_pcm_prepare(c->pcm);
            return 0;
        }
        if (n == -EAGAIN)
            return 0;
        if (n == -ESTRPIPE) {
            while (snd_pcm_resume(c->pcm) == -EAGAIN)
                usleep(10000);
            snd_pcm_prepare(c->pcm);
            return 0;
        }
        if (n == -EIO && c->eio_streak < CA_EIO_MAX_RETRIES) {
            /* SAI cards read EIO while the modem is not clocking the link,
             * which on the dialer side lasts until the call goes active --
             * typically right after the page has already opened the stream.
             * Treat it as "no data yet", not a dead card; the streak bound
             * keeps a genuinely wedged card from pinning the thread. */
            c->eio_streak++;
            snd_pcm_prepare(c->pcm);
            usleep(100000);
            return 0;
        }
        set_err(err, errlen, "readi: %s", snd_strerror((int)n));
        return -1;
    }
    c->eio_streak = 0;

    size_t bytes = (size_t)n * CA_BYTES_PER_FRAME;

    /* Lead accounting, ported from the Python fix. A healthy read blocks
     * until the wire clocks the next period, so it costs about one period of
     * wall time. A read that returns much faster got data that was already
     * queued -- we are draining a backlog. Bank that credit; the caller
     * sheds once it exceeds CA_MAX_LAG_SECS. Computed from the actual frame
     * count so a short read cannot skew it. */
    double t = now_secs();
    double chunk_secs = (double)n / CA_RATE;
    if (!c->primed) {
        c->primed = 1;          /* no previous read to measure against */
    } else {
        double elapsed = t - c->last;
        c->lead += chunk_secs - elapsed;
        if (c->lead < 0.0)
            c->lead = 0.0;      /* at or behind the wire: nothing to shed */
    }
    c->last = t;

    return (int)bytes;
}

double ca_capture_lead(const struct ca_capture *c)
{
    return c ? c->lead : 0.0;
}

void ca_capture_drop(struct ca_capture *c, size_t bytes)
{
    if (!c)
        return;
    c->lead -= (double)(bytes / CA_BYTES_PER_FRAME) / CA_RATE;
    if (c->lead < 0.0)
        c->lead = 0.0;
}

/* ------------------------------------------------------------- playback */

struct ca_playback *ca_playback_open(const char *device, char *err, size_t errlen)
{
    struct ca_playback *p = calloc(1, sizeof(*p));
    if (!p) {
        set_err(err, errlen, "out of memory");
        return NULL;
    }
    int rc = snd_pcm_open(&p->pcm, device, SND_PCM_STREAM_PLAYBACK, 0);
    if (rc < 0) {
        set_err(err, errlen, "open %s: %s", device, snd_strerror(rc));
        free(p);
        return NULL;
    }
    snd_pcm_uframes_t buffer = 0, period = 0;
    if (configure(p->pcm, CA_PLAYBACK_BUFFER_FRAMES, &buffer, &period,
                  err, errlen) < 0) {
        snd_pcm_close(p->pcm);
        free(p);
        return NULL;
    }

    /* aplay set start_threshold and stop_threshold to the whole buffer,
     * which is why the first word was eaten and why every underrun cost a
     * further bufferful of silence. Set them explicitly instead: start after
     * one period, and never auto-stop on underrun (stop_threshold at the
     * boundary) so a late POST does not tear the stream down. */
    snd_pcm_sw_params_t *sw;
    snd_pcm_sw_params_alloca(&sw);
    snd_pcm_sw_params_current(p->pcm, sw);
    snd_pcm_sw_params_set_start_threshold(p->pcm, sw, period);
    snd_pcm_sw_params_set_stop_threshold(p->pcm, sw, buffer);
    snd_pcm_sw_params_set_avail_min(p->pcm, sw, period);
    rc = snd_pcm_sw_params(p->pcm, sw);
    if (rc < 0) {
        set_err(err, errlen, "sw_params: %s", snd_strerror(rc));
        snd_pcm_close(p->pcm);
        free(p);
        return NULL;
    }

    fprintf(stderr, "call-audio: playback %s buffer=%lu frames (%.0fms) "
            "period=%lu frames (%.0fms) start_threshold=%lu\n", device,
            (unsigned long)buffer, buffer * 1000.0 / CA_RATE,
            (unsigned long)period, period * 1000.0 / CA_RATE,
            (unsigned long)period);

    rc = snd_pcm_prepare(p->pcm);
    if (rc < 0) {
        set_err(err, errlen, "prepare: %s", snd_strerror(rc));
        snd_pcm_close(p->pcm);
        free(p);
        return NULL;
    }
    return p;
}

void ca_playback_close(struct ca_playback *p)
{
    if (!p)
        return;
    if (p->pcm) {
        snd_pcm_drop(p->pcm);
        snd_pcm_close(p->pcm);
    }
    free(p);
}

static int delay_ms(snd_pcm_t *pcm)
{
    snd_pcm_sframes_t d = 0;
    if (!pcm || snd_pcm_delay(pcm, &d) < 0)
        return -1;
    if (d < 0)
        d = 0;
    return (int)(d * 1000 / CA_RATE);
}

int ca_playback_delay_ms(struct ca_playback *p)
{
    return p ? delay_ms(p->pcm) : -1;
}

int ca_capture_delay_ms(struct ca_capture *c)
{
    return c ? delay_ms(c->pcm) : -1;
}

int ca_playback_trim(struct ca_playback *p)
{
    if (!p || !p->pcm)
        return -1;
    int had = delay_ms(p->pcm);
    /* drop, not drain: drain would play the backlog out first, which is the
     * opposite of what we want. prepare() puts the stream back in a writable
     * state, and start_threshold (one period) restarts it on the next write. */
    if (snd_pcm_drop(p->pcm) < 0)
        return -1;
    if (snd_pcm_prepare(p->pcm) < 0)
        return -1;
    return had;
}

int ca_playback_write(struct ca_playback *p, const void *buf, size_t bytes,
                      char *err, size_t errlen)
{
    const char *cur = buf;
    snd_pcm_uframes_t left = bytes / CA_BYTES_PER_FRAME;

    while (left > 0) {
        snd_pcm_sframes_t n = snd_pcm_writei(p->pcm, cur, left);
        if (n < 0) {
            if (n == -EPIPE) {
                /* Underrun: the browser was late. Recover and keep going --
                 * dropping the stream here is what cost a bufferful of
                 * silence in the aplay version. */
                snd_pcm_prepare(p->pcm);
                continue;
            }
            if (n == -ESTRPIPE) {
                while (snd_pcm_resume(p->pcm) == -EAGAIN)
                    usleep(10000);
                snd_pcm_prepare(p->pcm);
                continue;
            }
            if (n == -EAGAIN)
                continue;
            set_err(err, errlen, "writei: %s", snd_strerror((int)n));
            return -1;
        }
        cur += (size_t)n * CA_BYTES_PER_FRAME;
        left -= (snd_pcm_uframes_t)n;
    }
    return 0;
}
