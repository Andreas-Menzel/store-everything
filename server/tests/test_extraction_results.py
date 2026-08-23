"""What a result envelope does to the database and the disk.

The claim under test is the one everything later leans on: a job's outputs land **whole**, each
row naming the run that produced it, or nothing lands at all. So most of these tests are about the
failing halves — an asset that was never staged, a value that cannot be stored as the type it
declared, a second delivery of the same result — because that is where "one guarded transaction"
either holds or does not.

The rest is chaining and reuse, which are the two ways a job comes to exist without anybody
uploading anything: an extractor's result satisfies another's precondition, or somebody uploads a
copy of a file that has already been analysed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.derived import DerivedStore
from store_everything.problems import problem_type
from store_everything.tables import derived_asset, metadata_entry, segment
from tests.extraction_helpers import (
    as_extractor,
    claim_one,
    extraction_ready,
    finish,
    install,
    rows_in,
    runs_in,
    stage,
)
from tests.upload_helpers import create_upload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONTENT = b"first line\nsecond line\nthird line\n"

#: A sample of every storage class, so one envelope exercises every typed column.
FACTS: list[dict[str, Any]] = [
    {"key": "page_count", "type": "integer", "value": 3},
    {"key": "duration", "type": "duration", "value": 12.5},
    {"key": "camera", "type": "string", "value": "Pixel 9"},
    {"key": "description", "type": "text", "value": "a paragraph about nothing"},
    {"key": "has_text_layer", "type": "boolean", "value": True},
    {"key": "taken_at", "type": "datetime", "value": "2026-08-23T10:11:12+00:00"},
    {"key": "document_date", "type": "date", "value": "2026-08-01"},
    {"key": "gps", "type": "geo", "value": {"lat": 48.137, "lon": 11.575}},
    {"key": "detected_objects", "type": "json", "value": {"labels": ["cat", "sofa"]}},
    {"key": "scene", "type": "string", "value": "indoor", "confidence": 0.82},
]

SEGMENTS: list[dict[str, Any]] = [
    {"text": "first line", "anchor": {"kind": "line", "start_line": 1, "end_line": 1}},
    {
        "text": "page two says this",
        "anchor": {"kind": "page", "page": 2, "char_start": 0, "char_end": 18},
        "confidence": 0.91,
        "language": "en",
    },
    {"text": "spoken here", "anchor": {"kind": "time", "start_ms": 4200, "end_ms": 6100}},
]


async def upload(client: Any, workspace: UUID, path: str, body: bytes = CONTENT) -> dict[str, Any]:
    response = await create_upload(client, workspace, path, body=body)
    assert response.status_code == 201, response.text
    return response.json()


def store_of(settings: Settings) -> DerivedStore:
    return DerivedStore(settings.derived_root)


# --------------------------------------------------------------------------- what lands


@pytest.mark.fr("02/INV-3")
async def test_every_output_lands_naming_the_run_that_produced_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """Invariant 3, from the outside: nothing derived exists without its provenance.

    Asserted through the API rather than the table, because the promise is that a *reader* can
    always tell what produced a fact — a `run_id` nobody can reach would satisfy the schema and
    miss the point.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")

        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            digest, staged = await stage(extractor, job, b"an excerpt")
            assert staged.status_code == 200, staged.text
            done = await finish(
                extractor,
                job,
                metadata=FACTS,
                text_segments=SEGMENTS,
                derived_assets=[
                    {
                        "kind": "text-excerpt",
                        "name": "excerpt.txt",
                        "content_hash": digest,
                        "media_type": "text/plain",
                        "params": {"bytes": 10},
                    }
                ],
            )

        assert done.status_code == 200, done.text
        assert done.json()["stored"] == {
            "metadata": len(FACTS),
            "text_segments": len(SEGMENTS),
            "derived_assets": 1,
        }

        facts = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/metadata")).json()
        assert {fact["key"] for fact in facts} == {fact["key"] for fact in FACTS}
        assert all(fact["extractor"] == "pdf-text" for fact in facts)
        assert all(fact["provenance"] == "auto" for fact in facts)
        assert all(fact["generation"] == 1 for fact in facts)

        spans = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/segments")).json()["data"]
        assert all(span["extractor"] == "pdf-text" for span in spans)

        # And the asset row, which has no reader of its own until thumbnails do (F-028).
        assets = await rows_in(identity_database, derived_asset, "name", "run_id", "generation")
        assert [asset["name"] for asset in assets] == ["excerpt.txt"]
        assert all(asset["run_id"] is not None for asset in assets)


async def test_every_value_type_survives_the_round_trip(
    identity_settings: Settings, identity_database: str
) -> None:
    """A type is a promise about what can be asked later, so it has to come back as it went in."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            assert (await finish(extractor, job, metadata=FACTS)).status_code == 200

        facts = {
            fact["key"]: fact
            for fact in (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/metadata")).json()
        }

        assert facts["page_count"]["value"] == 3
        assert facts["page_count"]["type"] == "integer"
        assert facts["duration"]["value"] == 12.5
        assert facts["camera"]["value"] == "Pixel 9"
        assert facts["has_text_layer"]["value"] is True
        assert facts["taken_at"]["value"].startswith("2026-08-23T10:11:12")
        assert facts["document_date"]["value"].startswith("2026-08-01")
        assert facts["gps"]["value"] == {"lat": 48.137, "lon": 11.575}
        assert facts["detected_objects"]["value"] == {"labels": ["cat", "sofa"]}
        assert facts["scene"]["confidence"] == 0.82


async def test_segments_come_back_in_reading_order_with_their_positions(
    identity_settings: Settings, identity_database: str
) -> None:
    """The positional half of a search hit: *pages 1, 3 and 7*, *at 04:12* (F-004/FR-1)."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(extractor, job, text_segments=SEGMENTS)

        page = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/segments")).json()
        spans = page["data"]

        assert [span["ordinal"] for span in spans] == [0, 1, 2]
        assert [span["text"] for span in spans] == [entry["text"] for entry in SEGMENTS]
        assert spans[0]["anchor_kind"] == "line"
        assert spans[0]["anchor"] == {"start_line": 1, "end_line": 1}
        assert spans[1]["anchor"] == {"page": 2, "char_start": 0, "char_end": 18}
        assert spans[1]["confidence"] == 0.91
        assert spans[1]["language"] == "en"
        assert spans[2]["anchor"] == {"start_ms": 4200, "end_ms": 6100}
        assert page["next_cursor"] is None


async def test_segments_are_paginated(identity_settings: Settings, identity_database: str) -> None:
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")
        many = [
            {
                "text": f"line {number}",
                "anchor": {"kind": "line", "start_line": number, "end_line": number},
            }
            for number in range(1, 8)
        ]
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(extractor, job, text_segments=many)

        first = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/segments?limit=3")).json()
        assert [span["ordinal"] for span in first["data"]] == [0, 1, 2]
        assert first["next_cursor"] is not None

        second = (
            await client.get(
                f"{API_V1_PREFIX}/files/{created['id']}/segments",
                params={"limit": 3, "cursor": first["next_cursor"]},
            )
        ).json()
        assert [span["ordinal"] for span in second["data"]] == [3, 4, 5]


async def test_a_staged_asset_lands_under_the_source_hash(
    identity_settings: Settings, identity_database: str
) -> None:
    """The layout of 09 § storage, which is what makes duplicate files share one set of assets."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            digest, _ = await stage(extractor, job, b"an excerpt")
            await finish(
                extractor,
                job,
                derived_assets=[
                    {
                        "kind": "text-excerpt",
                        "name": "excerpt.txt",
                        "content_hash": digest,
                        "media_type": "text/plain",
                    }
                ],
            )

        placed = store_of(identity_settings).path_for(created["content_hash"], "excerpt.txt")
        assert placed.is_file()
        assert placed.read_bytes() == b"an excerpt"
        # And nothing is left in staging: the move is a rename, not a copy.
        staging = store_of(identity_settings).staging_root
        assert not any(staging.rglob("*")) or not any(
            entry.is_file() for entry in staging.rglob("*")
        )


async def test_staging_the_same_asset_twice_is_harmless(
    identity_settings: Settings, identity_database: str
) -> None:
    """A retried upload must cost nothing: at-least-once applies to bytes as much as to jobs."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            first_digest, first = await stage(extractor, job, b"an excerpt")
            second_digest, second = await stage(extractor, job, b"an excerpt")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first_digest == second_digest
        assert second.json()["size"] == len(b"an excerpt")


# ------------------------------------------------------------------------ what is refused


async def test_bytes_that_do_not_match_their_digest_are_refused(
    identity_settings: Settings, identity_database: str
) -> None:
    """The hash in the URL is a claim, and staging is where it stops being one."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            wrong = await extractor.put(
                f"/extractor-api/v1/jobs/{job['id']}/assets/{'a' * 64}",
                content=b"not what the hash says",
            )

        assert wrong.status_code == 422
        assert wrong.json()["type"] == problem_type("content-hash-mismatch")


async def test_an_envelope_referencing_an_unstaged_asset_writes_nothing(
    identity_settings: Settings, identity_database: str
) -> None:
    """All or nothing: the metadata in the same envelope must not survive its missing asset."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            refused = await finish(
                extractor,
                job,
                metadata=[{"key": "page_count", "type": "integer", "value": 3}],
                derived_assets=[
                    {
                        "kind": "text-excerpt",
                        "name": "excerpt.txt",
                        "content_hash": "b" * 64,
                        "media_type": "text/plain",
                    }
                ],
            )

        assert refused.status_code == 409, refused.text
        assert refused.json()["type"] == problem_type("asset-not-staged")
        assert await rows_in(identity_database, metadata_entry, "key") == []
        assert await rows_in(identity_database, derived_asset, "name") == []
        # The job is still running, so the work can be submitted properly.
        status = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")).json()
        assert status["status"] == "pending"


@pytest.mark.parametrize(
    ("outputs", "because"),
    [
        (
            {"metadata": [{"key": "flag", "type": "boolean", "value": "yes"}]},
            "a string is not a boolean",
        ),
        (
            {"metadata": [{"key": "gps", "type": "geo", "value": {"lat": 91, "lon": 0}}]},
            "that latitude is not on the earth",
        ),
        (
            {"metadata": [{"key": "gps", "type": "geo", "value": [48.1, 11.5]}]},
            "a geo value is named, not positional",
        ),
        (
            {"metadata": [{"key": "count", "type": "integer", "value": "seven"}]},
            "a word is not an integer",
        ),
        (
            {"metadata": [{"key": "k", "type": "quaternion", "value": 1}]},
            "an unknown type could not be stored correctly",
        ),
        (
            {"text_segments": [{"text": "x", "anchor": {"kind": "page", "page": 0}}]},
            "there is no page zero",
        ),
        (
            {
                "text_segments": [
                    {"text": "x", "anchor": {"kind": "time", "start_ms": 900, "end_ms": 100}}
                ]
            },
            "that span ends before it starts",
        ),
        (
            {"text_segments": [{"text": "x", "anchor": {"kind": "sundial", "at": "noon"}}]},
            "an unknown anchor kind has no shape to store",
        ),
    ],
)
async def test_an_envelope_that_cannot_be_stored_is_refused_whole(
    identity_settings: Settings,
    identity_database: str,
    outputs: dict[str, Any],
    because: str,
) -> None:
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            refused = await finish(extractor, job, **outputs)

        assert refused.status_code == 422, f"{because}: {refused.text}"
        assert await rows_in(identity_database, metadata_entry, "key") == []
        assert await rows_in(identity_database, segment, "text") == []


@pytest.mark.parametrize("name", ["../escape.txt", "/etc/passwd", ".hidden", "Excerpt.TXT", ""])
async def test_an_asset_name_that_is_not_a_name_is_refused(
    identity_settings: Settings, identity_database: str, name: str
) -> None:
    """The core owns the directory. A name that could be a path is refused, never sanitised."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            digest, _ = await stage(extractor, job, b"an excerpt")
            refused = await finish(
                extractor,
                job,
                derived_assets=[
                    {
                        "kind": "text-excerpt",
                        "name": name,
                        "content_hash": digest,
                        "media_type": "text/plain",
                    }
                ],
            )

        assert refused.status_code == 422, refused.text
        assert await rows_in(identity_database, derived_asset, "name") == []


async def test_the_same_result_twice_replaces_rather_than_doubles(
    identity_settings: Settings, identity_database: str
) -> None:
    """At-least-once delivery means the *write* has to be idempotent, not the network."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            first = await finish(extractor, job, metadata=FACTS, text_segments=SEGMENTS)
            second = await finish(extractor, job, metadata=FACTS, text_segments=SEGMENTS)

        assert first.status_code == 200
        # The second is refused as a lost claim rather than applied — the job is already done.
        assert second.status_code == 409
        assert len(await rows_in(identity_database, metadata_entry, "key")) == len(FACTS)
        assert len(await rows_in(identity_database, segment, "text")) == len(SEGMENTS)


# --------------------------------------------------------------------------- rebuilding


@pytest.mark.fr("02/INV-5")
async def test_derived_data_can_be_thrown_away_and_rebuilt(
    identity_settings: Settings, identity_database: str
) -> None:
    """Invariant 5: everything derived is regenerable from the source plane.

    Exercised the way reprocessing will: the rows go, the file does not, and running the same
    extractor over the same bytes puts back the same answers.
    """
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import create_async_engine

    from store_everything.tables import extraction_run

    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(extractor, job, metadata=FACTS, text_segments=SEGMENTS)

        before = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/metadata")).json()

        # Everything derived, gone — runs included, which is what a purge of derived data means.
        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                await connection.execute(delete(extraction_run))
                await connection.commit()
        finally:
            await engine.dispose()

        assert (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/metadata")).json() == []
        assert (await client.get(f"{API_V1_PREFIX}/files/{created['id']}")).json()[
            "extraction_status"
        ] == "none"
        # The file itself is untouched: derived data is the only thing that went.
        content = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/content")
        assert content.content == CONTENT

        # Re-routing is what reprocessing does; here a re-upload of the same path stands in.
        await create_upload(client, workspace, "report.txt", body=CONTENT, if_exists="new_version")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(extractor, job, metadata=FACTS, text_segments=SEGMENTS)

        after = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/metadata")).json()
        assert [fact["key"] for fact in after] == [fact["key"] for fact in before]
        assert [fact["value"] for fact in after] == [fact["value"] for fact in before]


# ----------------------------------------------------------------------------- chaining


async def test_a_result_can_satisfy_another_extractors_precondition(
    identity_settings: Settings, identity_database: str
) -> None:
    """How `tesseract-ocr` learns a PDF needs it, without either extractor knowing the other.

    `pdf-text` writes `needs_ocr`; the OCR extractor's manifest says it accepts PDFs *when* that
    key is true, and asks for `ocr_pages` as a job parameter. The routing pass that runs when the
    first result lands is what connects them (04 § routing).
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        finder = await install(app, client, "pdf-text", accepts={"mime_types": ["*/*"]})
        ocr = await install(
            app,
            client,
            "tesseract-ocr",
            accepts={
                "mime_types": ["*/*"],
                "when": {"key": "needs_ocr", "equals": True},
                "params_from": {"ocr_pages": "pages"},
            },
        )
        created = await upload(client, workspace, "scan.pdf")

        # Before the first result, the OCR extractor has nothing to do.
        async with as_extractor(app, ocr.token) as extractor:
            assert await claim_one(extractor) is None

        async with as_extractor(app, finder.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(
                extractor,
                job,
                metadata=[
                    {"key": "needs_ocr", "type": "boolean", "value": True},
                    {"key": "ocr_pages", "type": "json", "value": [1, 4, 5]},
                ],
            )

        async with as_extractor(app, ocr.token) as extractor:
            chained = await claim_one(extractor)
            assert chained is not None, "the predicate was satisfied but no job appeared"
            assert chained["params"] == {"pages": [1, 4, 5]}
            await finish(extractor, chained, text_segments=SEGMENTS[:1])

        status = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")).json()
        assert status["status"] == "indexed"
        assert {run["extractor"] for run in status["runs"]} == {"pdf-text", "tesseract-ocr"}


async def test_a_predicate_that_is_not_satisfied_never_routes(
    identity_settings: Settings, identity_database: str
) -> None:
    """A flag is not a count: `True` and `1` are different answers (and Python disagrees)."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        finder = await install(app, client, "pdf-text")
        ocr = await install(
            app,
            client,
            "tesseract-ocr",
            accepts={"mime_types": ["*/*"], "when": {"key": "needs_ocr", "equals": True}},
        )
        await upload(client, workspace, "digital.pdf")

        async with as_extractor(app, finder.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(
                extractor,
                job,
                metadata=[
                    {"key": "needs_ocr", "type": "integer", "value": 1},
                    {"key": "has_text_layer", "type": "boolean", "value": True},
                ],
            )

        async with as_extractor(app, ocr.token) as extractor:
            assert await claim_one(extractor) is None


async def test_a_derived_asset_becomes_another_extractors_input(
    identity_settings: Settings, identity_database: str
) -> None:
    """The other half of chaining: an office document's converted PDF, a video's keyframes.

    One job per asset — which is what makes a video's fifty keyframes fifty pieces of work rather
    than one that cannot be resumed (12 § job atomicity).
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        producer = await install(
            app,
            client,
            "video-keyframes",
            produces=["derived_assets"],
            derived_asset_kinds=["keyframe"],
        )
        consumer = await install(
            app,
            client,
            "image-vision",
            accepts={"mime_types": [], "derived_kinds": ["keyframe"]},
        )
        created = await upload(client, workspace, "clip.txt")

        async with as_extractor(app, producer.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            frames = []
            for index, payload in enumerate((b"frame one", b"frame two"), start=1):
                digest, _ = await stage(extractor, job, payload)
                frames.append(
                    {
                        "kind": "keyframe",
                        "name": f"keyframe-{index:04d}.txt",
                        "content_hash": digest,
                        "media_type": "text/plain",
                        "params": {"at_ms": index * 1000},
                    }
                )
            await finish(extractor, job, derived_assets=frames)

        # The consumer never accepted a media type, only a derived kind — so it gets one job per
        # keyframe, each pointed at its own asset.
        claimed: list[dict[str, Any]] = []
        async with as_extractor(app, consumer.token) as extractor:
            while (job := await claim_one(extractor)) is not None:
                served = await extractor.get(f"/extractor-api/v1/jobs/{job['id']}/inputs/0")
                assert served.status_code == 200, served.text
                claimed.append({**job, "bytes": served.content})
                await finish(extractor, job)

        assert len(claimed) == 2
        assert {job["bytes"] for job in claimed} == {b"frame one", b"frame two"}
        for job in claimed:
            assert job["inputs"][0]["kind"] == "derived"
            assert job["inputs"][0]["asset_kind"] == "keyframe"
            assert job["inputs"][0]["asset"] is not None

        status = (await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")).json()
        assert status["status"] == "indexed"
        assert len([run for run in status["runs"] if run["extractor"] == "image-vision"]) == 2


# ------------------------------------------------------------------------------- reuse


@pytest.mark.fr("F-009/FR-8")
async def test_identical_content_reuses_the_analysis_instead_of_repeating_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """The same bytes analysed by the same extractor version need no second analysis.

    The rows are this version's own — a reader cannot tell the difference, which is the point —
    and the run records that they were copied. The asset needs no copy at all: it lives under the
    source content hash, which is identical by definition here.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        first = await upload(client, workspace, "report.txt")

        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            digest, _ = await stage(extractor, job, b"an excerpt")
            await finish(
                extractor,
                job,
                metadata=FACTS,
                text_segments=SEGMENTS,
                derived_assets=[
                    {
                        "kind": "text-excerpt",
                        "name": "excerpt.txt",
                        "content_hash": digest,
                        "media_type": "text/plain",
                    }
                ],
            )

        # The same bytes at another path: a copy, which is what a person makes all the time.
        second = await upload(client, workspace, "copy.txt")

        assert second["extraction_status"] == "indexed", "reuse should complete on arrival"
        async with as_extractor(app, installed.token) as extractor:
            assert await claim_one(extractor) is None, "no job should have been created"

        runs = await runs_in(identity_database)
        assert len(runs) == 2
        reused = next(run for run in runs if run["file_version_id"] == UUID(second["version"]))
        original = next(run for run in runs if run["file_version_id"] == UUID(first["version"]))
        assert reused["state"] == "succeeded"
        assert reused["extractor_version"] == original["extractor_version"]

        # The copy's own rows, indistinguishable from the original's.
        for identifier in (first["id"], second["id"]):
            facts = (await client.get(f"{API_V1_PREFIX}/files/{identifier}/metadata")).json()
            spans = (await client.get(f"{API_V1_PREFIX}/files/{identifier}/segments")).json()[
                "data"
            ]
            assert len(facts) == len(FACTS)
            assert [span["text"] for span in spans] == [entry["text"] for entry in SEGMENTS]

        assets = await rows_in(identity_database, derived_asset, "name", "content_hash")
        assert len(assets) == 2, "each version has its own row"
        assert len({asset["content_hash"] for asset in assets}) == 1, "pointing at the same bytes"
        # One file on disk, because the location is the source hash and the source hash is equal.
        placed = store_of(identity_settings).path_for(first["content_hash"], "excerpt.txt")
        assert placed.is_file()
        assert placed.read_bytes() == b"an excerpt"


async def test_a_different_extractor_version_does_not_reuse(
    identity_settings: Settings, identity_database: str
) -> None:
    """Reuse is exact or it is wrong: a new version of the extractor is a new answer."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(extractor, job, metadata=FACTS)
            # The image is upgraded, and says so.
            upgraded = await extractor.put(
                "/extractor-api/v1/registration",
                json={
                    "id": installed.id,
                    "version": "2.0.0",
                    "api_version": "v1",
                    "model": {"name": "pymupdf", "version": "1.28"},
                    "accepts": {"mime_types": ["*/*"]},
                    "produces": ["text_segments"],
                },
            )
            assert upgraded.status_code == 200, upgraded.text

        second = await upload(client, workspace, "copy.txt")

        assert second["extraction_status"] == "pending", "the new version has work to do"
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            assert job["file_version"]["id"] == second["version"]


async def test_a_reused_run_records_where_its_rows_came_from(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Provenance has to be honest about it: the rows are real, the analysis happened once."""
    from store_everything.tables import extraction_run

    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(extractor, job, metadata=FACTS)
        await upload(client, workspace, "copy.txt")

        runs = await rows_in(identity_database, extraction_run, "id", "reused_from", "state")
        reused = [run for run in runs if run["reused_from"] is not None]
        assert len(reused) == 1
        assert reused[0]["state"] == "succeeded"
        assert reused[0]["reused_from"] in {run["id"] for run in runs}
