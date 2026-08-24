"""`tesseract-ocr`: the text on pages that have none of their own.

This is the other half of `pdf-text`'s decision — and it never had to be told about it. `pdf-text`
writes `needs_ocr` and `ocr_pages` as ordinary metadata; this extractor's manifest says it accepts
PDFs *when* `needs_ocr` is true and asks for `ocr_pages` as a job parameter. The core matches the
predicate as results land and hands over a job. Neither extractor names the other, and a third one
could bind to the same keys tomorrow
([ADR-0020](../../../decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md)).

What it does per page: render at 300 dpi, run Tesseract **once**, and take two things from that
one recognition pass — the TSV (words, boxes, confidences) and Tesseract's own PDF (the page image
with an invisible text layer over it). The TSV becomes segments a search hit points into; the PDF
pages become the `searchable-pdf` rendition of
[F-004/FR-8](../../../features/F-004-document-text-extraction.md), assembled with the pages that
did *not* need OCR taken from the original. One pass, never two — OCR is the most expensive thing
this instance does, and doing it twice for two outputs would be the easiest waste to overlook.

**The original is never touched**
([02 § invariant 2](../../../specs/02-domain-model.md#invariants)). The searchable PDF is a
*rendition*: a second file, downloadable next to the original, which still answers
`GET /files/{id}/content` byte for byte.
"""

# The same suppression as the other imaging modules: libvips operations are looked up in the
# library at call time, so a type checker cannot see them.
# pyright: reportCallIssue=false, reportOptionalMemberAccess=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportOperatorIssue=false

from __future__ import annotations

import csv
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import pypdfium2 as pdfium

from se_extractor import pdfium_guard
from se_extractor.client import ExtractorClient
from se_extractor.language import detect_language
from se_extractor.loop import JobContext, PermanentFailure, Worker
from se_extractor.models import Job
from se_extractor.preview_gen import bitmap_to_image

_logger = logging.getLogger("se_extractor.tesseract_ocr")

EXTRACTOR_ID = "tesseract-ocr"
VERSION = "1.0.0"

RENDITION_KIND = "searchable-pdf"

#: The engine's own version travels with every row this writes: provenance is what makes a
#: reprocessing decision possible later (ADR-0004). Read from the binary at startup, with this as
#: the fallback when it cannot be read.
TESSERACT_VERSION = "5.5"

#: What Tesseract is given, and the trade behind it: `tessdata_fast` at 300 dpi is what the engine
#: is tuned for. Both languages this instance searches in (Q14), so a German scan is not read as
#: badly spelled English.
LANGUAGES = "eng+deu"
OCR_DPI = 300

#: A PDF point is 1/72 inch, so this is the render scale that puts a page at `OCR_DPI`.
_SCALE = OCR_DPI / 72

#: Tesseract's exit is not instant on a hostile page. Long enough that a dense A4 scan finishes on
#: a slow CPU, short enough that one page cannot hold a queue slot for an afternoon.
PAGE_TIMEOUT_SECONDS = 180

#: A page whose words average below this is noise — the "text" of a photograph of a wall. Recording
#: it would fill a search index with strings nobody will ever query, and hide the real hits.
MIN_MEAN_CONFIDENCE = 40.0

#: Below this a page has not been read: a stray glyph or two off a border. Same reasoning as
#: `pdf_text.MIN_CHARACTERS`, and deliberately the same number.
MIN_CHARACTERS = 24

MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    "model": {"name": "tesseract", "version": TESSERACT_VERSION},
    "accepts": {
        "mime_types": ["application/pdf"],
        # The predicate, and the parameter it brings with it. Both bound to well-known keys: this
        # extractor knows what `needs_ocr` means, not which extractor wrote it.
        "when": {"key": "needs_ocr", "equals": True},
        "params_from": {"ocr_pages": "pages"},
    },
    "produces": ["text_segments", "metadata", "derived_assets", "renditions"],
    "derived_asset_kinds": [RENDITION_KIND],
    "renditions": [
        {
            "kind": RENDITION_KIND,
            "format": "application/pdf",
            "label": "Searchable PDF",
        }
    ],
    # The most expensive thing on the instance: minutes per document rather than milliseconds
    # (04 § prioritization), which is why it is scheduled behind everything that is not.
    "cost_class": "heavy",
    "gpu": "none",
    "network": "none",
}


@dataclass(frozen=True, slots=True)
class Recognized:
    """One page, as Tesseract read it."""

    page: int
    text: str
    confidence: float
    #: Tesseract's rendering of this page — the image with an invisible text layer over it.
    searchable: bytes


def handle(job: Job, context: JobContext) -> dict[str, Any] | None:
    """OCR the pages this job names, or every page when it names none."""
    original = job.original
    if original is None:
        return None

    binary = shutil.which("tesseract")
    if binary is None:
        # An image without the engine will not grow one on a retry. Saying so permanently puts a
        # readable sentence on the file instead of a job that fails every few minutes forever.
        raise PermanentFailure("this container has no `tesseract` on its PATH")

    with tempfile.TemporaryDirectory(prefix="tesseract-ocr-") as scratch:
        room = Path(scratch)
        source = room / "document.pdf"
        with source.open("wb") as sink, context.client.stream_input(job) as chunks:
            for chunk in chunks:
                context.raise_if_cancelled()
                sink.write(chunk)

        try:
            with pdfium_guard.LOCK:
                document = pdfium.PdfDocument(str(source))
                total = len(document)
        except pdfium.PdfiumError as broken:
            raise PermanentFailure(f"this PDF cannot be read: {broken}") from broken

        wanted = wanted_pages(job, total)
        _logger.info("job %s will read %d of %d page(s)", job.id, len(wanted), total)

        read: list[Recognized] = []
        for page in wanted:
            context.raise_if_cancelled()
            recognized = read_page(document, page, room=room, binary=binary)
            if recognized is not None:
                read.append(recognized)

        rendition = searchable_pdf(document, read, room=room) if read else None
        with pdfium_guard.LOCK:
            document.close()

    if not read:
        # A scan of a blank page, or of something with no text on it. Not a failure — the file is
        # stored, and "there was nothing to read" is a true answer arrived at honestly.
        _logger.info("job %s found no readable text", job.id)
        return None

    joined = "\n".join(one.text for one in read)
    language = detect_language(joined)
    mean = sum(one.confidence for one in read) / len(read)

    segments = [
        {
            "text": one.text,
            "anchor": {"kind": "page", "page": one.page},
            # Per segment, because confidence is per page: a title page read cleanly and a
            # coffee-stained one read badly are different answers about different pages.
            "confidence": round(one.confidence / 100, 4),
            **({"language": language} if language else {}),
        }
        for one in read
    ]
    facts: list[dict[str, Any]] = [
        # FR-2's flag. The mean over the pages *this run* read, so a document whose scans differ
        # in quality reports what it actually is.
        {"key": "ocr_confidence", "type": "float", "value": round(mean / 100, 4)},
        {"key": "ocr_pages_read", "type": "json", "value": [one.page for one in read]},
    ]
    if language is not None:
        facts.append({"key": "language", "type": "string", "value": language})

    result: dict[str, Any] = {"text_segments": segments, "metadata": facts}
    if rendition is not None:
        result["derived_assets"] = [
            {
                "kind": RENDITION_KIND,
                "name": "searchable.pdf",
                "content_hash": context.client.stage_asset(job, rendition),
                "media_type": "application/pdf",
                "rendition_kind": RENDITION_KIND,
                "params": {"pages": total, "ocr_pages": [one.page for one in read]},
            }
        ]
    _logger.info("job %s read %d page(s) at %.0f%% mean confidence", job.id, len(read), mean)
    return result


def wanted_pages(job: Job, total: int) -> list[int]:
    """Which pages to read: the ones routing named, or all of them.

    `params.pages` is `ocr_pages` copied in by the core. A job without it is a document nobody
    said anything about — an OCR run asked for directly, or a re-run — and reading every page is
    the only defensible default. Out-of-range numbers are dropped rather than failing the job: a
    stale `ocr_pages` from a superseded version should not cost a document its readable pages.
    """
    asked = job.params.get("pages")
    if not isinstance(asked, list):
        return list(range(1, total + 1))

    numbers: list[int] = []
    for entry in asked:
        if isinstance(entry, bool) or not isinstance(entry, int):
            continue
        if 1 <= entry <= total and entry not in numbers:
            numbers.append(entry)
    return sorted(numbers) or list(range(1, total + 1))


def read_page(document: Any, page: int, *, room: Path, binary: str) -> Recognized | None:
    """Render one page and read it. `None` when there was nothing legible on it."""
    image = room / f"page-{page:04d}.png"
    try:
        with pdfium_guard.LOCK:
            bitmap = document[page - 1].render(scale=_SCALE)
            rendered = bitmap_to_image(bitmap)
        # PNG, not WebP: this is Tesseract's input, and lossless is the point — a compression
        # artefact on a serif is a misread character.
        rendered.write_to_file(str(image))
    except pdfium.PdfiumError as broken:
        _logger.warning("page %d could not be rendered (%s)", page, broken)
        return None

    base = room / f"page-{page:04d}"
    complaint = _run(
        [
            binary,
            str(image),
            str(base),
            "-l",
            LANGUAGES,
            "--dpi",
            str(OCR_DPI),
            # Two outputs, one recognition pass. This is the whole reason the rendition is free.
            "tsv",
            "pdf",
        ]
    )

    tsv = base.with_suffix(".tsv")
    if not tsv.exists():
        # Tesseract exits **zero** when it cannot write its output file — it prints
        # "could not create TSV output file" and returns success. So the outputs are what is
        # checked, not the status: trusting the exit code here would turn a misconfigured
        # container into pages that silently contain no text.
        raise PermanentFailure(
            f"tesseract wrote no TSV for page {page}: {complaint or 'no output'}"
        )

    lines, confidence = words(tsv)
    text = "\n".join(lines)
    if len(text) < MIN_CHARACTERS or confidence < MIN_MEAN_CONFIDENCE:
        _logger.info(
            "page %d read as %d character(s) at %.0f%% — not recorded", page, len(text), confidence
        )
        return None
    searchable = base.with_suffix(".pdf")
    return Recognized(
        page=page,
        text=text,
        confidence=confidence,
        # Absent only if the PDF writer failed where the TSV writer did not. The segments are the
        # requirement and the rendition is a convenience, so an empty one loses the convenience.
        searchable=searchable.read_bytes() if searchable.exists() else b"",
    )


def _run(command: list[str]) -> str:
    """Tesseract, with its failures turned into sentences a person can act on.

    Returns whatever it said on stderr — which is the only place it says anything about the
    failures it does not set an exit code for. The caller checks the files.
    """
    try:
        finished = subprocess.run(  # noqa: S603 - a fixed binary and paths we made
            command,
            capture_output=True,
            timeout=PAGE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as slow:
        # Retryable on purpose: a page that timed out under load may finish when the instance is
        # quiet, and the queue is what decides how many times to try.
        raise TimeoutError(f"tesseract did not finish within {PAGE_TIMEOUT_SECONDS}s") from slow
    said = finished.stderr.decode("utf-8", "replace").strip().splitlines()
    complaint = said[-1] if said else ""
    if finished.returncode != 0:
        raise PermanentFailure(f"tesseract failed: {complaint or 'no output'}")
    return complaint


def words(tsv: Path) -> tuple[list[str], float]:
    """The lines Tesseract found, and how sure it was on average.

    TSV rather than hOCR: the same recognition, one row per word, and no XML to parse. Level 5 is
    the word level; the rows above it are the page, block, paragraph and line boxes, which are
    interesting for layout analysis and not for this.
    """
    lines: dict[tuple[str, str, str], list[str]] = {}
    confidences: list[float] = []
    with tsv.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE):
            if row.get("level") != "5":
                continue
            word = (row.get("text") or "").strip()
            if not word:
                continue
            try:
                confidence = float(row.get("conf") or -1)
            except ValueError:
                continue
            if confidence < 0:
                continue
            key = (row.get("block_num", ""), row.get("par_num", ""), row.get("line_num", ""))
            lines.setdefault(key, []).append(word)
            confidences.append(confidence)

    if not confidences:
        return [], 0.0
    return [" ".join(words) for words in lines.values()], sum(confidences) / len(confidences)


def searchable_pdf(document: Any, read: list[Recognized], *, room: Path) -> bytes | None:
    """The original's pages, with the OCR'd ones replaced by Tesseract's searchable versions.

    Page order is the document's, not the OCR order: a rendition whose pages were shuffled would
    be worse than none. Pages that had their own text are imported from the original untouched —
    they are already searchable, and rasterizing them to make a uniform-looking file would throw
    away the vector text that made them good.
    """
    replacements: dict[int, Any] = {}
    try:
        with pdfium_guard.LOCK:
            composed = pdfium.PdfDocument.new()
            for one in read:
                if not one.searchable:
                    continue
                path = room / f"searchable-{one.page:04d}.pdf"
                path.write_bytes(one.searchable)
                replacements[one.page] = pdfium.PdfDocument(str(path))
            if not replacements:
                return None

            for page in range(1, len(document) + 1):
                source = replacements.get(page)
                if source is not None:
                    composed.import_pages(source, [0])
                else:
                    composed.import_pages(document, [page - 1])

            out = room / "searchable.pdf"
            composed.save(str(out))
        return out.read_bytes()
    except pdfium.PdfiumError as broken:
        # The segments are the requirement; the rendition is a convenience. Losing the second one
        # should not cost the first — so this is a warning, not a failure.
        _logger.warning("the searchable PDF could not be assembled (%s)", broken)
        return None


def _engine_version(binary: str | None) -> str:
    """What `tesseract --version` says, for the manifest's provenance."""
    if binary is None:
        return TESSERACT_VERSION
    try:
        reported = subprocess.run(  # noqa: S603 - a binary found on PATH by `shutil.which`
            [binary, "--version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return TESSERACT_VERSION
    first = reported.stdout.decode("utf-8", "replace").splitlines()
    if first and first[0].lower().startswith("tesseract"):
        return first[0].split()[-1]
    return TESSERACT_VERSION


def build_manifest() -> dict[str, Any]:
    """The manifest with the engine version this container actually has.

    A stamp that says 5.5 while the image ships 5.3 would make reprocessing decisions on a lie,
    and the version is the one thing about an engine a container can always ask.
    """
    manifest = {**MANIFEST}
    manifest["model"] = {"name": "tesseract", "version": _engine_version(shutil.which("tesseract"))}
    return manifest


def main() -> int:
    """Run `tesseract-ocr` from the environment. The image's entrypoint."""
    logging.basicConfig(
        level=os.environ.get("SE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    token = os.environ.get("SE_EXTRACTOR_TOKEN", "")
    if not token:
        print("SE_EXTRACTOR_TOKEN is not set", file=sys.stderr)
        return 2
    if shutil.which("tesseract") is None:
        # Fail at startup rather than one job at a time: a container that cannot do its work
        # should say so where an operator is already looking.
        print("no `tesseract` on PATH — is this the OCR image?", file=sys.stderr)
        return 2

    client = ExtractorClient(os.environ.get("SE_CORE_URL", "http://api:8000"), token)
    worker = Worker(
        client,
        build_manifest(),
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
