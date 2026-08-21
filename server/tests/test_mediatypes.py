"""Media type detection and the class every file carries before any extractor runs.

The class is what type-scoped listings key off the moment a file lands (04 § identification),
so the mapping is a product decision rather than a property of the MIME tree — and it deserves
tests that say which side of each line a type falls on.
"""

from __future__ import annotations

import pytest

from store_everything import mediatypes


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("image/jpeg", "image/jpeg"),
        ("IMAGE/JPEG", "image/jpeg"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("  application/pdf  ", "application/pdf"),
    ],
)
def test_a_media_type_is_normalized(declared: str, expected: str) -> None:
    assert mediatypes.normalize(declared) == expected


@pytest.mark.parametrize("declared", [None, "", "nonsense", "image/", "/jpeg", "a/b/c"])
def test_a_non_media_type_is_none(declared: str | None) -> None:
    assert mediatypes.normalize(declared) is None


def test_the_extension_outranks_a_generic_declaration() -> None:
    """Browsers declare `application/octet-stream` freely; a name the user chose is better
    evidence than that."""
    assert mediatypes.detect("beach.jpg", "application/octet-stream") == "image/jpeg"


def test_a_declared_type_covers_an_extensionless_name() -> None:
    assert mediatypes.detect("IMG_0001", "image/heic") == "image/heic"


def test_unknowable_content_is_octet_stream() -> None:
    """`other` is an honest answer; guessing would put a wrong facet on a real file."""
    assert mediatypes.detect("mystery", None) == mediatypes.DEFAULT_MEDIA_TYPE
    assert mediatypes.media_class(mediatypes.DEFAULT_MEDIA_TYPE) == "other"


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("image/png", "image"),
        ("image/svg+xml", "image"),
        ("video/mp4", "video"),
        ("audio/flac", "audio"),
        ("text/plain", "document"),
        ("text/markdown", "document"),
        ("application/pdf", "document"),
        ("application/vnd.oasis.opendocument.text", "document"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "document"),
        ("application/zip", "archive"),
        ("application/x-7z-compressed", "archive"),
        ("application/octet-stream", "other"),
        ("application/x-sqlite3", "other"),
    ],
)
def test_the_media_class_mapping(media_type: str, expected: str) -> None:
    assert mediatypes.media_class(media_type) == expected


def test_every_class_is_one_the_schema_allows() -> None:
    """The column carries a check constraint over exactly this vocabulary."""
    from store_everything.tables import MEDIA_CLASSES

    assert set(mediatypes.MEDIA_CLASSES) == set(MEDIA_CLASSES)


def test_parameters_do_not_change_the_class() -> None:
    assert mediatypes.media_class("text/plain; charset=utf-8") == "document"
