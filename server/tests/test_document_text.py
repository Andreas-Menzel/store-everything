"""Document text, from an upload to the segments a search will point into.

The extractors themselves are tested in `extractors/tests` — every branch of the decision tree,
every encoding, against fixtures whose truth is exact. This file tests something they cannot: that
what PyMuPDF read arrives at `GET /files/{id}/segments` with its page still attached.

That distinction is why these are not mocked.
[F-004/FR-1](../../features/F-004-document-text-extraction.md) is a claim about an API response,
and a fake extractor emitting a hand-written envelope would verify the plumbing while leaving the
requirement untested — the text has to come out of the real tool, over a real socket, in the real
result shape. So this runs the same three-sided arrangement
as `test_reference_extractor.py`: an instance serving HTTP, a worker draining its queue, and the
official extractors as separate threads of control talking to both.

The corpus is what makes the assertions exact
([ADR-0015](../../decisions/ADR-0015-ground-truth-corpus.md)): `three-pages.pdf` has a known line
per page, `mixed-text.pdf` has one page with text and one
without, `known-phrases.txt` has a known line 5. Nothing here asserts "some text was found".
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pymupdf
import pypdfium2 as pdfium
import pytest
from se_extractor import pdf_text as pdf_text_extractor
from se_extractor import tesseract_ocr as ocr_extractor
from se_extractor import text_plain as text_plain_extractor
from se_extractor.preview_gen import bitmap_to_image

from store_everything.api import API_V1_PREFIX
from store_everything.config import Settings
from tests.live_helpers import (
    active_workspace,
    await_run,
    await_status,
    facts,
    provision,
    running,
    segments,
    signed_in,
    upload,
)
from tests.live_instance import live_instance

# Synchronous on purpose: the SDK is, and so is a container.
pytestmark = [pytest.mark.integration]

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"

needs_engine = pytest.mark.skipif(
    shutil.which("tesseract") is None,
    reason="no tesseract on PATH (see tools/tesseract-in-docker.sh)",
)


def scan_of(source: Path, page: int) -> bytes:
    """One page of a fixture, rasterized into a PDF with no text layer of its own.

    The same trick as `extractors/tests/test_tesseract_ocr.py`, for the same reason: the words are
    the corpus's, so what OCR should return is known exactly, and the pixels come from PDFium
    rather than from whatever fonts this machine happens to have.
    """
    document = pdfium.PdfDocument(str(source))
    width, height = document[page - 1].get_size()
    # PDFium's stubs say `scale` is an int. It is not — a dpi is not a whole multiple of 72.
    rendered = document[page - 1].render(scale=200 / 72)  # pyright: ignore[reportArgumentType]
    image = bitmap_to_image(rendered)
    out = pymupdf.open()
    out.new_page(width=width, height=height).insert_image(
        pymupdf.Rect(0, 0, width, height), stream=image.write_to_buffer(".jpg[Q=90]")
    )
    return bytes(out.tobytes(deflate=True))


def half_scanned_document() -> bytes:
    """Page 1 as itself, page 2 a scan of the fixture's page 3 — one document, both paths."""
    source = CORPUS / "documents" / "three-pages.pdf"
    born = pymupdf.open(str(source))
    document = pymupdf.open()
    document.insert_pdf(born, from_page=0, to_page=0)
    document.insert_pdf(pymupdf.open(stream=scan_of(source, 3)))
    return bytes(document.tobytes(deflate=True))


@pytest.mark.fr("F-004/FR-1")
@pytest.mark.fr("F-004/FR-7")
def test_a_born_digital_pdf_becomes_segments_that_know_their_page(
    identity_settings: Settings,
) -> None:
    """Page 3's phrase is findable *as page 3* — which is the whole point of an anchor."""
    document = (CORPUS / "documents" / "three-pages.pdf").read_bytes()
    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin)
        token = provision(admin, pdf_text_extractor.EXTRACTOR_ID)
        with running(
            instance.base_url, token, pdf_text_extractor.MANIFEST, pdf_text_extractor.handle
        ):
            created = upload(admin, workspace, "three-pages.pdf", document, "application/pdf")
            await_status(admin, str(created["id"]), "indexed")
            found = segments(admin, str(created["id"]))
            known = facts(admin, str(created["id"]))
            content = admin.get(f"{API_V1_PREFIX}/files/{created['id']}/content")

        assert [span["anchor_kind"] for span in found] == ["page", "page", "page"]
        assert [span["anchor"]["page"] for span in found] == [1, 2, 3]
        # The exact fixture lines, not "some text": the corpus states what each page says.
        assert "fixture page one" in found[0]["text"]
        assert "quick brown fox" in found[1]["text"]
        assert "xylophone marmalade" in found[2]["text"]

        assert known["has_text_layer"] is True
        # No page needed OCR, so nothing routes there — and `ocr_pages` is absent rather than
        # empty, because a key that says "these pages" should not exist when there are none.
        assert known["needs_ocr"] is False
        assert "ocr_pages" not in known

        # FR-7: reading a text layer is reading. The bytes are the bytes that were uploaded.
        assert content.content == document
        assert hashlib.sha256(content.content).hexdigest() == hashlib.sha256(document).hexdigest()


@pytest.mark.fr("F-004/FR-2")
def test_a_page_without_text_is_named_for_ocr_while_its_neighbour_is_extracted(
    identity_settings: Settings,
) -> None:
    """The per-page decision, at the boundary: one document, one segment, one page sent onward.

    This is the mechanism the OCR extractor's routing predicate binds to
    ([ADR-0020](../../decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md)) — so it has to
    be true of what the *API* reports, not only of what the extractor returned.
    """
    document = (CORPUS / "documents" / "mixed-text.pdf").read_bytes()
    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin)
        token = provision(admin, pdf_text_extractor.EXTRACTOR_ID)
        with running(
            instance.base_url, token, pdf_text_extractor.MANIFEST, pdf_text_extractor.handle
        ):
            created = upload(admin, workspace, "mixed-text.pdf", document, "application/pdf")
            await_status(admin, str(created["id"]), "indexed")
            found = segments(admin, str(created["id"]))
            known = facts(admin, str(created["id"]))

        assert [span["anchor"]["page"] for span in found] == [1]
        assert "carries its own text" in found[0]["text"]

        assert known["has_text_layer"] is True
        assert known["needs_ocr"] is True
        assert known["ocr_pages"] == [2]


@pytest.mark.fr("F-004/FR-3")
@pytest.mark.fr("F-004/FR-4")
def test_a_text_file_becomes_segments_that_know_their_lines(identity_settings: Settings) -> None:
    """Line anchors and a language, for the files there are most of."""
    body = (CORPUS / "text" / "known-phrases.txt").read_bytes()
    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin)
        token = provision(admin, text_plain_extractor.EXTRACTOR_ID)
        with running(
            instance.base_url, token, text_plain_extractor.MANIFEST, text_plain_extractor.handle
        ):
            created = upload(admin, workspace, "known-phrases.txt", body, "text/plain")
            await_status(admin, str(created["id"]), "indexed")
            found = segments(admin, str(created["id"]))
            known = facts(admin, str(created["id"]))

        # Five lines with no blank line between them: one segment covering all five, which is
        # the honest answer — there is no paragraph boundary to split on.
        assert [span["anchor_kind"] for span in found] == ["line"]
        assert found[0]["anchor"] == {"start_line": 1, "end_line": 5}
        assert "xylophone marmalade" in found[0]["text"]

        # FR-4: the language reaches the API as a typed fact *and* rides on the segment, which is
        # what a language-aware analyser will read it from.
        assert known["language"] == "en"
        assert found[0]["language"] == "en"
        assert known["line_count"] == 6  # five lines and the trailing newline's empty sixth


@pytest.mark.fr("F-004/FR-3")
def test_paragraphs_become_their_own_segments_with_their_own_lines(
    identity_settings: Settings,
) -> None:
    """A markdown file has boundaries, and a hit should point at the paragraph it is in."""
    body = (CORPUS / "text" / "sample.md").read_bytes()
    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin)
        token = provision(admin, text_plain_extractor.EXTRACTOR_ID)
        with running(
            instance.base_url, token, text_plain_extractor.MANIFEST, text_plain_extractor.handle
        ):
            created = upload(admin, workspace, "sample.md", body, "text/markdown")
            await_status(admin, str(created["id"]), "indexed")
            found = segments(admin, str(created["id"]))

        # The fixture's five blocks, in reading order, each anchored where it starts.
        assert [span["anchor"]["start_line"] for span in found] == [1, 3, 5, 7, 10]
        assert found[0]["text"] == "# Fixture heading"
        assert "xylophone marmalade" in found[-1]["text"]
        assert [span["ordinal"] for span in found] == sorted(span["ordinal"] for span in found)


@pytest.mark.fr("F-004/FR-6")
def test_a_file_that_is_not_text_fails_visibly_and_stays_stored(
    identity_settings: Settings,
) -> None:
    """FR-6's promise, end to end: the failure is a status, not a lost file.

    The fixture is the mislabeled one — plain bytes that are not text under a name that claims
    otherwise. Here it is uploaded *as* `text/plain` with a payload no encoding explains, which is
    the shape a real broken export has.
    """
    body = bytes(range(0, 32)) * 40
    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin)
        token = provision(admin, text_plain_extractor.EXTRACTOR_ID)
        with running(
            instance.base_url, token, text_plain_extractor.MANIFEST, text_plain_extractor.handle
        ):
            created = upload(admin, workspace, "broken-export.txt", body, "text/plain")
            failed = await_status(admin, str(created["id"]), "failed")
            content = admin.get(f"{API_V1_PREFIX}/files/{created['id']}/content")

        run = failed["runs"][0]
        assert run["extractor"] == text_plain_extractor.EXTRACTOR_ID
        assert run["state"] == "failed"
        assert "not text" in (run["error"] or "")

        # Stored, downloadable, and findable by name — a failed extraction is not a failed upload.
        assert content.status_code == 200
        assert content.content == body
        assert not segments(admin, str(created["id"]))


@needs_engine
@pytest.mark.fr("F-004/FR-2")
def test_a_scanned_page_reaches_ocr_because_another_extractor_said_so(
    identity_settings: Settings,
) -> None:
    """The chain, end to end, with nothing in the core that knows PDFs and OCR are related.

    `pdf-text` reads page 1, finds nothing on page 2 and writes `needs_ocr` with `ocr_pages`.
    `tesseract-ocr`'s manifest declares it accepts PDFs *when* that key is true and asks for those
    pages as a parameter. The core matches the predicate as the first result lands. So the second
    job existing at all is the assertion — and then that its segment knows which page it came from
    ([ADR-0020](../../decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md)).
    """
    document = half_scanned_document()
    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin)
        text_token = provision(admin, pdf_text_extractor.EXTRACTOR_ID)
        ocr_token = provision(admin, ocr_extractor.EXTRACTOR_ID)
        with (
            running(
                instance.base_url,
                text_token,
                pdf_text_extractor.MANIFEST,
                pdf_text_extractor.handle,
            ),
            running(
                instance.base_url,
                ocr_token,
                ocr_extractor.build_manifest(),
                ocr_extractor.handle,
            ),
        ):
            created = upload(admin, workspace, "half-scanned.pdf", document, "application/pdf")
            run = await_run(admin, str(created["id"]), ocr_extractor.EXTRACTOR_ID)
            found = segments(admin, str(created["id"]))
            known = facts(admin, str(created["id"]))

        assert run["state"] == "succeeded", run["error"]

        by_page = {span["anchor"]["page"]: span for span in found}
        assert sorted(by_page) == [1, 2]
        # Page 1 came from the text layer, page 2 from the pixels — and the segments say which.
        assert by_page[1]["extractor"] == pdf_text_extractor.EXTRACTOR_ID
        assert by_page[2]["extractor"] == ocr_extractor.EXTRACTOR_ID
        assert "fixture page one" in by_page[1]["text"]
        assert "xylophone marmalade" in by_page[2]["text"]

        # FR-2's confidence, on the segment and as the document-level flag. The text-layer page
        # has no confidence to report: reading a string out of a PDF is not a guess.
        assert by_page[1]["confidence"] is None
        assert by_page[2]["confidence"] is not None
        assert by_page[2]["confidence"] > 0.8
        assert known["ocr_confidence"] > 0.8
        assert known["ocr_pages_read"] == [2]


@needs_engine
@pytest.mark.fr("F-004/FR-8")
@pytest.mark.fr("F-004/FR-7")
def test_the_searchable_pdf_is_offered_as_a_rendition_and_the_original_is_untouched(
    identity_settings: Settings,
) -> None:
    """FR-8 and the invariant that makes it safe.

    The rendition is a *second file*: a reader can download the original's pages with the OCR text
    laid over them, and `GET /files/{id}/content` still answers with the bytes that were uploaded,
    byte for byte ([02 § invariant 2](../../specs/02-domain-model.md#invariants)). "Searchable" is
    checked the way a reader would experience it — a PDF library opening the download finds the
    phrase on the page that was scanned.
    """
    document = half_scanned_document()
    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin)
        text_token = provision(admin, pdf_text_extractor.EXTRACTOR_ID)
        ocr_token = provision(admin, ocr_extractor.EXTRACTOR_ID)
        with (
            running(
                instance.base_url,
                text_token,
                pdf_text_extractor.MANIFEST,
                pdf_text_extractor.handle,
            ),
            running(
                instance.base_url,
                ocr_token,
                ocr_extractor.build_manifest(),
                ocr_extractor.handle,
            ),
        ):
            created = upload(admin, workspace, "half-scanned.pdf", document, "application/pdf")
            await_run(admin, str(created["id"]), ocr_extractor.EXTRACTOR_ID)
            offered = admin.get(f"{API_V1_PREFIX}/files/{created['id']}/renditions")
            downloaded = admin.get(
                f"{API_V1_PREFIX}/files/{created['id']}/renditions/searchable-pdf"
            )
            original = admin.get(f"{API_V1_PREFIX}/files/{created['id']}/content")

        assert [one["kind"] for one in offered.json()] == ["searchable-pdf"]
        assert downloaded.status_code == 200
        assert "attachment" in downloaded.headers["content-disposition"]

        enriched = pymupdf.open(stream=downloaded.content)
        assert enriched.page_count == 2
        # Both pages searchable in the rendition: page 1 because it always was, page 2 because
        # the OCR text is now a layer over the picture of it.
        assert "fixture page one" in enriched[0].get_text()
        assert "xylophone marmalade" in enriched[1].get_text()

        assert original.content == document
        assert hashlib.sha256(original.content).hexdigest() == hashlib.sha256(document).hexdigest()
