"""The upload wire dialect: header parsing, version negotiation, published limits.

ADR-0017 notes that the public tus conformance tester is pinned at an ancient draft and cannot
validate us, so this file *is* the conformance check for everything that does not need a
database. The rules it pins down are the ones a client depends on byte for byte.
"""

from __future__ import annotations

import pytest

from store_everything import resumable


@pytest.mark.parametrize(("raw", "expected"), [("?1", True), ("?0", False)])
def test_structured_booleans_are_understood(raw: str, expected: bool) -> None:
    assert resumable.parse_boolean(raw) is expected


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " ?1 "])
def test_a_sloppy_true_is_still_accepted(raw: str) -> None:
    """Lenient in, strict out: a client that spells this wrong is easier to accept than to
    argue with, and nothing downstream sees the spelling."""
    assert resumable.parse_boolean(raw) is True


@pytest.mark.parametrize("raw", [None, "", "maybe", "?2"])
def test_an_unintelligible_boolean_is_absent_rather_than_false(raw: str | None) -> None:
    """The distinction carries meaning: absent means "not speaking the protocol", while `?0`
    means "expect more data". Collapsing them would turn a plain upload into a stalled one."""
    assert resumable.parse_boolean(raw) is None


def test_we_only_ever_emit_structured_form() -> None:
    assert (resumable.boolean(True), resumable.boolean(False)) == ("?1", "?0")


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("42", 42), ("00", 0)])
def test_integers_are_parsed(raw: str, expected: int) -> None:
    assert resumable.parse_integer(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "-1", "1.5", "1e3", "abc", "1 2"])
def test_a_non_integer_is_absent(raw: str | None) -> None:
    """Negative included: an offset below zero is not a small mistake, it is not an offset."""
    assert resumable.parse_integer(raw) is None


@pytest.mark.parametrize("version", [9, 8, 6])
def test_the_supported_interop_versions_are_recognised(version: int) -> None:
    dialect = resumable.dialect_for(str(version))

    assert dialect is not None
    assert dialect.interop_version == version


@pytest.mark.parametrize("raw", [None, "3", "5", "10", "99", "nine"])
def test_an_unknown_version_degrades_to_a_plain_upload(raw: str | None) -> None:
    """The draft's own fallback, and the reason a future iOS release cannot break uploads:
    no dialect means no upload resource, served as an ordinary request (ADR-0017)."""
    assert resumable.dialect_for(raw) is None


def test_every_dialect_is_listed_once() -> None:
    versions = [dialect.interop_version for dialect in resumable.DIALECTS]

    assert len(versions) == len(set(versions))
    # Newest first, so the table reads as a history rather than a set.
    assert versions == sorted(versions, reverse=True)


def test_the_limits_header_uses_the_drafts_parameter_names() -> None:
    limits = resumable.Limits(
        max_size=1_000_000, min_append_size=1024, max_append_size=8192, max_age_seconds=604800
    )

    assert limits.render() == (
        "max-size=1000000, min-append-size=1024, max-append-size=8192, max-age=604800"
    )


def test_an_unlimited_instance_omits_max_size() -> None:
    """Publishing a made-up ceiling would be worse than publishing none: a client would
    chunk to fit a number that means nothing."""
    limits = resumable.Limits(
        max_size=None, min_append_size=1024, max_append_size=8192, max_age_seconds=60
    )

    assert "max-size" not in limits.render()
    assert limits.render().startswith("min-append-size=")


def test_the_offset_mismatch_type_is_the_registered_one() -> None:
    """A protocol-aware client recognises the IANA URI, not ours (08 § errors)."""
    assert resumable.OFFSET_MISMATCH_TYPE.endswith("#mismatching-upload-offset")
    assert "iana.org" in resumable.OFFSET_MISMATCH_TYPE
