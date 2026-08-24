"""`text-plain`: line anchors, encodings, and knowing when a file is not text.

The corpus text fixtures are the ground truth here — five known lines in one, a known markdown
structure in the other — so "which lines is this phrase on" has an exact answer
([F-004/FR-3](../../features/F-004-document-text-extraction.md)).

Encodings get their own tests because a file is bytes: something has to decide what "text" means,
and both failure modes are worse than an error. Silent mojibake fills a search index with
nonsense, and refusing every non-UTF-8 file loses the exports people actually have.
"""

from __future__ import annotations

import threading
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from se_extractor import ExtractorClient, Job, JobContext, PermanentFailure
from se_extractor.text_plain import MANIFEST, MAX_SEGMENT_CHARACTERS, decode, handle, split

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"
KNOWN = CORPUS / "text" / "known-phrases.txt"
MARKDOWN = CORPUS / "text" / "sample.md"
JOB_ID = "11111111-1111-1111-1111-111111111111"


def _job(payload: bytes, media_type: str = "text/plain") -> tuple[Job, JobContext]:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = ExtractorClient("http://core", "seext_probe", transport=httpx.MockTransport(answer))
    job = Job.of(
        {
            "id": JOB_ID,
            "attempt": 1,
            "idempotency_key": "extract:v:text-plain:1.0.0:-:1",
            "extractor_id": "text-plain",
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
                "media_class": "document",
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
    )
    return job, JobContext(client=client, _cancelled=threading.Event())


def _facts(envelope: dict[str, Any]) -> dict[str, Any]:
    return {entry["key"]: entry["value"] for entry in envelope["metadata"]}


def test_a_known_file_comes_out_with_the_lines_it_is_on() -> None:
    job, context = _job(KNOWN.read_bytes())

    envelope = handle(job, context)

    assert envelope is not None
    segments = envelope["text_segments"]
    # Five lines with no blank between them: one segment covering all of them, anchored to the
    # range rather than to "somewhere in this file".
    assert len(segments) == 1
    anchor = segments[0]["anchor"]
    assert anchor == {"kind": "line", "start_line": 1, "end_line": 5}
    assert "xylophone marmalade" in segments[0]["text"]

    facts = _facts(envelope)
    assert facts["encoding"] == "utf-8"
    assert facts["line_count"] == 6
    # The fixture is mostly English with one German line, and the whole file is judged once.
    assert facts["language"] in {"en", "de"}


def test_paragraphs_become_segments_with_their_own_ranges() -> None:
    text = MARKDOWN.read_text(encoding="utf-8")

    segments = split(text)

    assert [segment["anchor"]["start_line"] for segment in segments] == [1, 3, 5, 7, 10]
    assert segments[0]["text"] == "# Fixture heading"
    assert segments[3]["text"] == "- a list item\n- another list item"
    # Ranges do not overlap and move forward, which is what makes them usable as positions.
    ends = [segment["anchor"]["end_line"] for segment in segments]
    starts = [segment["anchor"]["start_line"] for segment in segments]
    assert all(start <= end for start, end in zip(starts, ends, strict=True))
    assert all(later > earlier for earlier, later in pairwise(starts))


def test_a_wall_of_text_is_split_by_length() -> None:
    """No blank lines to break on, so the bound does it — a segment is a snippet, not a book."""
    text = "\n".join(f"line {number} of a log file with no paragraphs" for number in range(400))

    segments = split(text)

    assert len(segments) > 1
    assert all(len(segment["text"]) <= MAX_SEGMENT_CHARACTERS + 200 for segment in segments)
    assert segments[0]["anchor"]["start_line"] == 1
    assert segments[-1]["anchor"]["end_line"] == 400


def test_an_encoding_that_is_not_utf_eight_is_still_read() -> None:
    body = "Grüße aus München, hier ist ein längerer Satz.".encode("cp1252")

    decoded, encoding = decode(body)

    assert "Grüße" in decoded or "München" in decoded
    assert encoding != "utf-8"


def test_binary_wearing_a_text_type_is_reported_rather_than_indexed() -> None:
    # A `.txt` that is really a PNG. Indexing its control characters would put noise in the
    # search index; reporting it puts a status on the file (FR-6).
    job, context = _job(
        CORPUS / "images" / "two-tone.png" and (CORPUS / "images" / "two-tone.png").read_bytes()
    )

    with pytest.raises(PermanentFailure):
        handle(job, context)


def test_an_empty_file_is_no_text_and_no_failure() -> None:
    job, context = _job(b"")

    envelope = handle(job, context)

    assert envelope is not None
    assert envelope["text_segments"] == []
    assert _facts(envelope)["encoding"] == "utf-8"


def test_the_manifest_accepts_the_text_families() -> None:
    assert "text/*" in MANIFEST["accepts"]["mime_types"]
    assert "application/json" in MANIFEST["accepts"]["mime_types"]
    assert set(MANIFEST["produces"]) == {"text_segments", "metadata"}
