"""Passwords, tokens and identifiers — the pieces authentication is built from.

No database here: these are the properties that hold before any row exists, and they are
the ones a mistake would quietly break (a hash that verifies anything, a token with no
entropy, an id that collides).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from store_everything import passwords, tokens
from store_everything.ids import new_id

# ------------------------------------------------------------------------ passwords


def test_a_hash_verifies_its_own_password_and_nothing_else() -> None:
    stored = passwords.hash_password("correct horse battery staple")

    assert passwords.verify_password(stored, "correct horse battery staple")
    assert not passwords.verify_password(stored, "correct horse battery stapl")
    assert not passwords.verify_password(stored, "")


def test_hashes_are_salted() -> None:
    """Two accounts with the same password must not share a hash."""
    first = passwords.hash_password("the same password twice")
    second = passwords.hash_password("the same password twice")

    assert first != second


def test_the_stored_hash_is_argon2id() -> None:
    assert passwords.hash_password("a-long-enough-password").startswith("$argon2id$")


def test_a_short_password_is_refused() -> None:
    with pytest.raises(passwords.WeakPasswordError):
        passwords.hash_password("short")


def test_an_absurdly_long_password_is_refused_rather_than_hashed() -> None:
    """The length cap is a denial-of-service guard: hashing is deliberately expensive."""
    with pytest.raises(passwords.WeakPasswordError):
        passwords.hash_password("x" * (passwords.MAX_LENGTH + 1))

    stored = passwords.hash_password("a-long-enough-password")
    assert not passwords.verify_password(stored, "x" * (passwords.MAX_LENGTH + 1))


def test_a_malformed_hash_never_verifies_and_never_raises() -> None:
    """A corrupted row must fail closed, not crash the login endpoint."""
    assert not passwords.verify_password("not-a-hash", "a-long-enough-password")
    assert not passwords.needs_rehash("not-a-hash")


def test_a_current_hash_does_not_need_rehashing() -> None:
    assert not passwords.needs_rehash(passwords.hash_password("a-long-enough-password"))


# -------------------------------------------------------------------------- tokens


def test_a_minted_token_carries_its_prefix_and_its_digest() -> None:
    minted = tokens.mint(tokens.ACCESS_TOKEN_PREFIX)

    assert minted.plaintext.startswith("sepat_")
    assert minted.digest == tokens.digest(minted.plaintext)
    # The digest is not the token: storing it cannot leak the credential.
    assert minted.plaintext not in minted.digest


def test_tokens_do_not_repeat() -> None:
    minted = {tokens.mint(tokens.SESSION_TOKEN_PREFIX).plaintext for _ in range(500)}

    assert len(minted) == 500


def test_a_token_carries_at_least_256_bits() -> None:
    """07 § tokens & credentials sets the floor; base64url is 6 bits per character."""
    body = tokens.mint(tokens.ACCESS_TOKEN_PREFIX).plaintext.removeprefix("sepat_")

    assert len(body) * 6 >= 256


@given(st.text(min_size=1, max_size=200))
def test_the_digest_is_stable_and_hex(value: str) -> None:
    digest = tokens.digest(value)

    assert digest == tokens.digest(value)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


# ------------------------------------------------------------------------------ ids


def test_ids_are_version_7_uuids() -> None:
    identifier = new_id()

    assert identifier.version == 7
    assert identifier.variant == "specified in RFC 4122"


def test_ids_are_unique() -> None:
    assert len({new_id() for _ in range(10_000)}) == 10_000


def test_ids_sort_in_creation_order() -> None:
    """Strictly increasing, including inside one millisecond — the tiebreak cursors rely on."""
    minted = [new_id() for _ in range(5_000)]

    assert minted == sorted(minted)
    # Lexicographic order agrees, which is what an index and a human reading a table see.
    assert [str(value) for value in minted] == sorted(str(value) for value in minted)


def test_ids_stay_unique_across_threads() -> None:
    """The counter is shared state; without its lock two threads could hand out one id."""
    from concurrent.futures import ThreadPoolExecutor

    def mint(_index: int) -> UUID:
        return new_id()

    with ThreadPoolExecutor(max_workers=8) as pool:
        minted = list(pool.map(mint, range(4_000)))

    assert len(set(minted)) == len(minted)


def test_an_id_carries_the_current_time() -> None:
    milliseconds = new_id().int >> 80
    stamped = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)

    assert abs((datetime.now(UTC) - stamped).total_seconds()) < 5
