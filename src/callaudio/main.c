/* pcat-call-audio -- RM500Q call audio over HTTPS, without Python.
 *
 * Serves exactly two endpoints on :9444, the same paths the phone page
 * already uses, so the browser change is only which port they point at:
 *
 *   GET  /api/v1/voice_call/audio/stream  chunked raw S16_LE 16k mono
 *   POST /api/v1/voice_call/audio/tx      raw S16_LE 16k mono
 *
 * Everything else on the phone page -- dialling, call control, history,
 * recordings, voicemail -- stays on Flask at :9443. This daemon exists only
 * to keep the interpreter out of the sample path: no arecord/aplay
 * subprocesses, no 64KB pipes, no GIL between ALSA and the socket.
 *
 * Threading: one thread per connection, but at most one stream and one
 * uplink at a time (the phone page is single-call by construction). A new
 * downlink stream displaces the previous one, matching what rx_stream() did
 * by bumping a generation counter.
 */

#include "audio.h"
#include "tls.h"
#include "auth.h"

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define PORT              9444
#define CERT_PATH         "/etc/pcat-manager-web/ssl/cert.pem"
#define KEY_PATH          "/etc/pcat-manager-web/ssl/key.pem"

#define REQ_MAX           8192
#define TX_BODY_MAX       (64 * 1024)

/* The browser's page origin. Because this daemon answers on a different port
 * than Flask, every response needs CORS -- :9443 and :9444 are separate
 * origins even on the same host. Credentials are not used (the token rides in
 * the URL/header), so a wildcard is safe and avoids echoing Origin. */
#define CORS_HEADERS \
    "Access-Control-Allow-Origin: *\r\n" \
    "Access-Control-Allow-Headers: content-type, x-audio-token\r\n" \
    "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"

static struct ca_playback *g_playback;
static pthread_mutex_t g_playback_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile unsigned g_rx_generation;
static volatile double g_last_tx;

/* Uplink telemetry, all touched under g_playback_lock. Byte counts alone could
 * not tell a healthy uplink from one running 300ms behind, which is exactly the
 * failure that hid here: the audio all arrived, just late. */
static unsigned long g_conn_reuse;      /* requests served on a reused conn */
static unsigned g_tx_posts, g_tx_win_bytes, g_tx_trims;
static unsigned g_tx_skips, g_tx_dropped_ms;
static long g_tx_ring_sum;
static unsigned g_tx_ring_n;
static double g_tx_win_t0;

static double now_secs(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void logmsg(const char *fmt, ...)
{
    char stamp[32];
    time_t t = time(NULL);
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", &tm);
    fprintf(stderr, "%s call-audio: ", stamp);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

/* ----------------------------------------------------------------- http */

/* `keep` decides whether the connection survives this response. It must,
 * for the uplink: every POST that opens a fresh connection costs a full
 * TCP+TLS handshake, which measured 125ms on this class of device -- RSA-2048
 * on a 1.8GHz A55 -- against a 2.6ms request. At the page's 25 posts/s that
 * is unpayable, so the uplink silently degraded into ~6 posts/s carrying
 * 160ms blocks, i.e. 160ms of mic audio queued before anything was even sent.
 * Reusing the connection makes the handshake a once-per-call cost. */
static void send_simple(struct ca_tls_conn *c, const char *status,
                        const char *ctype, const char *body, int keep)
{
    char hdr[512];
    int n = snprintf(hdr, sizeof(hdr),
                     "HTTP/1.1 %s\r\n"
                     "Content-Type: %s\r\n"
                     "Content-Length: %zu\r\n"
                     CORS_HEADERS
                     "Connection: %s\r\n"
                     "\r\n",
                     status, ctype, strlen(body), keep ? "keep-alive" : "close");
    if (n > 0)
        ca_tls_write(c, hdr, (size_t)n);
    if (*body)
        ca_tls_write(c, body, strlen(body));
}

/* Reads a request head (through the blank line) into buf, which already holds
 * `have` unconsumed bytes from the previous request on this connection.
 * Returns the total bytes buffered, or -1 (peer gone / idle timeout / no head
 * within cap). `head_len` receives the offset just past the blank line, so any
 * body bytes already read are buf[head_len..n).
 *
 * The scan is bounded by the byte count rather than strstr over the whole
 * buffer, because leftover bytes can be raw PCM containing NULs. */
static int read_head(struct ca_tls_conn *c, char *buf, size_t cap,
                     size_t have, size_t *head_len)
{
    size_t n = have, scanned = 0;
    for (;;) {
        for (size_t i = scanned; i + 4 <= n; i++) {
            if (memcmp(buf + i, "\r\n\r\n", 4) == 0) {
                *head_len = i + 4;
                return (int)n;
            }
        }
        scanned = n >= 3 ? n - 3 : 0;
        if (n >= cap - 1)
            return -1;
        int r = ca_tls_read(c, buf + n, cap - 1 - n);
        if (r <= 0)
            return -1;
        n += (size_t)r;
    }
}

static int header_value(const char *head, const char *name, char *out, size_t outlen)
{
    size_t namelen = strlen(name);
    const char *p = head;
    while ((p = strchr(p, '\n')) != NULL) {
        p++;
        if (strncasecmp(p, name, namelen) == 0 && p[namelen] == ':') {
            const char *v = p + namelen + 1;
            while (*v == ' ')
                v++;
            const char *e = strpbrk(v, "\r\n");
            size_t len = e ? (size_t)(e - v) : strlen(v);
            if (len >= outlen)
                len = outlen - 1;
            memcpy(out, v, len);
            out[len] = '\0';
            return 0;
        }
    }
    return -1;
}

/* -------------------------------------------------------------- downlink */

static void handle_stream(struct ca_tls_conn *c)
{
    if (!ca_card_present()) {
        send_simple(c, "503 Service Unavailable", "application/json",
                    "{\"success\":false,\"error\":\"no modem audio card\"}", 0);
        return;
    }
    char *dev = ca_device_name();
    if (!dev) {
        send_simple(c, "503 Service Unavailable", "application/json",
                    "{\"success\":false,\"error\":\"no modem audio card\"}", 0);
        return;
    }

    /* Displace any previous stream, the way rx_stream()'s generation counter
     * did: only one browser gets the call audio. */
    unsigned generation = __sync_add_and_fetch(&g_rx_generation, 1);

    char err[256] = {0};
    struct ca_capture *cap = ca_capture_open(dev, err, sizeof(err));
    free(dev);
    if (!cap) {
        logmsg("capture open failed: %s", err);
        send_simple(c, "503 Service Unavailable", "application/json",
                    "{\"success\":false,\"error\":\"capture unavailable\"}", 0);
        return;
    }

    static const char head[] =
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/octet-stream\r\n"
        "Cache-Control: no-store\r\n"
        "X-Accel-Buffering: no\r\n"
        CORS_HEADERS
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n"
        "\r\n";
    if (ca_tls_write(c, head, sizeof(head) - 1) < 0) {
        ca_capture_close(cap);
        return;
    }
    logmsg("downlink stream started");

    unsigned char pcm[CA_PERIOD_BYTES];
    unsigned long long total = 0, dropped = 0;
    double t0 = 0.0, tlast = 0.0, tlog = 0.0;

    for (;;) {
        if (generation != g_rx_generation)
            break;              /* a newer stream took over */

        int n = ca_capture_read(cap, pcm, err, sizeof(err));
        if (n < 0) {
            logmsg("capture read failed: %s", err);
            break;
        }
        if (n == 0)
            continue;           /* recovered xrun, nothing to send */

        double t = now_secs();
        if (t0 == 0.0)
            t0 = tlog = t;
        tlast = t;
        total += (unsigned long long)n;

        if (t - tlog >= 10.0) {
            tlog = t;
            logmsg("downlink: %d ms queued in card, %.0f ms lead, "
                   "%llu chunks shed so far",
                   ca_capture_delay_ms(cap), ca_capture_lead(cap) * 1000.0,
                   dropped);
        }

        /* Shed backlog rather than deliver it. Same policy as the Python
         * version and for the same reason: every stage from here to the
         * browser is a FIFO, so audio that arrived faster than the wire
         * clock is audio that was sitting buffered, and delivering it turns
         * a transient stall into permanent delay. */
        if (ca_capture_lead(cap) > CA_MAX_LAG_SECS) {
            ca_capture_drop(cap, (size_t)n);
            if (++dropped == 1 || dropped == 10 || dropped == 100)
                logmsg("downlink shedding buffered backlog (%llu chunks)", dropped);
            continue;
        }

        char chunkhdr[16];
        int hn = snprintf(chunkhdr, sizeof(chunkhdr), "%x\r\n", n);
        if (ca_tls_write(c, chunkhdr, (size_t)hn) < 0)
            break;
        if (ca_tls_write(c, pcm, (size_t)n) < 0)
            break;
        if (ca_tls_write(c, "\r\n", 2) < 0)
            break;
    }

    ca_tls_write(c, "0\r\n\r\n", 5);
    ca_capture_close(cap);

    if (t0 > 0.0 && tlast > t0) {
        double dur = tlast - t0;
        logmsg("downlink ended: %llu bytes in %.2fs = %d Hz measured "
               "(asked %d), %llu chunks shed",
               total, dur, (int)(total / 2.0 / dur), CA_RATE, dropped);
    } else {
        logmsg("downlink stream ended (no data)");
    }
}

/* ---------------------------------------------------------------- uplink */

/* Returns 0 when the connection may be reused, -1 when it must be closed. */
static int handle_tx(struct ca_tls_conn *c, const char *head,
                     const unsigned char *body, size_t have, size_t content_len)
{
    (void)head;
    if (content_len > TX_BODY_MAX) {
        send_simple(c, "413 Payload Too Large", "application/json",
                    "{\"success\":false}", 0);
        return -1;
    }
    unsigned char *buf = malloc(content_len ? content_len : 1);
    if (!buf) {
        send_simple(c, "500 Internal Server Error", "application/json",
                    "{\"success\":false}", 0);
        return -1;
    }
    if (have > content_len)
        have = content_len;
    memcpy(buf, body, have);
    while (have < content_len) {
        int r = ca_tls_read(c, buf + have, content_len - have);
        if (r <= 0) {
            free(buf);
            return -1;
        }
        have += (size_t)r;
    }

    if (!ca_card_present()) {
        free(buf);
        send_simple(c, "503 Service Unavailable", "application/json",
                    "{\"success\":false,\"error\":\"no modem audio card\"}", 0);
        return -1;
    }

    char err[256] = {0};
    pthread_mutex_lock(&g_playback_lock);
    if (!g_playback) {
        char *dev = ca_device_name();
        if (dev) {
            g_playback = ca_playback_open(dev, err, sizeof(err));
            free(dev);
            if (g_playback)
                logmsg("uplink sink started");
            else
                logmsg("uplink sink open failed: %s", err);
        }
    }
    int ok = 0;
    if (g_playback) {
        /* Shed uplink backlog before adding to it, for the same reason the
         * downlink sheds: the ring is a FIFO clocked by the modem, so audio
         * queued behind a stall is delay we would otherwise carry for the rest
         * of the call. Two stages, because the backlog can sit on either side
         * of us -- already in the card, or still in this POST's body. */
        const unsigned char *w = buf;
        size_t wlen = content_len;
        int ring = ca_playback_delay_ms(g_playback);
        if (ring > CA_MAX_UPLINK_DELAY_MS) {
            int had = ca_playback_trim(g_playback);
            ring = 0;
            if (++g_tx_trims == 1 || g_tx_trims % 10 == 0)
                logmsg("uplink: dropped %dms of queued audio from the card "
                       "(%u trims)", had, g_tx_trims);
        }
        if (ring >= 0) {
            /* Never leave more than the ceiling queued. Keep at least one
             * period so a big POST still produces continuous audio. */
            int room_ms = CA_MAX_UPLINK_DELAY_MS - ring;
            size_t room = (size_t)(room_ms > 0 ? room_ms : 0) *
                          CA_RATE / 1000 * CA_BYTES_PER_FRAME;
            if (room < CA_PERIOD_BYTES)
                room = CA_PERIOD_BYTES;
            if (wlen > room) {
                size_t drop = wlen - room;
                w += drop;
                wlen = room;
                g_tx_dropped_ms += (unsigned)(drop / 2 * 1000 / CA_RATE);
                if (++g_tx_skips == 1 || g_tx_skips % 25 == 0)
                    logmsg("uplink: skipped %ums of stale mic audio to hold the "
                           "%dms ceiling (%u skips, %ums total)",
                           (unsigned)(drop / 2 * 1000 / CA_RATE),
                           CA_MAX_UPLINK_DELAY_MS, g_tx_skips, g_tx_dropped_ms);
            }
        }
        /* Writing blocks until ALSA has room, which paces this at real time
         * for free -- the same property the Python writer thread relied on.
         * The browser posts every 40ms into a 120ms ring, so it stays fed. */
        ok = ca_playback_write(g_playback, w, wlen, err, sizeof(err)) == 0;
        if (!ok) {
            logmsg("uplink write failed: %s", err);
            ca_playback_close(g_playback);
            g_playback = NULL;
        }
    }
    double now = now_secs();
    if (ok) {
        int ring = ca_playback_delay_ms(g_playback);
        if (ring >= 0) {
            g_tx_ring_sum += ring;
            g_tx_ring_n++;
        }
        g_tx_posts++;
        g_tx_win_bytes += (unsigned)content_len;
        if (g_tx_win_t0 == 0.0) {
            g_tx_win_t0 = now;
        } else if (now - g_tx_win_t0 >= 5.0) {
            double win = now - g_tx_win_t0;
            logmsg("uplink: %u posts in %.1fs (%.1f/s), %.0fms audio per post, "
                   "card ring %ldms avg, %lu reused requests",
                   g_tx_posts, win, g_tx_posts / win,
                   g_tx_win_bytes / 2.0 / CA_RATE * 1000 /
                       (g_tx_posts ? g_tx_posts : 1),
                   g_tx_ring_n ? g_tx_ring_sum / (long)g_tx_ring_n : -1,
                   g_conn_reuse);
            g_tx_win_t0 = now;
            g_tx_posts = 0;
            g_tx_win_bytes = 0;
            g_tx_ring_sum = 0;
            g_tx_ring_n = 0;
        }
    }
    g_last_tx = now;
    pthread_mutex_unlock(&g_playback_lock);
    free(buf);

    send_simple(c, ok ? "200 OK" : "503 Service Unavailable",
                "application/json",
                ok ? "{\"success\":true}" : "{\"success\":false}", ok);
    return ok ? 0 : -1;
}

/* Closes the playback device after a period with no uplink, so a browser that
 * navigated away mid-call does not hold the card open forever. Mirrors the
 * reaper thread in pc_call_audio.py. */
static void *reaper(void *arg)
{
    (void)arg;
    for (;;) {
        sleep(2);
        pthread_mutex_lock(&g_playback_lock);
        if (g_playback && now_secs() - g_last_tx > 10.0) {
            ca_playback_close(g_playback);
            g_playback = NULL;
            logmsg("uplink sink idle, closed");
        }
        pthread_mutex_unlock(&g_playback_lock);
    }
    return NULL;
}

/* ------------------------------------------------------------ connection */

/* One thread per connection, now serving requests until the peer goes away
 * instead of exactly one. `have` carries whatever the last read pulled in past
 * the request it belonged to, so a pipelined POST is not lost. */
static void *serve(void *arg)
{
    struct ca_tls_conn *c = arg;
    char buf[REQ_MAX];
    size_t have = 0;
    unsigned served = 0;

    for (;;) {
        size_t head_len = 0;
        int n = read_head(c, buf, sizeof(buf), have, &head_len);
        if (n < 0)
            break;

        char method[8] = {0}, path[256] = {0};
        if (sscanf(buf, "%7s %255s", method, path) != 2) {
            send_simple(c, "400 Bad Request", "application/json",
                        "{\"success\":false}", 0);
            break;
        }

        /* An explicit close from the client wins over our own preference. */
        char conn_hdr[32] = {0};
        int client_close = header_value(buf, "connection", conn_hdr,
                                       sizeof(conn_hdr)) == 0 &&
                           strncasecmp(conn_hdr, "close", 5) == 0;

        size_t body_have = (size_t)n - head_len;
        size_t body_used = 0;
        int keep = !client_close;

        /* Strip any query string before matching (the token rides there), but
         * only after ca_auth_check(), which needs it. */
        int authed = ca_auth_check(buf, path);
        char *q = strchr(path, '?');
        if (q)
            *q = '\0';

        if (strcmp(method, "OPTIONS") == 0) {
            /* CORS preflight: the browser sends this before a cross-origin
             * POST with a content-type header, which our uplink is. */
            send_simple(c, "204 No Content", "text/plain", "", keep);
        } else if (!authed) {
            logmsg("rejected unauthenticated %s %s", method, path);
            send_simple(c, "403 Forbidden", "application/json",
                        "{\"success\":false,\"error\":\"forbidden\"}", 0);
            keep = 0;
        } else if (strcmp(method, "GET") == 0 &&
                   strcmp(path, "/api/v1/voice_call/audio/stream") == 0) {
            handle_stream(c);
            keep = 0;                   /* the stream owns the connection */
        } else if (strcmp(method, "POST") == 0 &&
                   strcmp(path, "/api/v1/voice_call/audio/tx") == 0) {
            char clen[32] = {0};
            size_t content_len = 0;
            if (header_value(buf, "content-length", clen, sizeof(clen)) == 0)
                content_len = strtoul(clen, NULL, 10);
            body_used = content_len < body_have ? content_len : body_have;
            if (handle_tx(c, buf, (unsigned char *)buf + head_len,
                          body_have, content_len) < 0)
                keep = 0;
        } else if (strcmp(method, "GET") == 0 &&
                   strcmp(path, "/api/v1/voice_call/audio/health") == 0) {
            send_simple(c, "200 OK", "application/json",
                        ca_card_present() ? "{\"success\":true,\"card_present\":true}"
                                          : "{\"success\":true,\"card_present\":false}",
                        keep);
        } else {
            send_simple(c, "404 Not Found", "application/json",
                        "{\"success\":false}", 0);
            keep = 0;
        }

        served++;
        if (served > 1)
            g_conn_reuse++;         /* counted as it happens, so the 5s uplink
                                     * line shows reuse during the call */
        if (!keep)
            break;
        size_t used = head_len + body_used;
        have = (size_t)n - used;
        if (have)
            memmove(buf, buf + used, have);
    }

    ca_tls_conn_free(c);
    return NULL;
}

int main(int argc, char **argv)
{
    const char *cert = CERT_PATH, *key = KEY_PATH;
    int port = PORT;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--cert") == 0 && i + 1 < argc)
            cert = argv[++i];
        else if (strcmp(argv[i], "--key") == 0 && i + 1 < argc)
            key = argv[++i];
        else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc)
            port = atoi(argv[++i]);
        else if (strcmp(argv[i], "--secret") == 0 && i + 1 < argc)
            ca_auth_set_secret_path(argv[++i]);
        else {
            fprintf(stderr,
                    "usage: %s [--port N] [--cert PEM] [--key PEM] [--secret FILE]\n",
                    argv[0]);
            return 2;
        }
    }

    /* A client that closes mid-stream would otherwise kill the daemon when
     * we write to the dead socket. */
    signal(SIGPIPE, SIG_IGN);

    if (ca_auth_init() < 0) {
        logmsg("FATAL: no auth secret available; refusing to start");
        return 1;
    }

    char err[256] = {0};
    struct ca_tls_server *srv = ca_tls_listen(port, cert, key, err, sizeof(err));
    if (!srv) {
        logmsg("FATAL: listen on :%d failed: %s", port, err);
        return 1;
    }
    logmsg("listening on :%d (cert %s)", port, cert);

    pthread_t rt;
    pthread_create(&rt, NULL, reaper, NULL);
    pthread_detach(rt);

    for (;;) {
        struct ca_tls_conn *c = ca_tls_accept(srv, err, sizeof(err));
        if (!c)
            continue;   /* failed handshake: a cert probe, normal. */
        pthread_t t;
        if (pthread_create(&t, NULL, serve, c) != 0) {
            ca_tls_conn_free(c);
            continue;
        }
        pthread_detach(t);
    }

    ca_tls_server_free(srv);
    return 0;
}
