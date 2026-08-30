"""What `preview-gen` makes of a real image and a real PDF.

Against the corpus fixtures rather than pictures invented here
([11 § test infrastructure](../../specs/11-engineering-standards.md#test-infrastructure)): the
two-tone PNG is 800x400 with a red half and a blue half, and the PDF has three pages — so
"did the aspect survive", "is the placeholder actually the picture" and "how many pages" all have
exact answers instead of approximate ones.

The core is a mock transport. What is real here is the *rendering*: libvips and PDFium run for
real, because the failures worth catching in this file are a squashed aspect ratio, an upscaled
thumbnail and a placeholder that decodes to mush.
"""

# The same suppression as the module under test, for the same reason: libvips operations are
# looked up at call time, so every `thumbnail_image` here is invisible to a type checker.
# pyright: reportCallIssue=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false
# pyright: reportOperatorIssue=false, reportArgumentType=false, reportUnknownArgumentType=false

from __future__ import annotations

import base64
import struct
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import pyvips

from se_extractor import ExtractorClient, Job, JobContext, PermanentFailure
from se_extractor.preview_gen import (
    MANIFEST,
    PLACEHOLDER_COLUMNS,
    PLACEHOLDER_ROWS,
    THUMBNAIL_SIZES,
    handle,
)

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"
IMAGE = CORPUS / "images" / "two-tone.png"
PDF = CORPUS / "documents" / "three-pages.pdf"

JOB_ID = "11111111-1111-1111-1111-111111111111"


def _job(path: Path, media_type: str) -> tuple[Job, JobContext, dict[str, bytes]]:
    """A claimed job whose one input is a file on disk, and a core that accepts assets."""
    payload = path.read_bytes()
    staged: dict[str, bytes] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/jobs/{JOB_ID}/inputs/0"):
            return httpx.Response(200, content=payload)
        if "/assets/" in request.url.path:
            staged[request.url.path.rsplit("/", 1)[-1]] = request.content
            return httpx.Response(200, json={"content_hash": request.url.path.rsplit("/", 1)[-1]})
        return httpx.Response(404, json={"title": "not-found", "status": 404})

    client = ExtractorClient("http://core", "seext_probe", transport=httpx.MockTransport(answer))
    document: dict[str, Any] = {
        "id": JOB_ID,
        "attempt": 1,
        "idempotency_key": "extract:v:preview-gen:1.0.0:-:1",
        "extractor_id": "preview-gen",
        "generation": 1,
        "params": {},
        "lease_expires_at": "2026-08-24T12:00:00Z",
        "heartbeat_interval_seconds": 2,
        "cancel_requested": False,
        "file_version": {
            "id": "22222222-2222-2222-2222-222222222222",
            "content_hash": "a" * 64,
            "size": len(payload),
            "media_type": media_type,
            "media_class": "image" if media_type.startswith("image/") else "document",
            "is_current": True,
        },
        "inputs": [
            {
                "index": 0,
                "kind": "original",
                "url": f"/extractor-api/v1/jobs/{JOB_ID}/inputs/0",
                "media_type": media_type,
                "size": len(payload),
                "content_hash": "a" * 64,
            }
        ],
    }
    job = Job.of(document)
    return job, JobContext(client=client, _cancelled=threading.Event()), staged


def _produced(envelope: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [asset for asset in envelope["derived_assets"] if asset["kind"] == kind]


def _fact(envelope: dict[str, Any], key: str) -> Any:
    return next(entry["value"] for entry in envelope["metadata"] if entry["key"] == key)


def _decoded(staged: dict[str, bytes], name: str, envelope: dict[str, Any]) -> pyvips.Image:
    asset = next(one for one in envelope["derived_assets"] if one["name"] == name)
    return pyvips.Image.new_from_buffer(staged[asset["content_hash"]], "")


def test_an_image_gets_every_tier_with_its_aspect_kept() -> None:
    job, context, staged = _job(IMAGE, "image/png")

    envelope = handle(job, context)

    assert envelope is not None
    thumbnails = _produced(envelope, "thumbnail")
    assert [asset["params"]["size"] for asset in thumbnails] == list(THUMBNAIL_SIZES)
    # 800x400 is 2:1, and every tier says so: the longest edge is the tier, the short edge half.
    assert [(one["params"]["width"], one["params"]["height"]) for one in thumbnails] == [
        (800, 400),  # 1024 asked for, but nothing is upscaled
        (512, 256),
        (256, 128),
    ]
    assert _fact(envelope, "dimensions") == {"width": 800, "height": 400}

    # The bytes are real WebP of the stated size, not a promise about them.
    smallest = _decoded(staged, "thumb-256.webp", envelope)
    assert (smallest.width, smallest.height) == (256, 128)
    assert smallest.get("vips-loader") == "webpload_buffer"


def test_nothing_is_upscaled() -> None:
    """A small image keeps its size in every tier: every URL exists, no detail is invented."""
    job, context, staged = _job(IMAGE, "image/png")
    envelope = handle(job, context)
    assert envelope is not None

    largest = _decoded(staged, "thumb-1024.webp", envelope)
    assert (largest.width, largest.height) == (800, 400)
    # And no image preview: the source is already smaller than the largest tier, so a preview
    # would be a copy of the original under a different name.
    assert _produced(envelope, "image-preview") == []


def test_a_bigger_image_also_gets_a_preview(tmp_path: Path) -> None:
    wide = pyvips.Image.black(3000, 1500).add(90).cast("uchar").colourspace("srgb")
    source = tmp_path / "wide.png"
    wide.write_to_file(str(source))

    job, context, staged = _job(source, "image/png")
    envelope = handle(job, context)

    assert envelope is not None
    preview = _produced(envelope, "image-preview")
    assert [(one["params"]["width"], one["params"]["height"]) for one in preview] == [(2048, 1024)]
    assert _decoded(staged, "preview.webp", envelope).width == 2048


def test_the_placeholder_is_the_picture_in_a_few_dozen_bytes() -> None:
    job, context, _staged = _job(IMAGE, "image/png")

    envelope = handle(job, context)

    assert envelope is not None
    encoded = _fact(envelope, "placeholder_hash")
    # F-028/FR-5's bound, which is what makes it free to inline in every listing row.
    assert len(encoded) <= 64

    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    version, columns, rows, width, height = struct.unpack(">BBBHH", raw[:7])
    assert (version, columns, rows) == (1, PLACEHOLDER_COLUMNS, PLACEHOLDER_ROWS)
    # The dimensions are in the placeholder so a grid reserves the right space before any
    # thumbnail lands.
    assert (width, height) == (800, 400)

    cells = [tuple(raw[7 + index * 3 : 10 + index * 3]) for index in range(columns * rows)]
    left, right = cells[0], cells[columns - 1]
    assert left[0] > left[2], f"the left half is red, not {left}"
    assert right[2] > right[0], f"the right half is blue, not {right}"


def test_a_pdf_is_its_first_page() -> None:
    job, context, staged = _job(PDF, "application/pdf")

    envelope = handle(job, context)

    assert envelope is not None
    assert _fact(envelope, "page_count") == 3
    thumbnails = _produced(envelope, "thumbnail")
    assert [asset["params"]["size"] for asset in thumbnails] == list(THUMBNAIL_SIZES)

    # Letter portrait: the long edge is the tier, and the page is taller than it is wide.
    largest = _decoded(staged, "thumb-1024.webp", envelope)
    assert largest.height == 1024
    assert largest.width < largest.height
    # A rendered page is not an image preview: page images are their own asset with their own
    # policy, and they are not this extractor's work.
    assert _produced(envelope, "image-preview") == []


@pytest.mark.parametrize("fixture", ["truncated.png", "oversized-dimensions.png"])
def test_a_file_that_cannot_be_rendered_fails_permanently(fixture: str) -> None:
    """Both shapes of broken, because they fail at different moments.

    `truncated.png` stops inside its header, so opening it fails; `oversized-dimensions.png` has
    a perfectly good header claiming 100 000 x 100 000 pixels, so it opens and then fails on the
    way through. Either way the answer is *permanent* — the bytes will not improve on a retry,
    and a poison file must stop costing queue slots after the first attempt.
    """
    job, context, _staged = _job(CORPUS / "adversarial" / fixture, "image/png")

    with pytest.raises(PermanentFailure):
        handle(job, context)


def test_a_pdf_that_cannot_be_read_fails_permanently(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot really\n%%EOF\n")

    job, context, _staged = _job(broken, "application/pdf")

    with pytest.raises(PermanentFailure):
        handle(job, context)


def test_the_manifest_claims_only_what_it_produces() -> None:
    # The kinds here are single-provider claims (ADR-0020): claiming one this extractor does not
    # actually make would block the extractor that does.
    assert MANIFEST["derived_asset_kinds"] == ["thumbnail", "image-preview"]
    assert MANIFEST["accepts"]["mime_types"] == ["image/*", "application/pdf"]
    assert MANIFEST["network"] == "none"


def test_a_job_with_no_input_produces_nothing() -> None:
    """A job with nothing to read is not a failure — there is simply nothing to render."""
    job, context, _staged = _job(IMAGE, "image/png")
    empty = Job.of(
        {
            "id": job.id,
            "attempt": job.attempt,
            "idempotency_key": job.idempotency_key,
            "extractor_id": job.extractor_id,
            "generation": job.generation,
            "params": {},
            "lease_expires_at": "2026-08-24T12:00:00Z",
            "heartbeat_interval_seconds": 2,
            "cancel_requested": False,
            "file_version": {
                "id": job.file_version.id,
                "content_hash": job.file_version.content_hash,
                "size": job.file_version.size,
                "media_type": job.file_version.media_type,
                "media_class": job.file_version.media_class,
                "is_current": True,
            },
            "inputs": [],
        }
    )

    assert handle(empty, context) is None
