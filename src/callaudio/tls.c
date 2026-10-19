#include "tls.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <mbedtls/ssl.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/entropy.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/x509_crt.h>
#include <mbedtls/pk.h>
#include <mbedtls/error.h>

struct ca_tls_server {
    mbedtls_net_context listen_fd;
    mbedtls_ssl_config conf;
    mbedtls_entropy_context entropy;
    mbedtls_ctr_drbg_context drbg;
    mbedtls_x509_crt cert;
    mbedtls_pk_context key;
};

struct ca_tls_conn {
    mbedtls_net_context fd;
    mbedtls_ssl_context ssl;
};

static void set_err(char *err, size_t errlen, const char *fmt, ...)
{
    if (!err || errlen == 0)
        return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, errlen, fmt, ap);
    va_end(ap);
}

static void mbed_err(char *err, size_t errlen, const char *what, int rc)
{
    char buf[128];
    mbedtls_strerror(rc, buf, sizeof(buf));
    set_err(err, errlen, "%s: %s (-0x%04x)", what, buf, (unsigned)-rc);
}

struct ca_tls_server *ca_tls_listen(int port, const char *cert, const char *key,
                                    char *err, size_t errlen)
{
    struct ca_tls_server *s = calloc(1, sizeof(*s));
    if (!s) {
        set_err(err, errlen, "out of memory");
        return NULL;
    }

    mbedtls_net_init(&s->listen_fd);
    mbedtls_ssl_config_init(&s->conf);
    mbedtls_entropy_init(&s->entropy);
    mbedtls_ctr_drbg_init(&s->drbg);
    mbedtls_x509_crt_init(&s->cert);
    mbedtls_pk_init(&s->key);

    int rc = mbedtls_ctr_drbg_seed(&s->drbg, mbedtls_entropy_func, &s->entropy,
                                   (const unsigned char *)"pcat-call-audio", 15);
    if (rc != 0) {
        mbed_err(err, errlen, "ctr_drbg_seed", rc);
        goto fail;
    }

    rc = mbedtls_x509_crt_parse_file(&s->cert, cert);
    if (rc != 0) {
        mbed_err(err, errlen, "load cert", rc);
        goto fail;
    }
#if MBEDTLS_VERSION_MAJOR >= 3
    rc = mbedtls_pk_parse_keyfile(&s->key, key, NULL, mbedtls_ctr_drbg_random, &s->drbg);
#else
    rc = mbedtls_pk_parse_keyfile(&s->key, key, NULL);
#endif
    if (rc != 0) {
        mbed_err(err, errlen, "load key", rc);
        goto fail;
    }

    char portstr[8];
    snprintf(portstr, sizeof(portstr), "%d", port);
    rc = mbedtls_net_bind(&s->listen_fd, NULL, portstr, MBEDTLS_NET_PROTO_TCP);
    if (rc != 0) {
        mbed_err(err, errlen, "bind", rc);
        goto fail;
    }

    rc = mbedtls_ssl_config_defaults(&s->conf, MBEDTLS_SSL_IS_SERVER,
                                     MBEDTLS_SSL_TRANSPORT_STREAM,
                                     MBEDTLS_SSL_PRESET_DEFAULT);
    if (rc != 0) {
        mbed_err(err, errlen, "config_defaults", rc);
        goto fail;
    }
    mbedtls_ssl_conf_rng(&s->conf, mbedtls_ctr_drbg_random, &s->drbg);
    /* Paired with the recv_timeout BIO below: bounds how long a keep-alive
     * connection may sit idle between requests. */
    mbedtls_ssl_conf_read_timeout(&s->conf, CA_TLS_IDLE_MS);
    rc = mbedtls_ssl_conf_own_cert(&s->conf, &s->cert, &s->key);
    if (rc != 0) {
        mbed_err(err, errlen, "conf_own_cert", rc);
        goto fail;
    }
    return s;

fail:
    ca_tls_server_free(s);
    return NULL;
}

void ca_tls_server_free(struct ca_tls_server *s)
{
    if (!s)
        return;
    mbedtls_net_free(&s->listen_fd);
    mbedtls_x509_crt_free(&s->cert);
    mbedtls_pk_free(&s->key);
    mbedtls_ssl_config_free(&s->conf);
    mbedtls_ctr_drbg_free(&s->drbg);
    mbedtls_entropy_free(&s->entropy);
    free(s);
}

struct ca_tls_conn *ca_tls_accept(struct ca_tls_server *s, char *err, size_t errlen)
{
    struct ca_tls_conn *c = calloc(1, sizeof(*c));
    if (!c) {
        set_err(err, errlen, "out of memory");
        return NULL;
    }
    mbedtls_net_init(&c->fd);
    mbedtls_ssl_init(&c->ssl);

    int rc = mbedtls_net_accept(&s->listen_fd, &c->fd, NULL, 0, NULL);
    if (rc != 0) {
        mbed_err(err, errlen, "accept", rc);
        goto fail;
    }
    rc = mbedtls_ssl_setup(&c->ssl, &s->conf);
    if (rc != 0) {
        mbed_err(err, errlen, "ssl_setup", rc);
        goto fail;
    }
    /* recv_timeout rather than plain recv, so conf_read_timeout applies. */
    mbedtls_ssl_set_bio(&c->ssl, &c->fd, mbedtls_net_send, NULL,
                        mbedtls_net_recv_timeout);

    while ((rc = mbedtls_ssl_handshake(&c->ssl)) != 0) {
        if (rc != MBEDTLS_ERR_SSL_WANT_READ && rc != MBEDTLS_ERR_SSL_WANT_WRITE) {
            mbed_err(err, errlen, "handshake", rc);
            goto fail;
        }
    }
    return c;

fail:
    ca_tls_conn_free(c);
    return NULL;
}

void ca_tls_conn_free(struct ca_tls_conn *c)
{
    if (!c)
        return;
    mbedtls_ssl_close_notify(&c->ssl);
    mbedtls_ssl_free(&c->ssl);
    mbedtls_net_free(&c->fd);
    free(c);
}

int ca_tls_read(struct ca_tls_conn *c, void *buf, size_t len)
{
    for (;;) {
        int rc = mbedtls_ssl_read(&c->ssl, buf, len);
        if (rc == MBEDTLS_ERR_SSL_WANT_READ || rc == MBEDTLS_ERR_SSL_WANT_WRITE)
            continue;
        if (rc == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY)
            return 0;
        if (rc == MBEDTLS_ERR_SSL_TIMEOUT)
            return -2;      /* idle keep-alive connection: caller reaps it */
        if (rc < 0)
            return -1;
        return rc;
    }
}

int ca_tls_write(struct ca_tls_conn *c, const void *buf, size_t len)
{
    const unsigned char *p = buf;
    size_t left = len;
    while (left > 0) {
        int rc = mbedtls_ssl_write(&c->ssl, p, left);
        if (rc == MBEDTLS_ERR_SSL_WANT_READ || rc == MBEDTLS_ERR_SSL_WANT_WRITE)
            continue;
        if (rc <= 0)
            return -1;      /* peer gone: normal when the page is closed */
        p += rc;
        left -= (size_t)rc;
    }
    return 0;
}
