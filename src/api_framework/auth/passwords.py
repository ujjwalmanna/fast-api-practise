"""Password hashing.

Argon2id, via `argon2-cffi`. It is OWASP's first-choice password hash and the
winner of the Password Hashing Competition; bcrypt remains acceptable but caps
the input at 72 bytes and has no memory-hardness parameter.

`passlib` is deliberately not used: it has had no release since 2020 and
mis-detects bcrypt >= 4.x.

The defaults from `argon2-cffi` follow the current RFC 9106 guidance. Tune only
with measurements - the cost must be high enough to hurt an offline attacker
and low enough that your login endpoint is not a self-inflicted DoS.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from api_framework.core.logging import get_logger

logger = get_logger(__name__)

_hasher = PasswordHasher()

# Argon2 processes the whole input, but an unbounded password is a cheap DoS:
# hashing a 10 MB string is expensive. Reject early.
MAX_PASSWORD_BYTES = 1024


def hash_password(plain_password: str) -> str:
    """Return an Argon2id hash. The salt is random and encoded in the output."""
    if len(plain_password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password exceeds {MAX_PASSWORD_BYTES} bytes")
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Constant-time-ish verification. Never raises on a wrong password.

    Returns False for a mismatch *and* for a corrupt stored hash, so a damaged
    row denies access rather than 500-ing.
    """
    try:
        return _hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, VerificationError):
        return False
    except InvalidHashError:
        logger.error("Stored password hash is malformed; denying authentication")
        return False


def needs_rehash(password_hash: str) -> bool:
    """True if the hash was made with weaker parameters than current defaults.

    Call after a successful login and transparently re-hash. This is how you
    raise cost parameters over time without forcing a password reset.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
