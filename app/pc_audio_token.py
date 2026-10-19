"""Shared-secret tokens for the pcat-call-audio daemon.

The C audio daemon (src/callaudio) serves the two in-call audio endpoints on
:9444, separately from Flask. Being a different port it is a different origin
and cannot see the Flask session cookie, so it cannot reuse @api_auth_required.

Rather than reimplement itsdangerous' signed-cookie format in C, Flask -- which
has already authenticated the user -- mints a short-lived token that the daemon
verifies against a shared secret:

    <expiry_unix>.<hex hmac-sha256(secret, expiry)>

The secret is generated once at first start into a 0600 file that both sides
read. The expiry is inside the MAC, so a client cannot extend its own token,
and a leaked one stops working by itself.

Tokens are deliberately short-lived but longer than a call: the page fetches
one when audio connects, and a call outliving the token would drop audio
mid-sentence. TOKEN_TTL is the ceiling on how long a leaked token is useful.
"""

import hashlib
import hmac
import os
import secrets
import time

SECRET_PATH = "/etc/pcat-manager-web/audio_token.key"
TOKEN_TTL = 4 * 3600        # 4h: comfortably longer than any call

_secret = None


def _load_or_create():
    """Reads the shared secret, generating it on first use.

    Written 0600 before any content goes in, so the key is never briefly
    world-readable. A pre-existing file is never overwritten -- doing so would
    invalidate every token the running daemon still accepts.
    """
    global _secret
    if _secret is not None:
        return _secret
    try:
        with open(SECRET_PATH, "rb") as fh:
            data = fh.read().strip()
        if len(data) >= 16:
            _secret = data
            return _secret
    except OSError:
        pass

    key = secrets.token_hex(32).encode()
    try:
        os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
        fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key + b"\n")
    except OSError:
        # No secret on disk means the daemon will refuse to start; the phone
        # page falls back to reporting audio unavailable rather than silently
        # serving an unauthenticated stream.
        return None
    _secret = key
    return _secret


def mint(ttl=TOKEN_TTL):
    """Returns a token for the audio daemon, or None if no secret exists."""
    secret = _load_or_create()
    if not secret:
        return None
    expiry = str(int(time.time()) + ttl)
    sig = hmac.new(secret, expiry.encode(), hashlib.sha256).hexdigest()
    return expiry + "." + sig


def verify(token):
    """Mirror of the daemon's check. Only used by tests and by Flask if it
    ever needs to validate a token it did not mint."""
    secret = _load_or_create()
    if not secret or not token or "." not in token:
        return False
    expiry, _, sig = token.partition(".")
    if not expiry.isdigit():
        return False
    if int(expiry) < time.time():
        return False
    want = hmac.new(secret, expiry.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)
