"""What `pdf-pages` makes of the corpus PDF.

The three-page fixture is the point: a page image is only checkable against a document whose
pages are known one by one, and this one's are ("Store Everything fixture page one." on page 1,
the pangram on 2, the distinctive phrase on 3).

Rendering runs for real — PDFium and libvips both — because the failures worth catching here are
a page rendered at the wrong size, the wrong page rendered, and a request for a page that does
not exist coming back as something a queue will retry forever.
"""

# The same suppression as the module under test: libvips operations are looked up at call time.
# pyright: reportCallIssue=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false
# pyright: reportOperatorIssue=false, reportArgumentType=false, reportUnknownArgumentType=false

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import pyvips

from se_extractor import ExtractorClient, Job, JobContext, PermanentFailure
from se_extractor.pdf_pages import MANIFEST, PAGE_MAX_EDGE, handle, page_name

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"
PDF = CORPUS / "documents" / "three-pages.pdf"

JOB_ID = "11111111-1111-1111-1111-111111111111"


def _job(params: dict[str, Any] | None = None) -> tuple[Job, JobContext, dict[str, bytes]]:
    payload = PDF.read_bytes()
    staged: dict[str, bytes] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/jobs/{JOB_ID}/inputs/0"):
            return httpx.Response(200, content=payload)
        if "/assets/" in request.url.path:
            digest = request.url.path.rsplit("/", 1)[-1]
            staged[digest] = request.content
            return httpx.Response(200, json={"content_hash": digest})
        return httpx.Response(404, json={"title": "not-found", "status": 404})

    client = ExtractorClient("http://core", "seext_probe", transport=httpx.MockTransport(answer))
    job = Job.of(
        {
            "id": JOB_ID,
            "attempt": 1,
            "idempotency_key": "extract:v:pdf-pages:1.0.0:-:1",
            "extractor_id": "pdf-pages",
            "generation": 1,
            "params": params or {},
            "lease_expires_at": "2026-08-24T12:00:00Z",
            "heartbeat_interval_seconds": 2,
            "cancel_requested": False,
            "file_version": {
                "id": "22222222-2222-2222-2222-222222222222",
                "content_hash": "a" * 64,
                "size": len(payload),
                "media_type": "application/pdf",
                "media_class": "document",
                "is_current": True,
            },
            "inputs": [
                {
                    "index": 0,
                    "kind": "original",
                    "url": f"/extractor-api/v1/jobs/{JOB_ID}/inputs/0",
                    "media_type": "application/pdf",
                    "size": len(payload),
                    "content_hash": "a" * 64,
                }
            ],
        }
    )
    return job, JobContext(client=client, _cancelled=threading.Event()), staged


def test_a_job_with_no_page_renders_the_first_one() -> None:
    """Routing's own job: the eager page that arrives with the thumbnail."""
    job, context, staged = _job()

    envelope = handle(job, context)

    assert envelope is not None
    asset = envelope["derived_assets"][0]
    assert asset["kind"] == "page"
    assert asset["name"] == page_name(1)
    assert asset["params"]["page"] == 1
    assert asset["params"]["pages"] == 3

    image = pyvips.Image.new_from_buffer(staged[asset["content_hash"]], "")
    # Letter portrait at the configured long edge, aspect kept.
    assert image.height == PAGE_MAX_EDGE
    assert image.width < image.height
    assert asset["params"]["width"] == image.width


def test_the_page_the_job_asks_for_is_the_page_it_renders() -> None:
    job, context, staged = _job({"page": 3})

    envelope = handle(job, context)

    assert envelope is not None
    asset = envelope["derived_assets"][0]
    assert asset["name"] == page_name(3)
    assert asset["params"]["page"] == 3
    # Page 3 of this fixture is visibly different from page 1: different text, so different bytes.
    other, other_context, other_staged = _job({"page": 1})
    first = handle(other, other_context)
    assert first is not None
    assert staged[asset["content_hash"]] != other_staged[first["derived_assets"][0]["content_hash"]]


@pytest.mark.parametrize("page", [0, 4, 99])
def test_a_page_that_does_not_exist_fails_permanently(page: int) -> None:
    """A three-page document has no page four, and a retry will not change that."""
    job, context, _staged = _job({"page": page})

    with pytest.raises(PermanentFailure):
        handle(job, context)


def test_a_page_parameter_that_is_not_a_number_fails_permanently() -> None:
    job, context, _staged = _job({"page": "last"})

    with pytest.raises(PermanentFailure):
        handle(job, context)


def test_the_manifest_claims_only_page_images() -> None:
    # `page` is a single-provider kind (ADR-0020): claiming `thumbnail` here would take it from
    # `preview-gen`, which is the extractor that actually makes them.
    assert MANIFEST["derived_asset_kinds"] == ["page"]
    assert MANIFEST["accepts"]["mime_types"] == ["application/pdf"]
    assert MANIFEST["produces"] == ["derived_assets"]
