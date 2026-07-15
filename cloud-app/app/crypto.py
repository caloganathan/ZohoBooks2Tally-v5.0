"""
Symmetric encryption for secrets at rest (Zoho OAuth tokens, connector secrets).

Uses Fernet (AES-128-CBC + HMAC). The key comes from ``APP_ENCRYPTION_KEY``:
either a real Fernet key, or any passphrase (which is stretched to a valid key
via SHA-256). If unset, a deterministic development key is used so the app runs
out of the box -- production deployments MUST set APP_ENCRYPTION_KEY.

``decrypt`` tolerates values that were stored as plaintext before encryption was
introduced, so enabling this on an existing database is non-breaking.
"""
import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

_ENC_PREFIX = "enc::"  # marks values this module produced


def _key_from_passphrase(passphrase: str) -> bytes:
    digest = hashlib.sha256(passphrase.encode()).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = (settings.app_encryption_key or "").strip()
    if key:
        try:
            return Fernet(key)  # already a valid Fernet key
        except (ValueError, TypeError):
            return Fernet(_key_from_passphrase(key))
    # Development fallback -- deterministic, NOT for production.
    return Fernet(_key_from_passphrase("zohobooks2tally-insecure-dev-key"))


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a string. ``None``/empty pass through unchanged."""
    if not plaintext:
        return plaintext
    token = _fernet().encrypt(plaintext.encode()).decode()
    return _ENC_PREFIX + token


def decrypt(value: str | None) -> str | None:
    """
    Decrypt a value produced by :func:`encrypt`.

    Values without the encryption prefix are returned as-is (legacy plaintext),
    making the migration to encryption-at-rest backwards compatible.
    """
    if not value:
        return value
    if not value.startswith(_ENC_PREFIX):
        return value  # legacy plaintext
    token = value[len(_ENC_PREFIX):]
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        # Wrong key / corrupted data -- surface rather than silently returning junk.
        raise ValueError("Unable to decrypt stored secret (APP_ENCRYPTION_KEY mismatch?)")
