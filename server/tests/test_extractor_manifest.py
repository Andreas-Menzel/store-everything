"""The manifest as a value: what parses, what is refused, what survives the round trip.

No database — these are the rules that hold before anything is stored, and they are the ones an
extractor author meets first. The registry's behaviour around them is in
`test_extractor_registry.py`.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from store_everything.extractors import (
    Claim,
    ClaimType,
    CostClass,
    GpuMode,
    Manifest,
    NetworkMode,
    OutputKind,
    claims_of,
)
from store_everything.tables import (
    EXTRACTOR_CLAIM_TYPES,
    EXTRACTOR_COST_CLASSES,
    EXTRACTOR_GPU_MODES,
    EXTRACTOR_NETWORK_MODES,
    EXTRACTOR_OUTPUT_KINDS,
)


def valid(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": "tesseract-ocr",
        "version": "5.5.0",
        "api_version": "v1",
        "accepts": {"mime_types": ["image/*"]},
        "produces": ["text_segments"],
    }
    document.update(overrides)
    return document


@pytest.mark.parametrize(
    ("alias", "vocabulary"),
    [
        (OutputKind, EXTRACTOR_OUTPUT_KINDS),
        (CostClass, EXTRACTOR_COST_CLASSES),
        (GpuMode, EXTRACTOR_GPU_MODES),
        (NetworkMode, EXTRACTOR_NETWORK_MODES),
        (ClaimType, EXTRACTOR_CLAIM_TYPES),
    ],
)
def test_the_typed_and_the_stored_vocabulary_agree(alias: Any, vocabulary: tuple[str, ...]) -> None:
    """One vocabulary, spelled twice — once for the type checker, once for a check constraint.

    Drift between them is the nastiest kind: a value the types accept and the database refuses,
    which surfaces as a `500` on a manifest that looked fine.
    """
    assert get_args(alias.__value__) == vocabulary


def test_a_chaining_manifest_keeps_its_predicate() -> None:
    """`accepts.when` is how `tesseract-ocr` gets scanned PDF pages without either extractor
    knowing the other exists (04 § routing). Chunk-3 routing reads exactly this."""
    manifest = Manifest.model_validate(
        valid(
            accepts={
                "mime_types": ["image/*", "application/pdf"],
                "derived_kinds": ["keyframe"],
                "when": {"key": "needs_ocr", "equals": True},
                "params_from": {"ocr_pages": "pages"},
            }
        )
    )

    assert manifest.accepts.when is not None
    assert manifest.accepts.when.key == "needs_ocr"
    assert manifest.accepts.when.equals is True
    assert manifest.accepts.params_from == {"ocr_pages": "pages"}
    # And it survives the shape that gets stored, under its wire name.
    assert manifest.document()["accepts"]["when"] == {"key": "needs_ocr", "equals": True}


def test_the_stored_document_uses_wire_names() -> None:
    """`model` on the wire, spelled otherwise in Python because Pydantic reserves `model_`."""
    manifest = Manifest.model_validate(valid(model={"name": "tesseract", "version": "5.5"}))

    document = manifest.document()

    assert document["model"] == {"name": "tesseract", "version": "5.5"}
    assert "declared_model" not in document


def test_claims_cover_the_three_exclusive_namespaces() -> None:
    manifest = Manifest.model_validate(
        valid(
            produces=["renditions", "derived_assets", "embeddings"],
            renditions=[{"kind": "searchable-pdf", "format": "pdf", "label": "Searchable PDF"}],
            derived_asset_kinds=["ocr-overlay"],
            embedding_spaces=["text-v1"],
        )
    )

    assert claims_of(manifest) == (
        Claim("rendition", "searchable-pdf"),
        Claim("derived_asset", "ocr-overlay"),
        Claim("embedding_space", "text-v1"),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"produces": ["derived_assets"], "derived_asset_kinds": ["Not A Slug"]},
        {"produces": ["embeddings"], "embedding_spaces": ["text_v1"]},
        {"accepts": {"derived_kinds": ["Keyframe"]}},
    ],
)
def test_kinds_must_be_names_a_derived_store_path_can_hold(overrides: dict[str, Any]) -> None:
    """These names become directory entries and URL segments, so the shape is not cosmetic."""
    with pytest.raises(ValidationError):
        Manifest.model_validate(valid(**overrides))


def test_a_manifest_may_not_be_endlessly_long() -> None:
    """A bound on what one registration can assert, so a manifest cannot become a denial of
    service against routing."""
    with pytest.raises(ValidationError):
        Manifest.model_validate(
            valid(accepts={"mime_types": [f"image/x-{index}" for index in range(101)]})
        )
