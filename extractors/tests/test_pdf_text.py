"""`pdf-text`'s decision tree, against documents whose text is known exactly.

The corpus PDF has one line per page and this file knows all three, so "did the right text come
out under the right page anchor" has an exact answer
([F-004/FR-1](../../features/F-004-document-text-extraction.md)).

The other half is the part that protects search: a page with no text, and — the case that
actually bites — a page with a *full* text layer of nonsense from a broken font encoding. Both
have to come out as pages needing OCR (FR-2), because indexing the second one would make search
answer confidently with garbage.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import httpx
import pymupdf
import pytest

from se_extractor import ExtractorClient, Job, JobContext, PermanentFailure
from se_extractor.pdf_text import MANIFEST, handle, usable

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"
PDF = CORPUS / "documents" / "three-pages.pdf"
JOB_ID = "11111111-1111-1111-1111-111111111111"


def _job(payload: bytes) -> tuple[Job, JobContext]:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = ExtractorClient("http://core", "seext_probe", transport=httpx.MockTransport(answer))
    job = Job.of(
        {
            "id": JOB_ID,
            "attempt": 1,
            "idempotency_key": "extract:v:pdf-text:1.0.0:-:1",
            "extractor_id": "pdf-text",
            "generation": 1,
            "params": {},
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
    return job, JobContext(client=client, _cancelled=threading.Event())


def _facts(envelope: dict[str, Any]) -> dict[str, Any]:
    return {entry["key"]: entry["value"] for entry in envelope["metadata"]}


def test_every_page_comes_out_under_its_own_anchor() -> None:
    job, context = _job(PDF.read_bytes())

    envelope = handle(job, context)

    assert envelope is not None
    segments = envelope["text_segments"]
    assert [segment["anchor"]["page"] for segment in segments] == [1, 2, 3]
    assert segments[0]["text"] == "Store Everything fixture page one."
    assert "quick brown fox" in segments[1]["text"]
    # The phrase the corpus promises is on page three, and this is the assertion that makes a
    # page anchor mean something.
    assert "xylophone marmalade" in segments[2]["text"]
    assert all(segment["anchor"]["kind"] == "page" for segment in segments)

    facts = _facts(envelope)
    assert facts["has_text_layer"] is True
    assert facts["needs_ocr"] is False
    assert "ocr_pages" not in facts
    # English, detected once for the document and stamped on every segment (FR-4).
    assert facts["language"] == "en"
    assert all(segment["language"] == "en" for segment in segments)


def test_a_page_with_no_text_is_a_page_for_ocr(tmp_path: Path) -> None:
    """The routing signal `tesseract-ocr` binds to, from the only place that can produce it."""
    document = pymupdf.open()
    document.new_page()  # empty: a scan's page before OCR
    page = document.new_page()
    page.insert_text((72, 700), "This page has plenty of perfectly readable text on it.")
    scanned = tmp_path / "half-scanned.pdf"
    document.save(scanned)
    document.close()

    job, context = _job(scanned.read_bytes())
    envelope = handle(job, context)

    assert envelope is not None
    facts = _facts(envelope)
    assert facts["needs_ocr"] is True
    # Page one needs OCR; page two does not. Per page, because a document is rarely all one or
    # all the other.
    assert facts["ocr_pages"] == [1]
    assert facts["has_text_layer"] is True
    assert [segment["anchor"]["page"] for segment in envelope["text_segments"]] == [2]


def test_a_document_that_cannot_be_opened_fails_permanently() -> None:
    job, context = _job(b"%PDF-1.4\nthis is not a document\n%%EOF\n")

    with pytest.raises(PermanentFailure):
        handle(job, context)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Store Everything fixture page one.", True),
        ("Ein deutscher Satz mit genug Zeichen darin.", True),
        ("", False),
        ("Page 1", False),
        # A broken font encoding: a full text layer of private-use codepoints, which is the case
        # that would otherwise poison the index while looking healthy.
        ("\ue000" * 200, False),
        # The decoder gave up: replacement characters everywhere.
        ("\ufffd" * 40 + "some words here", False),
        # A little noise in real text is still real text.
        ("A perfectly ordinary sentence with one \ufffd in it somewhere.", True),
    ],
)
def test_the_garble_check_decides_what_is_worth_indexing(text: str, expected: bool) -> None:
    assert usable(text) is expected


def test_the_manifest_routes_pdfs_and_produces_text() -> None:
    assert MANIFEST["accepts"]["mime_types"] == ["application/pdf"]
    assert set(MANIFEST["produces"]) == {"text_segments", "metadata"}
    # No asset kinds: page images belong to `pdf-pages`, and claiming them here would take the
    # kind from the extractor that actually renders them (ADR-0020).
    assert "derived_asset_kinds" not in MANIFEST
