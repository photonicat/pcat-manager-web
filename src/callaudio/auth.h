/* Token auth for the audio daemon.
 *
 * The daemon answers on a different port than Flask, so it cannot see the
 * Flask session cookie -- and reimplementing itsdangerous' signed-cookie
 * scheme in C would be a lot of crypto to get subtly wrong. Instead Flask,
 * which has already authenticated the user, mints a short-lived token and
 * hands it to the phone page; the page passes it here.
 *
 * Token format:  <expiry_unix>.<hex hmac-sha256(secret, expiry)>
 *
 * Both sides share a secret file written at startup by the Flask app (mode
 * 0600, /etc/pcat-manager-web/audio_token.key). Verification is a constant-
 * time compare of the HMAC plus an expiry check, so a leaked token stops
 * working on its own and cannot be extended by the client.
 *
 * Accepted either as `?token=...` on the URL (needed for the streaming GET,
 * since fetch cannot set headers on some paths cleanly) or an
 * X-Audio-Token header.
 */
#ifndef CALLAUDIO_AUTH_H
#define CALLAUDIO_AUTH_H

/* Overrides the default secret path. Call before ca_auth_init(). */
void ca_auth_set_secret_path(const char *path);

/* Loads the shared secret. Returns 0 on success, -1 if it is missing or
 * unreadable -- in which case the daemon must refuse to start rather than
 * serve call audio to anyone who asks. */
int ca_auth_init(void);

/* True when the request carries a valid, unexpired token. `head` is the raw
 * request head (for the header form), `path` the request target (for the
 * query-string form). */
int ca_auth_check(const char *head, const char *path);

#endif /* CALLAUDIO_AUTH_H */
