"""The conformance kit's own rules — the half that needs no instance.

A kit that cannot go red is decoration
([11 § test layers](../../specs/11-engineering-standards.md)), and the manifest rules are where a
third-party author meets this contract first: they run before anything is pointed at a core, in
the author's own unit test. So each rule is checked against a manifest that breaks it, and against
the shipped manifests that must not break any of them.

`manifest_problems` is deliberately a *copy* of the core's rules. That is a real risk — two
implementations of one rule set drift — which is why the last test here holds the copy against the
manifests the core actually accepts every day.
"""

from __future__ import annotations

from typing import Any

import pytest

from se_extractor import basic_metadata, pdf_pages, pdf_text, preview_gen, reference, text_plain
from se_extractor import tesseract_ocr as ocr
from se_extractor.conformance import manifest_problems, matches_pattern


def _manifest(**overrides: Any) -> dict[str, Any]:
    """A coherent manifest, to break one field of at a time."""
    base: dict[str, Any] = {
        "id": "sample",
        "version": "1.0.0",
        "api_version": "v1",
        "accepts": {"mime_types": ["text/*"]},
        "produces": ["metadata"],
    }
    base.update(overrides)
    return base


def test_a_coherent_manifest_has_nothing_to_report() -> None:
    assert manifest_problems(_manifest()) == []


@pytest.mark.parametrize("field", ["id", "version", "api_version"])
def test_the_three_fields_an_extractor_must_name(field: str) -> None:
    """Identity and contract version. Without them the core cannot even file the registration."""
    assert manifest_problems(_manifest(**{field: ""})) == [f"`{field}` is missing"]


def test_an_extractor_that_produces_nothing_is_refused() -> None:
    assert manifest_problems(_manifest(produces=[])) == [
        "`produces` must name at least one output kind"
    ]


def test_an_output_kind_nobody_has_heard_of_is_named() -> None:
    """The typo case: `segments` instead of `text_segments` would silently produce nothing."""
    problems = manifest_problems(_manifest(produces=["metadata", "segments"]))

    assert problems == ["`produces` names unknown kinds: segments"]


def test_a_duplicate_output_kind_is_reported() -> None:
    assert manifest_problems(_manifest(produces=["metadata", "metadata"])) == [
        "`produces` contains a duplicate"
    ]


def test_an_extractor_that_accepts_nothing_would_never_run() -> None:
    assert manifest_problems(_manifest(accepts={})) == [
        "`accepts` names neither a media-type pattern nor a derived kind"
    ]


def test_a_declared_output_needs_the_names_that_go_with_it() -> None:
    """`produces: derived_assets` without kinds is a promise with no address on it."""
    assert manifest_problems(_manifest(produces=["derived_assets"])) == [
        "produces `derived_assets` but `derived_asset_kinds` names nothing"
    ]


def test_names_without_the_output_they_belong_to_are_reported() -> None:
    """And the other direction: rendition kinds nobody declared they produce."""
    problems = manifest_problems(
        _manifest(renditions=[{"kind": "pdf", "format": "application/pdf", "label": "PDF"}])
    )

    assert problems == ["`renditions` is declared but `produces` omits `renditions`"]


def test_a_model_without_a_version_is_refused() -> None:
    """A model stamp is provenance — half of one is worse than none, because it looks complete."""
    assert manifest_problems(_manifest(model={"name": "tesseract"})) == [
        "`model` needs both a name and a version — it is provenance"
    ]


@pytest.mark.parametrize(
    ("pattern", "media_type", "matches"),
    [
        ("*/*", "application/pdf", True),
        ("image/*", "image/png", True),
        ("image/*", "application/pdf", False),
        ("application/pdf", "application/pdf", True),
        ("application/pdf", "application/pdfx", False),
        ("  text/plain  ", "text/plain", True),
    ],
)
def test_a_pattern_matches_what_it_should(pattern: str, media_type: str, matches: bool) -> None:
    """How the kit decides what to hand an extractor — and `image/*` must not swallow a PDF."""
    assert matches_pattern(pattern, media_type) is matches


SHIPPED = (
    preview_gen.MANIFEST,
    pdf_pages.MANIFEST,
    basic_metadata.MANIFEST,
    pdf_text.MANIFEST,
    text_plain.MANIFEST,
    ocr.MANIFEST,
    reference.build_manifest("verify"),
)


@pytest.mark.parametrize("manifest", SHIPPED, ids=[str(one["id"]) for one in SHIPPED])
def test_every_manifest_this_repository_ships_is_coherent(manifest: dict[str, Any]) -> None:
    """The rules against the manifests the core accepts daily — which is what keeps the kit's
    copy of those rules honest."""
    assert manifest_problems(manifest) == []
