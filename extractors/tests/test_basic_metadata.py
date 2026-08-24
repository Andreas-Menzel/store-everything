"""What `basic-metadata` reads, and what it refuses to invent.

The inputs here are built rather than curated, because the point is the *parsing*: a camera writes
`48/1 8/1 15/1` and a hemisphere letter in a separate tag, a PDF writes `D:20240317094102Z`, and
both have to come out as the well-known keys a timeline and a map bind to
([02 § MetadataEntry](../../specs/02-domain-model.md#metadataentry)).

The negative cases matter as much: a camera that wrote `0000:00:00` for the date, a position with
degrees but no hemisphere, a file with no tags at all. All of them mean *no fact*, because a wrong
date on a photo is worse than no date — it moves the photo in a timeline.
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
import pyvips

from se_extractor import ExtractorClient, Job, JobContext
from se_extractor.basic_metadata import MANIFEST, document_facts, handle, image_facts

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"
JOB_ID = "11111111-1111-1111-1111-111111111111"

TAGS = {
    "exif-ifd0-Make": "Fixture",
    "exif-ifd0-Model": "Camera One",
    "exif-ifd2-DateTimeOriginal": "2024:03:17 09:41:02",
    "exif-ifd2-ISOSpeedRatings": "400",
    "exif-ifd3-GPSLatitude": "48/1 8/1 15/1",
    "exif-ifd3-GPSLatitudeRef": "N",
    "exif-ifd3-GPSLongitude": "11/1 34/1 30/1",
    "exif-ifd3-GPSLongitudeRef": "E",
}


def tagged(path: Path, **overrides: str | None) -> Path:
    """A small JPEG carrying the EXIF this test cares about."""
    image = pyvips.Image.black(60, 40).add(200).cast("uchar").colourspace("srgb").copy()
    for name, value in {**TAGS, **overrides}.items():
        if value is None:
            continue
        image.set_type(pyvips.GValue.gstr_type, name, value)
    image.write_to_file(str(path))
    return path


def _facts(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {entry["key"]: entry["value"] for entry in entries}


def _types(entries: list[dict[str, Any]]) -> dict[str, str]:
    return {entry["key"]: entry["type"] for entry in entries}


def test_a_photograph_says_when_where_and_with_what(tmp_path: Path) -> None:
    facts = image_facts(tagged(tmp_path / "photo.jpg"))
    read = _facts(facts)

    assert read["taken_at"] == "2024-03-17T09:41:02+00:00"
    assert read["camera"] == "Fixture Camera One"
    # Degrees, minutes and seconds, folded into the pair a map filter uses.
    assert read["gps"] == {"lat": 48.1375, "lon": 11.575}
    assert read["iso"] == 400
    # Every value under its well-known key, at its registered type — that is what makes a range
    # query over `taken_at` and a bounding box over `gps` possible at all.
    assert _types(facts)["taken_at"] == "datetime"
    assert _types(facts)["gps"] == "geo"
    assert _types(facts)["iso"] == "integer"


def test_the_southern_and_western_hemispheres_are_negative(tmp_path: Path) -> None:
    facts = image_facts(
        tagged(
            tmp_path / "south.jpg",
            **{"exif-ifd3-GPSLatitudeRef": "S", "exif-ifd3-GPSLongitudeRef": "W"},
        )
    )

    assert _facts(facts)["gps"] == {"lat": -48.1375, "lon": -11.575}


def test_a_position_without_a_hemisphere_is_not_a_position(tmp_path: Path) -> None:
    """Degrees with no letter is a point on the wrong side of the equator half the time."""
    facts = image_facts(
        tagged(
            tmp_path / "unsigned.jpg",
            **{"exif-ifd3-GPSLatitudeRef": None, "exif-ifd3-GPSLongitudeRef": None},
        )
    )

    assert "gps" not in _facts(facts)


def test_a_date_a_camera_never_set_is_no_date(tmp_path: Path) -> None:
    """`0000:00:00 00:00:00` is the common "unset", and it is not a date."""
    facts = image_facts(
        tagged(tmp_path / "unset.jpg", **{"exif-ifd2-DateTimeOriginal": "0000:00:00 00:00:00"})
    )

    assert "taken_at" not in _facts(facts)
    # The rest of the block is still read: one bad tag does not discard the others.
    assert _facts(facts)["camera"] == "Fixture Camera One"


def test_an_image_with_no_tags_says_nothing(tmp_path: Path) -> None:
    plain = tmp_path / "plain.png"
    pyvips.Image.black(10, 10).write_to_file(str(plain))

    assert image_facts(plain) == []


def test_something_that_is_not_an_image_says_nothing() -> None:
    # Not an error either: reporting a broken image is the thumbnail extractor's job, and this
    # one has simply nothing to read.
    assert image_facts(CORPUS / "adversarial" / "truncated.png") == []
    assert document_facts(CORPUS / "adversarial" / "zero-byte.bin") == []


def test_a_document_says_what_it_calls_itself(tmp_path: Path) -> None:
    """The corpus PDF has no information dictionary, so one is written here."""
    source = tmp_path / "titled.pdf"
    original = (CORPUS / "documents" / "three-pages.pdf").read_bytes()
    # Add an Info dictionary to the trailer the fixture already has.
    patched = original.replace(
        b"trailer\n<< /Size",
        b"90 0 obj\n<< /Title (Quarterly report) /Author (A Person) "
        b"/CreationDate (D:20240317094102Z) >>\nendobj\n"
        b"trailer\n<< /Info 90 0 R /Size",
    )
    source.write_bytes(patched)

    read = _facts(document_facts(source))

    assert read["title"] == "Quarterly report"
    assert read["author"] == "A Person"
    # `document_date`, not `taken_at`: a scan is dated, not taken, and a timeline that mixed the
    # two would sort a document by when somebody scanned it.
    assert read["document_date"].startswith("2024-03-17T09:41:02")


def test_a_job_with_nothing_to_read_produces_no_envelope(tmp_path: Path) -> None:
    payload = b"just some text"

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = ExtractorClient("http://core", "seext_probe", transport=httpx.MockTransport(answer))
    job = Job.of(
        {
            "id": JOB_ID,
            "attempt": 1,
            "idempotency_key": "extract:v:basic-metadata:1.0.0:-:1",
            "extractor_id": "basic-metadata",
            "generation": 1,
            "params": {},
            "lease_expires_at": "2026-08-24T12:00:00Z",
            "heartbeat_interval_seconds": 2,
            "cancel_requested": False,
            "file_version": {
                "id": "22222222-2222-2222-2222-222222222222",
                "content_hash": "a" * 64,
                "size": len(payload),
                "media_type": "text/plain",
                "media_class": "document",
                "is_current": True,
            },
            "inputs": [
                {
                    "index": 0,
                    "kind": "original",
                    "url": f"/extractor-api/v1/jobs/{JOB_ID}/inputs/0",
                    "media_type": "text/plain",
                    "size": len(payload),
                    "content_hash": "a" * 64,
                }
            ],
        }
    )

    # A text file has nothing this extractor reads, and that is a completed job with no outputs
    # rather than a failure.
    assert handle(job, JobContext(client=client, _cancelled=threading.Event())) is None


def test_the_manifest_produces_only_metadata() -> None:
    assert MANIFEST["produces"] == ["metadata"]
    assert MANIFEST["accepts"]["mime_types"] == ["*/*"]
    # No derived-asset kinds: nothing here is a single-provider claim, because metadata is
    # deliberately multi-producer (05 § registration).
    assert "derived_asset_kinds" not in MANIFEST
