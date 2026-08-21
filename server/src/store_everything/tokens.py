"""Minting and hashing of bearer credentials — personal access tokens and session tokens.

Rules from 07-identity-permissions-sharing.md § tokens & credentials:

- **high entropy** (256 bit) — these are the credential, so guessing must be hopeless;
- **prefixed**, so a leaked token is recognizable to secret scanners (ours included:
  the repository's gitleaks rules can match `sepat_`);
- **hashed at rest** with SHA-256 — a database dump must not be a set of live credentials.

Why plain SHA-256 rather than argon2, which `passwords.py` uses: a password is
low-entropy and needs a slow hash to survive an offline dictionary attack. A 256-bit
random token has no dictionary, and lookup happens on every authenticated request — a
memory-hard hash there would be self-inflicted load with nothing to show for it. The
lookup is an indexed equality on the digest, so no comparison over the secret takes place
in the application at all.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

#: Recognizable to secret scanners. `se` for the product, then the credential kind.
ACCESS_TOKEN_PREFIX = "sepat_"  # noqa: S105 - a prefix, not a secret
SESSION_TOKEN_PREFIX = "sesess_"  # noqa: S105 - a prefix, not a secret

#: 32 bytes = 256 bit, per the spec's floor.
_TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A fresh credential: the plaintext is shown to its owner exactly once."""

    plaintext: str
    digest: str


def mint(prefix: str) -> MintedToken:
    return _minted(f"{prefix}{secrets.token_urlsafe(_TOKEN_BYTES)}")


def digest(plaintext: str) -> str:
    """The stored form of a token."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _minted(plaintext: str) -> MintedToken:
    return MintedToken(plaintext=plaintext, digest=digest(plaintext))
