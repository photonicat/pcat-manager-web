"""SQLite-backed remember-me tokens.

Uses the selector + validator pattern so cookies survive reboots and
SECRET_KEY rotations, and can be revoked server-side.

Cookie format:  "<selector>:<validator>"
Table:          remember_tokens(selector, validator_hash, username, expires_at, created_at)
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from pcat_util import pcat_logger

REMEMBER_DB_PATH = "/etc/pcat-remember.db"

_SELECTOR_BYTES = 16
_VALIDATOR_BYTES = 32
_DEFAULT_TTL_DAYS = 180


def _hash_validator(validator: str) -> str:
    return hashlib.sha256(validator.encode("utf-8")).hexdigest()


def _connect():
    return sqlite3.connect(REMEMBER_DB_PATH, timeout=5)


def init_remember_db():
    """Create the tokens table if it doesn't exist. Safe to call repeatedly."""
    try:
        os.makedirs(os.path.dirname(REMEMBER_DB_PATH), exist_ok=True)
        con = _connect()
        con.execute(
            """CREATE TABLE IF NOT EXISTS remember_tokens (
                selector TEXT PRIMARY KEY,
                validator_hash TEXT NOT NULL,
                username TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_remember_expires ON remember_tokens(expires_at)"
        )
        con.commit()
        con.close()
        try:
            os.chmod(REMEMBER_DB_PATH, 0o600)
        except OSError:
            pass
        return True
    except Exception as e:
        pcat_logger.logger.warning(f"init_remember_db failed: {e}")
        return False


def issue_token(username: str, ttl_days: int = _DEFAULT_TTL_DAYS):
    """Generate a new (selector, validator) pair, store hashed validator, return raw pair.

    The caller writes f"{selector}:{validator}" into the response cookie. The raw
    validator is never persisted, only its sha256 hash.
    """
    selector = secrets.token_hex(_SELECTOR_BYTES)
    validator = secrets.token_hex(_VALIDATOR_BYTES)
    now = int(time.time())
    expires_at = now + ttl_days * 86400
    try:
        con = _connect()
        # Opportunistic prune so the table doesn't grow unbounded.
        con.execute("DELETE FROM remember_tokens WHERE expires_at < ?", (now,))
        con.execute(
            "INSERT INTO remember_tokens(selector, validator_hash, username, expires_at, created_at) VALUES(?, ?, ?, ?, ?)",
            (selector, _hash_validator(validator), username, expires_at, now),
        )
        con.commit()
        con.close()
        return selector, validator, expires_at
    except Exception as e:
        pcat_logger.logger.warning(f"issue_token failed: {e}")
        return None, None, 0


def validate_token(selector: str, validator: str):
    """Return the username if (selector, validator) is valid and unexpired, else None.

    Uses constant-time comparison on the validator hash to defeat timing attacks.
    """
    if not selector or not validator:
        return None
    try:
        con = _connect()
        row = con.execute(
            "SELECT validator_hash, username, expires_at FROM remember_tokens WHERE selector = ?",
            (selector,),
        ).fetchone()
        con.close()
    except Exception as e:
        pcat_logger.logger.warning(f"validate_token query failed: {e}")
        return None
    if not row:
        return None
    stored_hash, username, expires_at = row
    if int(time.time()) >= int(expires_at):
        # Lazy cleanup of this expired row.
        revoke_token(selector)
        return None
    if not hmac.compare_digest(stored_hash, _hash_validator(validator)):
        return None
    return username


def revoke_token(selector: str):
    if not selector:
        return
    try:
        con = _connect()
        con.execute("DELETE FROM remember_tokens WHERE selector = ?", (selector,))
        con.commit()
        con.close()
    except Exception as e:
        pcat_logger.logger.warning(f"revoke_token failed: {e}")


def revoke_all_for_user(username: str):
    if not username:
        return
    try:
        con = _connect()
        con.execute("DELETE FROM remember_tokens WHERE username = ?", (username,))
        con.commit()
        con.close()
    except Exception as e:
        pcat_logger.logger.warning(f"revoke_all_for_user failed: {e}")


def parse_cookie(raw: str):
    """Split a "selector:validator" cookie value. Returns (selector, validator) or (None, None)."""
    if not raw or ":" not in raw:
        return None, None
    selector, _, validator = raw.partition(":")
    if not selector or not validator:
        return None, None
    return selector, validator
