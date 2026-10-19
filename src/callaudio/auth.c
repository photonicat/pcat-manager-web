#include "auth.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>

#include <mbedtls/md.h>

#define SECRET_MAX 128
#define DEFAULT_SECRET_PATH "/etc/pcat-manager-web/audio_token.key"

static const char *g_secret_path = DEFAULT_SECRET_PATH;
static unsigned char g_secret[SECRET_MAX];
static size_t g_secret_len;

void ca_auth_set_secret_path(const char *path)
{
    if (path && *path)
        g_secret_path = path;
}

int ca_auth_init(void)
{
    FILE *f = fopen(g_secret_path, "rb");
    if (!f)
        return -1;
    size_t n = fread(g_secret, 1, sizeof(g_secret), f);
    fclose(f);

    /* Trim trailing newline so the file can be written with a plain echo
     * without silently changing the key. */
    while (n > 0 && (g_secret[n - 1] == '\n' || g_secret[n - 1] == '\r'))
        n--;
    if (n < 16)
        return -1;          /* too short to be a real secret */
    g_secret_len = n;
    return 0;
}

/* Constant-time compare: a byte-at-a-time early return would leak how much
 * of a forged HMAC was correct, which is enough to forge one a byte at a
 * time. */
static int ct_equal(const char *a, const char *b, size_t len)
{
    unsigned char diff = 0;
    for (size_t i = 0; i < len; i++)
        diff |= (unsigned char)(a[i] ^ b[i]);
    return diff == 0;
}

/* Verifies "<expiry>.<hex hmac>". */
static int token_valid(const char *token)
{
    const char *dot = strchr(token, '.');
    if (!dot)
        return 0;
    size_t explen = (size_t)(dot - token);
    if (explen == 0 || explen > 20)
        return 0;

    char expiry[24];
    memcpy(expiry, token, explen);
    expiry[explen] = '\0';

    /* Reject anything non-numeric before trusting strtoll. */
    for (size_t i = 0; i < explen; i++)
        if (expiry[i] < '0' || expiry[i] > '9')
            return 0;

    long long exp = strtoll(expiry, NULL, 10);
    if ((long long)time(NULL) > exp)
        return 0;               /* expired */

    const char *sig = dot + 1;
    size_t siglen = strcspn(sig, "&\r\n ");
    if (siglen != 64)
        return 0;               /* hex sha256 is 64 chars */

    unsigned char mac[32];
    const mbedtls_md_info_t *md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!md)
        return 0;
    if (mbedtls_md_hmac(md, g_secret, g_secret_len,
                        (const unsigned char *)expiry, explen, mac) != 0)
        return 0;

    char hex[65];
    for (int i = 0; i < 32; i++)
        snprintf(hex + i * 2, 3, "%02x", mac[i]);

    return ct_equal(hex, sig, 64);
}

int ca_auth_check(const char *head, const char *path)
{
    if (g_secret_len == 0)
        return 0;

    /* Query-string form: ...?token=<t> or ...&token=<t> */
    const char *q = strchr(path, '?');
    while (q) {
        const char *p = q + 1;
        if (strncmp(p, "token=", 6) == 0)
            return token_valid(p + 6);
        q = strchr(p, '&');
    }

    /* Header form: X-Audio-Token: <t> */
    const char *p = head;
    while ((p = strchr(p, '\n')) != NULL) {
        p++;
        if (strncasecmp(p, "x-audio-token:", 14) == 0) {
            const char *v = p + 14;
            while (*v == ' ')
                v++;
            return token_valid(v);
        }
    }
    return 0;
}
