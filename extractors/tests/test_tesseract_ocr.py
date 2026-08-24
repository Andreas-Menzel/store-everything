"""`tesseract-ocr`: the parsing, the page selection, and one real recognition pass.

Two kinds of test, deliberately separated. Most of what can go wrong here is *arithmetic* —
grouping words into lines, dropping the rows Tesseract marks as unrecognised, deciding which pages
a job is about — and none of it needs the engine. Those run everywhere, from a checked-in TSV whose
expected output can be read off it by eye.

The rest needs the real thing, because the claim is about the real thing:
[F-004/FR-2](../../features/F-004-document-text-extraction.md) says a scanned page becomes a
segment with a page anchor, and [FR-8](../../features/F-004-document-text-extraction.md) says the
`searchable-pdf` rendition carries the OCR text over the original pages. A mocked Tesseract would
prove neither. Those tests skip when there is no `tesseract` on PATH, and
`tools/tesseract-in-docker.sh` is how a machine without one still runs them.
"""

# The same suppression as the other imaging tests: PDFium and libvips are looked up at call time,
# so a type checker cannot see their surface.
# pyright: reportCallIssue=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false
# pyright: reportOperatorIssue=false, reportArgumentType=false, reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pymupdf
import pypdfium2 as pdfium
import pytest

from se_extractor import tesseract_ocr as ocr
from se_extractor.loop import PermanentFailure
from se_extractor.models import Job
from se_extractor.preview_gen import bitmap_to_image

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"

#: What `three-pages.pdf` says, page by page. Stated in the corpus manifest's `asserts` for that
#: fixture, and repeated here because this is the test that reads the *pixels*: if somebody
#: changes the fixture's wording, this failing is the point.
PAGE_LINES = (
    "Store Everything fixture page one.",
    "The quick brown fox jumps over the lazy dog.",
    "This page mentions xylophone marmalade exactly once.",
)

needs_engine = pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="no tesseract on PATH (see tools/tesseract-in-docker.sh)",
)

#: Tesseract's own column order, with the rows it actually emits: the page, block, paragraph and
#: line boxes at levels 1 to 4, then one row per word at level 5. `conf` is -1 on everything above
#: word level, and on words it could not read at all.
_TSV = "\t".join(
    (
        "level",
        "page_num",
        "block_num",
        "par_num",
        "line_num",
        "word_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    )
)


def _tsv(path: Path, rows: list[tuple[Any, ...]]) -> Path:
    path.write_text(
        "\n".join([_TSV, *("\t".join(str(cell) for cell in row) for row in rows)]) + "\n",
        encoding="utf-8",
    )
    return path


def _job(**params: Any) -> Job:
    return Job.of(
        {
            "id": "01a02900-0000-7000-8000-00000000000a",
            "attempt": 1,
            "idempotency_key": "k",
            "extractor_id": ocr.EXTRACTOR_ID,
            "generation": 1,
            "params": params,
        }
    )


def test_words_become_lines_in_reading_order(tmp_path: Path) -> None:
    """One row per word, and a line is what shares a block, paragraph and line number."""
    tsv = _tsv(
        tmp_path / "page.tsv",
        [
            (1, 1, 0, 0, 0, 0, 0, 0, 100, 100, -1, ""),
            (5, 1, 1, 1, 1, 1, 10, 10, 40, 12, 96.0, "Invoice"),
            (5, 1, 1, 1, 1, 2, 60, 10, 30, 12, 94.0, "2024"),
            (5, 1, 1, 1, 2, 1, 10, 30, 50, 12, 90.0, "Total"),
            (5, 1, 1, 1, 2, 2, 70, 30, 40, 12, 88.0, "48,00"),
        ],
    )
    lines, confidence = ocr.words(tsv)

    assert lines == ["Invoice 2024", "Total 48,00"]
    assert confidence == pytest.approx(92.0)


def test_unreadable_words_do_not_become_text_or_confidence(tmp_path: Path) -> None:
    """A `conf` of -1 is Tesseract saying it did not read that box.

    Counting those as words would fabricate text; counting them as zero would drag a clean page's
    confidence down for boxes that hold nothing.
    """
    tsv = _tsv(
        tmp_path / "page.tsv",
        [
            (5, 1, 1, 1, 1, 1, 10, 10, 40, 12, 95.0, "Rechnung"),
            (5, 1, 1, 1, 1, 2, 60, 10, 10, 12, -1, ""),
            (5, 1, 1, 1, 1, 3, 80, 10, 10, 12, -1, "~"),
            (4, 1, 1, 1, 1, 0, 10, 10, 90, 12, -1, ""),
        ],
    )
    lines, confidence = ocr.words(tsv)

    assert lines == ["Rechnung"]
    assert confidence == pytest.approx(95.0)


def test_a_page_that_read_as_nothing_says_so(tmp_path: Path) -> None:
    """An empty TSV is a legitimate outcome — a photograph of a wall — not an error."""
    lines, confidence = ocr.words(_tsv(tmp_path / "page.tsv", []))

    assert lines == []
    assert confidence == 0.0


def test_the_pages_to_read_are_the_ones_routing_named() -> None:
    """`ocr_pages` arrives as `params.pages`, and it is a whitelist."""
    assert ocr.wanted_pages(_job(pages=[3, 1, 3]), total=5) == [1, 3]


def test_a_job_that_names_no_pages_reads_the_whole_document() -> None:
    """A run asked for directly — a re-run, or an operator's — has nothing to narrow it."""
    assert ocr.wanted_pages(_job(), total=3) == [1, 2, 3]


def test_pages_that_are_not_in_the_document_are_dropped_not_fatal() -> None:
    """A stale `ocr_pages` from a superseded version must not cost the document its real pages."""
    assert ocr.wanted_pages(_job(pages=[2, 99, "seven", True, None]), total=4) == [2]


def test_a_page_list_with_nothing_usable_in_it_falls_back_to_every_page() -> None:
    """Better to read a document twice than to record that it has no text when nobody looked."""
    assert ocr.wanted_pages(_job(pages=[0, 99]), total=2) == [1, 2]


def test_an_image_without_the_engine_fails_permanently(monkeypatch: Any) -> None:
    """A container missing Tesseract will not grow one on a retry.

    The distinction matters: a retryable failure here would put one job in front of the queue
    every few minutes forever, and the log would fill with the same line instead of the file
    carrying a sentence an operator can act on.
    """
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    job = _job()
    job = Job.of(
        {
            **{"id": job.id, "attempt": 1, "idempotency_key": "k", "generation": 1},
            "extractor_id": ocr.EXTRACTOR_ID,
            "inputs": [
                {
                    "index": 0,
                    "kind": "original",
                    "url": "/x",
                    "media_type": "application/pdf",
                    "size": 1,
                    "content_hash": "a" * 64,
                }
            ],
        }
    )

    with pytest.raises(PermanentFailure, match="no `tesseract`"):
        ocr.handle(job, None)  # the context is never reached: the check comes first


def scanned(source: Path, page: int) -> bytes:
    """A corpus page as a *scan*: the same page, rasterized, with no text layer left.

    Built rather than committed, and built from a fixture rather than from a font: the words are
    the corpus's (so the expected OCR output is exact), the pixels come from PDFium (so they are
    the same on every machine), and nothing here depends on which fonts a developer happens to
    have installed.
    """
    document = pdfium.PdfDocument(str(source))
    width, height = document[page - 1].get_size()
    # 200 dpi: enough for Tesseract on 14-point text, and a quarter of the pixels of 400.
    image = bitmap_to_image(document[page - 1].render(scale=200 / 72))
    out = pymupdf.open()
    out.new_page(width=width, height=height).insert_image(
        pymupdf.Rect(0, 0, width, height), stream=image.write_to_buffer(".jpg[Q=90]")
    )
    return bytes(out.tobytes(deflate=True))


@needs_engine
@pytest.mark.parametrize("page", [1, 3])
def test_a_scanned_corpus_page_comes_back_word_for_word(tmp_path: Path, page: int) -> None:
    """The exact line the corpus says is on that page — not "some text was found"."""
    source = tmp_path / "scan.pdf"
    source.write_bytes(scanned(CORPUS / "documents" / "three-pages.pdf", page))

    read = ocr.read_page(
        pdfium.PdfDocument(str(source)),
        1,
        room=tmp_path,
        binary=str(shutil.which("tesseract")),
    )

    assert read is not None
    assert read.text == PAGE_LINES[page - 1]
    # A clean 200-dpi render of a built-in font: anything below this would mean something is
    # wrong with the rendering, not with Tesseract.
    assert read.confidence > 80


@needs_engine
def test_the_searchable_pdf_keeps_every_page_and_makes_the_scanned_one_findable(
    tmp_path: Path,
) -> None:
    """FR-8, at the only boundary that matters: can a reader search the thing.

    So the assertion is not "a PDF came out" — it is that a PDF library reading the rendition
    finds the OCR text on the page that was scanned, and that the pages that were not scanned are
    still there and still say what they said.
    """
    original = tmp_path / "mixed.pdf"
    original.write_bytes(_two_page_scan_of(CORPUS / "documents" / "three-pages.pdf", tmp_path))
    document = pdfium.PdfDocument(str(original))

    read = ocr.read_page(document, 2, room=tmp_path, binary=str(shutil.which("tesseract")))
    assert read is not None
    rendition = ocr.searchable_pdf(document, [read], room=tmp_path)
    assert rendition is not None

    opened = pymupdf.open(stream=rendition)
    assert opened.page_count == 2
    # Page 1 was never OCR'd: it is the original page, with the text it always had.
    assert "fixture page one" in opened[0].get_text()
    # Page 2 was a picture of text and is now searchable.
    assert "xylophone marmalade" in opened[1].get_text()


def _two_page_scan_of(source: Path, room: Path) -> bytes:
    """Page 1 of the fixture as itself, page 3 as a scan — one document that needs both paths."""
    born = pymupdf.open(str(source))
    document = pymupdf.open()
    document.insert_pdf(born, from_page=0, to_page=0)
    document.insert_pdf(pymupdf.open(stream=scanned(source, 3)))
    return bytes(document.tobytes(deflate=True))
