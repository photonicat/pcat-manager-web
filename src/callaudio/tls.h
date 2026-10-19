/* Thin TLS + TCP listener wrapper.
 *
 * Deliberately narrow so the underlying library is a contained swap: the
 * daemon only ever accepts a connection, reads a request, and writes bytes.
 * Built against mbedTLS (what OpenWrt links by default); the same five
 * functions map onto OpenSSL if the target's toolchain has that instead.
 *
 * TLS is required because the phone page needs a secure context for
 * getUserMedia, and a page served over https cannot fetch plain http without
 * tripping mixed-content. The cert is the one the init script already
 * generates with px5g for the :9443 Flask listener -- same files, no new PKI.
 */
#ifndef CALLAUDIO_TLS_H
#define CALLAUDIO_TLS_H

#include <stddef.h>

/* How long a read may block before the connection is abandoned. With HTTP
 * keep-alive a thread sits in ca_tls_read() between requests, so without this
 * a browser that vanished mid-call would pin a thread and its TLS context for
 * good. Long enough that it can only fire when the peer is really gone: the
 * phone page posts uplink audio every 40ms. */
#define CA_TLS_IDLE_MS 20000

struct ca_tls_server;
struct ca_tls_conn;

/* Binds `port` and loads the cert/key PEMs. Returns NULL on failure with a
 * reason in err. */
struct ca_tls_server *ca_tls_listen(int port, const char *cert, const char *key,
                                    char *err, size_t errlen);
void ca_tls_server_free(struct ca_tls_server *s);

/* Blocks until a client connects and the handshake completes. Returns NULL
 * on a failed handshake (the common, harmless case: a browser probing the
 * self-signed cert), which the caller should log at most quietly. */
struct ca_tls_conn *ca_tls_accept(struct ca_tls_server *s, char *err, size_t errlen);
void ca_tls_conn_free(struct ca_tls_conn *c);

/* Returns bytes read, 0 on clean close, -1 on error. */
/* >0 bytes read, 0 on a clean peer close, -1 on error, -2 on idle timeout. */
int ca_tls_read(struct ca_tls_conn *c, void *buf, size_t len);

/* Writes all `len` bytes. Returns 0 on success, -1 if the peer went away --
 * which is normal mid-call (the user navigated away) and is how the streaming
 * loop learns to stop. */
int ca_tls_write(struct ca_tls_conn *c, const void *buf, size_t len);

#endif /* CALLAUDIO_TLS_H */
