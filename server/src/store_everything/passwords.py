"""Password hashing and the password policy.

**argon2id** (07-identity-permissions-sharing.md § users): memory-hard, the current
consensus choice, and the library's defaults follow RFC 9106's recommended profile.

Policy is NIST-style deliberately: a length floor and nothing else. Composition rules
("one digit, one symbol") measurably produce weaker secrets — people satisfy them with
`Password1!` — and forced rotation produces `Password2!`.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12

#: Hashing cost is paid on every login attempt, so an unbounded password is a cheap way to
#: make the server do expensive work. The cap is far above any real passphrase.
MAX_LENGTH = 1024

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """The password does not meet the policy. The message is safe to show a user."""


def check_policy(password: str) -> None:
    """Raise `WeakPasswordError` if the password is unacceptable."""
    if len(password) < MIN_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_LENGTH} characters long.")
    if len(password) > MAX_LENGTH:
        raise WeakPasswordError(f"Password must be at most {MAX_LENGTH} characters long.")


def hash_password(password: str) -> str:
    """Hash a password after checking the policy."""
    check_policy(password)
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Verify a password. Never raises for an ordinary mismatch or a malformed hash."""
    if len(password) > MAX_LENGTH:
        # Refuse before hashing rather than after: this is the DoS guard, not a policy check.
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash predates the current cost parameters.

    Raising the cost later is then a code change plus one transparent re-hash at each
    user's next successful login, instead of a migration nobody can write.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return False
