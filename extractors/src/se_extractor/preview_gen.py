"""`preview-gen`: the thumbnails every grid needs, and the placeholder that fills the gap.

The first official extractor that does real work, and the reason it is first: a file browser with
holes in it feels broken, so thumbnails are eager for every file that has a visual source
([09 § thumbnails](../../../specs/09-previews.md#thumbnails)). What it produces:

- **three thumbnails** — WebP, aspect preserved, longest edge 256, 512 and 1024. A fixed set
  rather than free-form resizing, because that is what makes a thumbnail URL immutable and
  cacheable forever (F-028/FR-1);
- **a placeholder** — a few dozen bytes describing the image's colours, stored as the well-known
  `placeholder_hash` metadata key so a listing can render an aspect-correct blurred cell with no
  extra request at all (FR-5);
- **an image preview** — one larger WebP for the detail view, so looking at a photo does not mean
  downloading the original;
- **the facts a client needs to lay out before any pixel arrives**: the source's dimensions, and
  a PDF's page count.

Two renderers, deliberately. Images go through **libvips** (fast, streaming, and it handles the
formats a phone produces); a PDF's first page is rendered with **PDFium** — its own renderer
rather than waiting for the text extractor's queue, because a thumbnail is P1 work and text
extraction is not ([05 § built-in extractors](../../../specs/05-extractor-contract.md)).

What this does **not** do is upscale. A 100 px image's three tiers are all 100 px: every tier
exists so every URL works, and none of them invents detail the file does not have.
"""

# libvips operations reach Python through `__getattr__` — `thumbnail_image`, `bandjoin` and the
# rest are looked up in the library at call time, so a type checker cannot know their signatures
# and reports every one of them. Suppressed for this module rather than line by line, because the
# alternative is a comment on two thirds of the lines; the *shapes* those calls return are
# asserted in `tests/test_preview_gen.py` against real fixtures instead.
# pyright: reportCallIssue=false, reportOptionalMemberAccess=false, reportOptionalCall=false
# pyright: reportAttributeAccessIssue=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false, reportIndexIssue=false
# pyright: reportArgumentType=false, reportReturnType=false, reportOperatorIssue=false

from __future__ import annotations

import base64
import logging
import os
import signal
import struct
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

_logger = logging.getLogger("se_extractor.preview_gen")

EXTRACTOR_ID = "preview-gen"
VERSION = "1.0.0"

#: The fixed set (09 § thumbnails, Q42). Ordered so the largest is rendered from the source and
#: the smaller ones from it — one decode, three encodes.
THUMBNAIL_SIZES = (1024, 512, 256)

THUMBNAIL_KIND = "thumbnail"
PREVIEW_KIND = "image-preview"

#: The detail view's image. Big enough for a full-screen look on a dense display, small enough
#: that it is not the original in disguise.
PREVIEW_MAX_EDGE = 2048

#: WebP quality. 82 is the usual "cannot tell without pixel-peeping" point for photographs, and
#: the difference between 82 and 90 is a third more bytes for every thumbnail on the instance.
WEBP_QUALITY = 82

#: The placeholder's grid. Twelve cells is enough to see *where* the picture is dark and where it
#: is red; it fits in well under the 64 bytes F-028/FR-5 allows once encoded.
PLACEHOLDER_COLUMNS = 4
PLACEHOLDER_ROWS = 3
PLACEHOLDER_VERSION = 1

MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    "accepts": {"mime_types": ["image/*", "application/pdf"]},
    "produces": ["metadata", "derived_assets"],
    "derived_asset_kinds": [THUMBNAIL_KIND, PREVIEW_KIND],
    # Thumbnails are what a browsing person waits for, and they are cheap: light class, so they
    # run ahead of everything that only makes a file *findable* (04 § prioritization).
    "cost_class": "light",
    "gpu": "none",
    "network": "none",
}


def placeholder(image: pyvips.Image, *, width: int, height: int) -> str:
    """A few dozen bytes that look like the picture (F-028/FR-5).

    Its own format rather than ThumbHash, and small enough to state in a sentence: a version
    byte, the grid, the source's dimensions, then one RGB triple per cell of a 4x3 downscale.
    The dimensions are the *source's* and are passed in rather than read off the image, because
    the colours may come from an already-downscaled copy while the aspect a client has to reserve
    space for is the original's — a grid that reflows when the real thumbnail lands is worse than
    a grey box.

    Encoded base64url without padding: 43 bytes in, 58 characters out.
    """
    grid = image.thumbnail_image(
        PLACEHOLDER_COLUMNS, height=PLACEHOLDER_ROWS, size="force"
    ).colourspace("srgb")
    cells = grid.write_to_memory()
    bands = grid.bands
    payload = bytearray(
        struct.pack(
            ">BBBHH",
            PLACEHOLDER_VERSION,
            PLACEHOLDER_COLUMNS,
            PLACEHOLDER_ROWS,
            min(width, 0xFFFF),
            min(height, 0xFFFF),
        )
    )
    for index in range(PLACEHOLDER_COLUMNS * PLACEHOLDER_ROWS):
        offset = index * bands
        payload += bytes(cells[offset : offset + 3])
    return base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")


def handle(job: Job, context: JobContext) -> dict[str, Any] | None:
    """Render what this file needs. The whole extractor, in one function."""
    original = job.original
    if original is None:
        _logger.info("job %s has nothing to render", job.id)
        return None

    with tempfile.TemporaryDirectory(prefix="preview-gen-") as scratch:
        # To a file rather than into memory: libvips streams from disk, PDFium wants a path, and
        # a 200 megapixel TIFF should not have to fit in the container's memory limit twice.
        source = Path(scratch) / "source"
        _download(job, context, source)
        context.raise_if_cancelled()

        media_type = original.media_type
        if media_type == "application/pdf":
            image, facts = _from_pdf(source)
        else:
            image, facts = _from_image(source)

        try:
            return _render(job, context, image, facts)
        except pyvips.Error as broken:
            # A decode failure arrives *here* rather than at open for anything libvips reads
            # lazily: a truncated scanline, a PNG whose declared dimensions are absurd. Same
            # verdict as a file that would not open at all — the bytes will not improve on a
            # retry, and a poison file should stop costing queue slots after the first attempt.
            raise PermanentFailure(f"this file could not be rendered: {broken}") from broken


def _download(job: Job, context: JobContext, destination: Path) -> None:
    with destination.open("wb") as sink, context.client.stream_input(job) as chunks:
        for chunk in chunks:
            context.raise_if_cancelled()
            sink.write(chunk)


def _from_image(source: Path) -> tuple[pyvips.Image, list[dict[str, Any]]]:
    """Open an image, or say permanently that it is not one.

    A file that cannot be decoded will not decode on a retry either, so this is a permanent
    failure — a broken upload should stop costing queue slots after the first attempt.
    """
    try:
        # Sequential access so libvips streams instead of holding the whole raster: it is what
        # makes a 100-megapixel scan a bounded amount of memory rather than a gamble.
        image = pyvips.Image.new_from_file(str(source), access="sequential")
    except pyvips.Error as broken:
        raise PermanentFailure(f"this is not an image libvips can read: {broken}") from broken
    return image, [_dimensions(image.width, image.height)]


def _from_pdf(source: Path) -> tuple[pyvips.Image, list[dict[str, Any]]]:
    """Render page one, and count the pages.

    Page one because that is what a document looks like in a grid. The rest of the pages are a
    different asset with a different policy — rendered when somebody asks for them
    ([09 § previews](../../../specs/09-previews.md#previews)) — and not this extractor's work.
    """
    try:
        document = pdfium.PdfDocument(str(source))
        pages = len(document)
        if pages == 0:
            raise PermanentFailure("this PDF has no pages")
        bitmap = document[0].render(scale=_pdf_scale())
    except pdfium.PdfiumError as broken:
        raise PermanentFailure(f"this PDF cannot be rendered: {broken}") from broken

    image = bitmap_to_image(bitmap)
    return image, [_dimensions(image.width, image.height), _page_count(pages)]


def _pdf_scale() -> float:
    """Render at enough resolution for the largest tier: 1024 px over a 612 pt letter page."""
    return max(THUMBNAIL_SIZES) / 612


def bitmap_to_image(bitmap: Any) -> pyvips.Image:
    """A PDFium bitmap as a libvips image, in RGB.

    Two details PDFium's buffer forces: the rows may be padded to a stride wider than the image,
    and the channels arrive as BGR. Both are cheaper to fix here than to render around.
    """
    bands = bitmap.n_channels
    stride = bitmap.stride
    padded = pyvips.Image.new_from_memory(
        bytes(bitmap.buffer), stride // bands, bitmap.height, bands, "uchar"
    )
    image = padded.crop(0, 0, bitmap.width, bitmap.height)
    blue, green, red = image[0], image[1], image[2]
    return red.bandjoin([green, blue])


def _render(
    job: Job, context: JobContext, image: pyvips.Image, facts: list[dict[str, Any]]
) -> dict[str, Any]:
    """Encode the tiers and the preview, stage them, and describe what was made.

    **One pass over the source, then everything from memory.** A sequentially-opened image can be
    read exactly once, forward — that is what keeps a huge scan within a container's memory limit
    — so the largest thing anybody needs is materialised first and every smaller size is derived
    from *it*. Reaching back to the source for the second tier would fail, and it should: the
    alternative is decoding a 100-megapixel file four times.
    """
    assets: list[dict[str, Any]] = []
    is_pdf = job.original is not None and job.original.media_type == "application/pdf"
    original_edge = max(image.width, image.height)

    working = image.thumbnail_image(PREVIEW_MAX_EDGE, size="down").copy_memory()
    context.raise_if_cancelled()

    for size in THUMBNAIL_SIZES:
        context.raise_if_cancelled()
        # `size="down"` is the no-upscaling rule: a small image keeps its own dimensions in every
        # tier, so every URL exists and none of them pretends to detail that is not there.
        scaled = working.thumbnail_image(size, size="down")
        payload = scaled.write_to_buffer(f".webp[Q={WEBP_QUALITY}]")
        assets.append(
            {
                "kind": THUMBNAIL_KIND,
                "name": f"thumb-{size}.webp",
                "content_hash": context.client.stage_asset(job, payload),
                "media_type": "image/webp",
                # What distinguishes this asset from its siblings: the tier it answers for, and
                # what the client will actually paint.
                "params": {"size": size, "width": scaled.width, "height": scaled.height},
            }
        )

    # A preview only where it buys something: an image bigger than the largest tier. A PDF's
    # pages are their own asset with their own policy, and a small image's "preview" would be a
    # copy of the original under a different name.
    if not is_pdf and original_edge > max(THUMBNAIL_SIZES):
        context.raise_if_cancelled()
        assets.append(
            {
                "kind": PREVIEW_KIND,
                "name": "preview.webp",
                "content_hash": context.client.stage_asset(
                    job, working.write_to_buffer(f".webp[Q={WEBP_QUALITY}]")
                ),
                "media_type": "image/webp",
                "params": {"width": working.width, "height": working.height},
            }
        )

    facts.append(
        {
            "key": "placeholder_hash",
            "type": "string",
            "value": placeholder(working, width=image.width, height=image.height),
        }
    )
    _logger.info("job %s produced %d asset(s)", job.id, len(assets))
    return {"metadata": facts, "derived_assets": assets}


def _dimensions(width: int, height: int) -> dict[str, Any]:
    """The well-known `dimensions` key: one fact with two numbers, not two keys."""
    return {"key": "dimensions", "type": "json", "value": {"width": width, "height": height}}


def _page_count(pages: int) -> dict[str, Any]:
    return {"key": "page_count", "type": "integer", "value": pages}


def main() -> int:
    """Run `preview-gen` from the environment. The image's entrypoint."""
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
