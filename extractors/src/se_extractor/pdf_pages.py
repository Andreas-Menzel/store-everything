"""`pdf-pages`: a document's pages as images, one at a time.

Page images are what makes a PDF readable in the app without downloading it, and what a search
hit on page 40 needs in order to *show* page 40. The policy is the reason this is its own
extractor rather than part of `preview-gen`
([09 § previews](../../../specs/09-previews.md#previews)):

- **page 1 is eager** — it is what a document looks like in a list, and it arrives with the
  thumbnail;
- **every other page is rendered when somebody asks**, at interactive priority, and stored. A
  300-page document rendered up front is 299 images nobody looked at, times every document on
  the instance.

The core decides which page: an on-demand job carries `params.page`, and a job without one is the
eager first page. Nothing here knows about queues or caching — it renders what it was asked for
and hands the bytes back.
"""

# The same suppression as `preview_gen`, for the same reason: libvips operations are looked up in
# the library at call time, so a type checker cannot see them.
# pyright: reportCallIssue=false, reportOptionalMemberAccess=false, reportOptionalCall=false
# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportArgumentType=false, reportReturnType=false, reportOperatorIssue=false

from __future__ import annotations

import logging
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import FrameType
from typing import Any

import pypdfium2 as pdfium
import pyvips

from se_extractor.client import ExtractorClient
from se_extractor.loop import JobContext, PermanentFailure, Worker
from se_extractor.models import Job
from se_extractor.preview_gen import WEBP_QUALITY, bitmap_to_image

_logger = logging.getLogger("se_extractor.pdf_pages")

EXTRACTOR_ID = "pdf-pages"
VERSION = "1.0.0"

PAGE_KIND = "page"

#: The long edge of a rendered page. Enough to read body text on a laptop and to zoom a little;
#: beyond this the bytes grow faster than the legibility does (09 § previews names ~1600 px).
PAGE_MAX_EDGE = 1600

#: US Letter's long edge in points. The scale factor is derived from it so a page comes out near
#: `PAGE_MAX_EDGE` whatever its own size, and the thumbnail step below trims the rest.
LETTER_LONG_EDGE = 792

MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    "accepts": {"mime_types": ["application/pdf"]},
    "produces": ["derived_assets"],
    "derived_asset_kinds": [PAGE_KIND],
    "cost_class": "light",
    "gpu": "none",
    "network": "none",
}


def page_name(page: int) -> str:
    """`page-0003.webp` — zero-padded so a directory listing sorts the way a document reads."""
    return f"page-{page:04d}.webp"


def handle(job: Job, context: JobContext) -> dict[str, Any] | None:
    """Render the page this job is about."""
    original = job.original
    if original is None:
        _logger.info("job %s has no document to render", job.id)
        return None

    requested = _requested_page(job)

    with tempfile.TemporaryDirectory(prefix="pdf-pages-") as scratch:
        source = Path(scratch) / "document.pdf"
        with source.open("wb") as sink, context.client.stream_input(job) as chunks:
            for chunk in chunks:
                context.raise_if_cancelled()
                sink.write(chunk)

        try:
            document = pdfium.PdfDocument(str(source))
            pages = len(document)
        except pdfium.PdfiumError as broken:
            raise PermanentFailure(f"this PDF cannot be read: {broken}") from broken

        if requested < 1 or requested > pages:
            # Asking for page 12 of a four-page document is a mistake that will not become
            # right on a retry.
            raise PermanentFailure(f"page {requested} of a {pages}-page document")

        context.raise_if_cancelled()
        try:
            bitmap = document[requested - 1].render(scale=PAGE_MAX_EDGE / LETTER_LONG_EDGE)
            image = bitmap_to_image(bitmap).thumbnail_image(PAGE_MAX_EDGE, size="down")
            payload = image.write_to_buffer(f".webp[Q={WEBP_QUALITY}]")
        except (pdfium.PdfiumError, pyvips.Error) as broken:
            raise PermanentFailure(f"page {requested} could not be rendered: {broken}") from broken

    _logger.info("job %s rendered page %d of %d", job.id, requested, pages)
    return {
        "derived_assets": [
            {
                "kind": PAGE_KIND,
                "name": page_name(requested),
                "content_hash": context.client.stage_asset(job, payload),
                "media_type": "image/webp",
                # The page number is what a client asks by, and the dimensions are what it lays
                # out with before the bytes arrive.
                "params": {
                    "page": requested,
                    "width": image.width,
                    "height": image.height,
                    "pages": pages,
                },
            }
        ]
    }


def _requested_page(job: Job) -> int:
    """Which page: what the job asked for, or the first one.

    A job with no `page` parameter is routing's own — the eager first page that arrives with the
    thumbnail. Anything else came from somebody waiting for that page.
    """
    asked = job.params.get("page", 1)
    try:
        return int(asked)
    except (TypeError, ValueError) as invalid:
        raise PermanentFailure(f"`page` must be a number, not {asked!r}") from invalid


def main() -> int:
    """Run `pdf-pages` from the environment. The image's entrypoint."""
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
