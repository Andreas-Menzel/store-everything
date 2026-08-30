"""`basic-metadata`: what a file says about itself.

The facts that make a library navigable *before* anything understands its content — when a photo
was taken, where, with what; what a document calls itself. They are cheap (a header read, no
pixels decoded), they are what a timeline and a map are built on, and they are worth collecting
from the first day a file lands rather than when the surfaces that use them arrive.

Every value goes out under a **well-known key** with a fixed type
([02 § MetadataEntry](../../../specs/02-domain-model.md#metadataentry)): `taken_at` is a datetime
whatever the camera wrote, `gps` is a latitude/longitude pair whatever the EXIF rationals looked
like. That is the whole point of the registry — a map filter binds to `gps`, not to "whatever
Canon calls it".

**Tooling, and what is still missing.** Phase 2 reads EXIF through libvips and document
information through PDFium, both already in this image and both header-only. `exiftool` and
`ffprobe` ([05 § built-in extractors](../../../specs/05-extractor-contract.md)) read far more —
XMP, IPTC, maker notes, container metadata — and arrive with the A/V work in phase 3, where the
formats that need them do too. Until then this extractor is honest about its range: it emits what
it can read and stays quiet about the rest.
"""

# The same suppression as the other imaging modules: libvips operations are looked up in the
# library at call time, so a type checker cannot see them.
# pyright: reportCallIssue=false, reportOptionalMemberAccess=false, reportAttributeAccessIssue=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportReturnType=false
# pyright: reportOperatorIssue=false, reportArgumentType=false, reportUnknownArgumentType=false

from __future__ import annotations

import logging
import os
import re
import signal
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Any

import pypdfium2 as pdfium
import pyvips

from se_extractor import pdfium_guard
from se_extractor.client import ExtractorClient
from se_extractor.loop import JobContext, Worker
from se_extractor.models import Job

_logger = logging.getLogger("se_extractor.basic_metadata")

EXTRACTOR_ID = "basic-metadata"
VERSION = "1.0.0"

MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    # Everything, because every file has *something* to say about itself, and a file this
    # extractor can read nothing from costs one header read to find out.
    "accepts": {"mime_types": ["*/*"]},
    "produces": ["metadata"],
    "cost_class": "light",
    "gpu": "none",
    "network": "none",
}

#: How libvips hands an EXIF value back: the value, then the library's own annotation. The
#: annotation always ends in `N components, N bytes)`, which is what this matches — a make like
#: `Nikon (Japan)` keeps its parentheses rather than being cut in half by a greedy strip.
_ANNOTATION = re.compile(r"\s*\([^()]*\d+ components,\s*\d+ bytes\)$")

#: EXIF writes time as `2024:03:17 09:41:02`, with no zone. Read as UTC and *said* to be UTC:
#: the alternative is guessing the server's zone, which is wrong in a different way every time
#: the file moves. A camera's own offset (EXIF 2.31 `OffsetTimeOriginal`) arrives with exiftool.
_EXIF_TIME = "%Y:%m:%d %H:%M:%S"

#: PDF writes `D:20240317094102Z` or `D:20240317094102+01'00'`.
_PDF_TIME = re.compile(r"^D:(?P<stamp>\d{14})(?P<zone>Z|[+-]\d{2}'?\d{2}'?)?")

#: Bounds for anything that ends up in a string column, so a file with a novel in its EXIF
#: cannot fill the table.
MAX_TEXT = 200


def handle(job: Job, context: JobContext) -> dict[str, Any] | None:
    """Read the file's own account of itself. Never fails a job over a missing tag."""
    original = job.original
    if original is None:
        return None

    with tempfile.TemporaryDirectory(prefix="basic-metadata-") as scratch:
        source = Path(scratch) / "source"
        with source.open("wb") as sink, context.client.stream_input(job) as chunks:
            for chunk in chunks:
                context.raise_if_cancelled()
                sink.write(chunk)

        if original.media_type == "application/pdf":
            facts = document_facts(source)
        elif original.media_type.startswith("image/"):
            facts = image_facts(source)
        else:
            # Not a failure: most files have nothing this extractor can read, and saying so with
            # an empty result is cheaper for everyone than a job that errors.
            facts = []

    _logger.info("job %s read %d fact(s)", job.id, len(facts))
    return {"metadata": facts} if facts else None


def image_facts(source: Path) -> list[dict[str, Any]]:
    """EXIF, as far as libvips can read it — and nothing invented where it cannot."""
    try:
        # No `access` hint: this only reads the header, and asking for pixels would decode a
        # 100-megapixel file to learn its camera model.
        image = pyvips.Image.new_from_file(str(source))
        fields = {name: image.get(name) for name in image.get_fields() if name.startswith("exif")}
    except pyvips.Error:
        # A file that is not an image, or not one libvips knows. The *thumbnail* extractor is
        # where that is worth reporting; here it simply means there is nothing to read.
        return []

    facts: list[dict[str, Any]] = []
    taken = _exif_time(_tag(fields, "exif-ifd2-DateTimeOriginal")) or _exif_time(
        _tag(fields, "exif-ifd0-DateTime")
    )
    if taken is not None:
        facts.append({"key": "taken_at", "type": "datetime", "value": taken.isoformat()})

    camera = " ".join(
        part for part in (_tag(fields, "exif-ifd0-Make"), _tag(fields, "exif-ifd0-Model")) if part
    ).strip()
    if camera:
        facts.append({"key": "camera", "type": "string", "value": camera[:MAX_TEXT]})

    position = _coordinates(fields)
    if position is not None:
        latitude, longitude = position
        facts.append({"key": "gps", "type": "geo", "value": {"lat": latitude, "lon": longitude}})

    iso = _integer(_tag(fields, "exif-ifd2-ISOSpeedRatings"))
    if iso is not None:
        facts.append({"key": "iso", "type": "integer", "value": iso})

    orientation = _integer(_tag(fields, "exif-ifd0-Orientation"))
    if orientation is not None and orientation != 1:
        # Only when the file claims a rotation. `1` means upright, which every encoder writes
        # whether or not anybody looked at the picture — recording it would put a fact on every
        # image in the library that says nothing.
        facts.append({"key": "orientation", "type": "integer", "value": orientation})

    return facts


def document_facts(source: Path) -> list[dict[str, Any]]:
    """A PDF's information dictionary: what it calls itself, and when it was written."""
    try:
        with pdfium_guard.LOCK:
            document = pdfium.PdfDocument(str(source))
            information = {
                key: str(value) for key, value in (document.get_metadata_dict() or {}).items()
            }
            # Closed on the thread that opened it, under the lock (see `pdfium_guard`).
            document.close()
    except pdfium.PdfiumError:
        return []

    facts: list[dict[str, Any]] = []
    title = (information.get("Title") or "").strip()
    if title:
        facts.append({"key": "title", "type": "string", "value": title[:MAX_TEXT]})
    author = (information.get("Author") or "").strip()
    if author:
        facts.append({"key": "author", "type": "string", "value": author[:MAX_TEXT]})

    written = _pdf_time(information.get("CreationDate")) or _pdf_time(information.get("ModDate"))
    if written is not None:
        # `document_date` rather than `taken_at`: a photograph is taken and a document is dated,
        # and a timeline that mixed the two would sort a scan by when it was scanned.
        facts.append({"key": "document_date", "type": "datetime", "value": written.isoformat()})
    return facts


def _tag(fields: dict[str, Any], name: str) -> str | None:
    """One EXIF field with libvips' annotation removed."""
    raw = fields.get(name)
    if not isinstance(raw, str):
        return None
    return _ANNOTATION.sub("", raw).strip() or None


def _exif_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _EXIF_TIME).replace(tzinfo=UTC)
    except ValueError:
        # Cameras write nonsense here more often than one would hope — `0000:00:00 00:00:00` is
        # a common "unset". A missing date is better than a wrong one.
        return None


def _pdf_time(value: str | None) -> datetime | None:
    if not value:
        return None
    matched = _PDF_TIME.match(value.strip())
    if matched is None:
        return None
    try:
        stamp = datetime.strptime(matched.group("stamp"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    zone = (matched.group("zone") or "Z").replace("'", "")
    if zone in {"Z", ""}:
        return stamp.replace(tzinfo=UTC)
    try:
        hours, minutes = int(zone[1:3]), int(zone[3:5] or 0)
    except ValueError:
        return stamp.replace(tzinfo=UTC)
    sign = -1 if zone[0] == "-" else 1
    # Kept as the offset the document declared rather than normalised to UTC: it is the only
    # hint about where the thing was written, and the database column carries the zone anyway.
    return stamp.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))


def _integer(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split()[0])
    except (ValueError, IndexError):
        return None


def _coordinates(fields: dict[str, Any]) -> tuple[float, float] | None:
    """The GPS block as two floats, or nothing.

    EXIF writes coordinates as three rationals — degrees, minutes, seconds — plus a hemisphere
    letter in a separate tag. Both halves are required: degrees without the letter is a position
    on the wrong side of the equator, which is worse than no position at all.
    """
    latitude = _degrees(_tag(fields, "exif-ifd3-GPSLatitude"))
    longitude = _degrees(_tag(fields, "exif-ifd3-GPSLongitude"))
    north = _tag(fields, "exif-ifd3-GPSLatitudeRef")
    east = _tag(fields, "exif-ifd3-GPSLongitudeRef")
    if latitude is None or longitude is None or not north or not east:
        return None

    if north.upper().startswith("S"):
        latitude = -latitude
    if east.upper().startswith("W"):
        longitude = -longitude
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return None
    return round(latitude, 7), round(longitude, 7)


def _degrees(value: str | None) -> float | None:
    """`48/1 8/1 15/1` → 48.1375."""
    if not value:
        return None
    total = 0.0
    for index, part in enumerate(value.split()[:3]):
        numerator, _, denominator = part.partition("/")
        try:
            step = float(numerator) / float(denominator or 1)
        except (ValueError, ZeroDivisionError):
            return None
        total += step / (60**index)
    return total


def main() -> int:
    """Run `basic-metadata` from the environment. The image's entrypoint."""
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
