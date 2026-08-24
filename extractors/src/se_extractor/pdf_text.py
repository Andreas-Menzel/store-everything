"""`pdf-text`: a document's own text, page by page — and an honest answer when there is none.

A PDF is not one kind of file. Some carry a text layer that is exactly what the author typed;
some are photographs of paper with no text at all; and some carry a text layer that is *worse*
than nothing — a broken font encoding that yields `????????` for every word. Treating all three
the same is how a search index fills up with garbage that a person cannot find anything in.

So this is a decision tree, per page
([05 § built-in extractors](../../../specs/05-extractor-contract.md)):

1. **enough plausible text** → emit it as a segment anchored to that page;
2. **too little, or too much of it nonsense** → record the page as needing OCR;
3. and the document as a whole says `has_text_layer` and, when any page failed the check,
   `needs_ocr` with the list of pages.

Those last two are well-known metadata keys, and they are the whole mechanism behind routing to
OCR: `tesseract-ocr` declares `when: needs_ocr = true` and receives `ocr_pages` as parameters
(ADR-0020). Nothing in the core knows that PDFs and OCR are related.

**The original is never touched.** OCR text becomes segments; a text layer is *read*. That is
[F-004/FR-7](../../../features/F-004-document-text-extraction.md) and
[02 § invariant 2](../../../specs/02-domain-model.md#invariants), and it is why this extractor
opens the file read-only and writes nothing back.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
import unicodedata
from pathlib import Path
from types import FrameType
from typing import Any

import pymupdf

from se_extractor.client import ExtractorClient
from se_extractor.language import detect_language
from se_extractor.loop import JobContext, PermanentFailure, Worker
from se_extractor.models import Job

_logger = logging.getLogger("se_extractor.pdf_text")

EXTRACTOR_ID = "pdf-text"
VERSION = "1.0.0"

MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    "accepts": {"mime_types": ["application/pdf"]},
    "produces": ["text_segments", "metadata"],
    # Text is what makes a file *findable*, which is the slower and more important half of
    # ingestion (04 § prioritization) — medium rather than light.
    "cost_class": "medium",
    "gpu": "none",
    "network": "none",
}

#: Below this many characters, a page is not a page of text — it is a picture of one, or a
#: heading over an image. Chosen low on purpose: a title page with six words is still text, and
#: the cost of a false "needs OCR" is one wasted OCR pass, not a wrong answer.
MIN_CHARACTERS = 24

#: `U+FFFD` is what a decoder writes when it gave up. A page with more than a few is a page whose
#: font encoding is broken, and its "text" would poison a search index.
MAX_REPLACEMENT_RATIO = 0.05

#: How much of a page has to be characters a person could have typed. Below this the text layer
#: is mojibake — the classic broken-CMap PDF, where every glyph maps to a private-use codepoint.
MIN_PLAUSIBLE_RATIO = 0.75

#: Character categories a real document is made of: letters, digits, marks, punctuation,
#: separators and symbols. What is left — private use, unassigned, control — is what a broken
#: encoding produces.
_PLAUSIBLE_CATEGORIES = ("L", "N", "M", "P", "Z", "S")


def handle(job: Job, context: JobContext) -> dict[str, Any] | None:
    """Read every page, decide about every page, and say what was found."""
    original = job.original
    if original is None:
        return None

    with tempfile.TemporaryDirectory(prefix="pdf-text-") as scratch:
        source = Path(scratch) / "document.pdf"
        with source.open("wb") as sink, context.client.stream_input(job) as chunks:
            for chunk in chunks:
                context.raise_if_cancelled()
                sink.write(chunk)

        try:
            document = pymupdf.open(source)
        except Exception as broken:  # pymupdf raises its own hierarchy and plain exceptions
            # A file that cannot be opened will not open on a retry. Saying so permanently keeps
            # one corrupt document from occupying a queue slot four times (F-004/FR-6).
            raise PermanentFailure(f"this PDF cannot be opened: {broken}") from broken

        segments: list[dict[str, Any]] = []
        needs_ocr: list[int] = []
        for number in range(document.page_count):
            context.raise_if_cancelled()
            text = _page_text(document, number)
            if usable(text):
                segments.append(
                    {
                        "text": text,
                        "anchor": {"kind": "page", "page": number + 1},
                    }
                )
            else:
                needs_ocr.append(number + 1)
        pages = document.page_count
        document.close()

    language = detect_language(" ".join(segment["text"] for segment in segments))
    if language is not None:
        for segment in segments:
            segment["language"] = language

    facts: list[dict[str, Any]] = [
        # Two facts rather than one: "this document has text" is what a viewer asks, and "these
        # pages do not" is what the OCR extractor's routing predicate binds to.
        {"key": "has_text_layer", "type": "boolean", "value": bool(segments)},
        {"key": "needs_ocr", "type": "boolean", "value": bool(needs_ocr)},
    ]
    if needs_ocr:
        facts.append({"key": "ocr_pages", "type": "json", "value": needs_ocr})
    if language is not None:
        facts.append({"key": "language", "type": "string", "value": language})

    _logger.info(
        "job %s read %d of %d page(s); %d need OCR", job.id, len(segments), pages, len(needs_ocr)
    )
    return {"text_segments": segments, "metadata": facts}


def _page_text(document: Any, number: int) -> str:
    """One page's text, or nothing if that page refuses to be read.

    A single broken page is not a broken document: a scan with one corrupt object should still
    yield the other 299 pages, and the page that failed becomes a page that needs OCR.
    """
    try:
        return str(document[number].get_text()).strip()
    except Exception:  # one page's problem, not the document's
        _logger.warning("page %d could not be read", number + 1)
        return ""


def usable(text: str) -> bool:
    """Whether a page's text layer is worth indexing (F-004/FR-1, FR-2).

    Three ways it is not: there is too little of it, too much of it is the decoder's replacement
    character, or too little of it is characters a person could have typed. The last is the case
    that matters most in practice — a PDF with a broken font encoding has a *full* text layer of
    private-use codepoints, and a search index that swallowed it would answer queries with
    nonsense while looking perfectly healthy.
    """
    if len(text) < MIN_CHARACTERS:
        return False
    if text.count("�") / len(text) > MAX_REPLACEMENT_RATIO:
        return False
    plausible = sum(
        1
        for character in text
        if unicodedata.category(character).startswith(_PLAUSIBLE_CATEGORIES)
        or character in "\n\r\t"
    )
    return plausible / len(text) >= MIN_PLAUSIBLE_RATIO


def main() -> int:
    """Run `pdf-text` from the environment. The image's entrypoint."""
    logging.basicConfig(
        level=os.environ.get("SE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.environ.get("SE_EXTRACTOR_TOKEN", "")
    if not token:
        print("SE_EXTRACTOR_TOKEN is not set", file=sys.stderr)
        return 2

    client = ExtractorClient(os.environ.get("SE_CORE_URL", "http://api:8000"), token)
    worker = Worker(
        client,
        MANIFEST,
        handle,
        claim_wait=int(os.environ.get("SE_CLAIM_WAIT_SECONDS", "30") or 30),
        worker_name=os.environ.get("HOSTNAME"),
    )

    def stop(_signal: int, _frame: FrameType | None) -> None:
        _logger.info("stopping")
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        worker.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the image's entrypoint
    raise SystemExit(main())
