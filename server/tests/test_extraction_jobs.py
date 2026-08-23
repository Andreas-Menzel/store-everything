"""The life of an extraction job, driven over the wire the way a container drives it.

Routing happens in the transaction that creates a file version, so these tests upload a file and
then look at what appeared: one job per matching extractor, a run per job, and a status the
listing tells the truth about. From there the extractor claims, heartbeats, reads its input and
finishes — or fails, or loses its lease — and each of those is a rule from ADR-0020 rather than
an implementation detail.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from store_everything import extraction
from store_everything.api.extractor_api import EXTRACTOR_API_PREFIX
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.problems import problem_type
from tests.extraction_helpers import (
    CLAIM,
    EXTRACTORS,
    as_extractor,
    claim_one,
    expire_lease,
    extraction_ready,
    finish,
    heartbeat,
    install,
    jobs_in,
    read_input,
    report_error,
    runs_in,
)
from tests.identity_helpers import SAME_ORIGIN
from tests.upload_helpers import create_upload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONTENT = b"a report about quarterly things"


async def upload(
    client: Any, workspace_id: UUID, path: str, body: bytes = CONTENT, *, replace: bool = False
) -> dict[str, Any]:
    response = await create_upload(
        client, workspace_id, path, body=body, if_exists="new_version" if replace else None
    )
    assert response.status_code == 201, response.text
    return response.json()


# ----------------------------------------------------------------------------------- routing


@pytest.mark.fr("F-001/FR-3", "F-001/FR-8")
async def test_an_upload_queues_extraction_and_says_so(
    identity_settings: Settings, identity_database: str
) -> None:
    """The two halves phase 1 could not verify: the upload answers with a reference to the work
    it queued, and the file is immediately listed as `pending` rather than as finished."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await install(app, client)

        created = await upload(client, workspace, "report.txt")

        assert created["extraction_status"] == "pending"
        assert created["version"] is not None

        # The reference: somewhere to ask, because there is no result to return yet.
        status = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["status"] == "pending"
        assert [run["extractor"] for run in body["runs"]] == ["pdf-text"]
        assert body["runs"][0]["state"] == "queued"
        # Nothing has run, so nothing is stamped with a version yet.
        assert body["runs"][0]["extractor_version"] is None

        listed = await client.get(f"{API_V1_PREFIX}/folders/{created['id']}/children")
        # The file's own row in its folder listing says the same thing.
        children = await client.get(
            f"{API_V1_PREFIX}/folders/{(await _root_of(client, workspace))}/children"
        )
        assert listed.status_code in {200, 404}
        assert children.status_code == 200, children.text
        rows = [row for row in children.json()["data"] if row["kind"] == "file"]
        assert [row["extraction_status"] for row in rows] == ["pending"]


async def _root_of(client: Any, workspace: UUID) -> str:
    response = await client.get(f"{API_V1_PREFIX}/workspaces/{workspace}")
    assert response.status_code == 200, response.text
    return response.json()["root_folder"]


async def test_a_file_nothing_analyses_says_so_rather_than_pretending(
    identity_settings: Settings, identity_database: str
) -> None:
    """`none` is not a failure: no installed extractor accepts this type."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await install(app, client, "image-vision", accepts={"mime_types": ["image/*"]})

        created = await upload(client, workspace, "notes.txt")

        assert created["extraction_status"] == "none"
        assert await runs_in(identity_database) == []


async def test_only_matching_enabled_registered_extractors_get_work(
    identity_settings: Settings, identity_database: str
) -> None:
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await install(app, client, "text-plain", accepts={"mime_types": ["text/*"]})
        await install(app, client, "image-vision", accepts={"mime_types": ["image/*"]})
        switched_off = await install(app, client, "basic-metadata")
        await client.patch(
            f"{EXTRACTORS}/{switched_off.id}", json={"enabled": False}, headers=SAME_ORIGIN
        )
        # Provisioned but never started: no manifest, so nothing is known about what it takes.
        await client.post(EXTRACTORS, json={"id": "never-started"}, headers=SAME_ORIGIN)

        await upload(client, workspace, "notes.txt")

        assert [run["extractor_id"] for run in await runs_in(identity_database)] == ["text-plain"]


async def test_an_extractor_waiting_on_another_result_is_not_routed_yet(
    identity_settings: Settings, identity_database: str
) -> None:
    """`accepts.when` is a precondition, and nothing can satisfy it before derived data exists —
    so `tesseract-ocr` waiting on `needs_ocr` gets no job rather than a premature one."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await install(
            app,
            client,
            "tesseract-ocr",
            accepts={
                "mime_types": ["application/pdf"],
                "when": {"key": "needs_ocr", "equals": True},
            },
        )

        await upload(client, workspace, "scan.pdf")

        assert await runs_in(identity_database) == []


async def test_the_core_assigns_the_priority_from_what_an_extractor_produces(
    identity_settings: Settings, identity_database: str
) -> None:
    """Presence work outranks searchability work, and extractors never pick (04 § scheduling)."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await install(
            app,
            client,
            "preview-gen",
            produces=["derived_assets"],
            derived_asset_kinds=["thumbnail"],
        )
        await install(app, client, "text-plain", produces=["text_segments"])

        await upload(client, workspace, "notes.txt")

        priorities = {job["kind"]: job["priority"] for job in await jobs_in(identity_database)}
        assert priorities == {
            "extract.preview-gen": extraction.PRIORITY_PRESENCE,
            "extract.text-plain": extraction.PRIORITY_SEARCHABILITY,
        }


async def test_routing_the_same_version_twice_creates_one_job(
    identity_settings: Settings, identity_database: str
) -> None:
    """Idempotent per (version, extractor, generation) — the reason it is safe to route from
    every path that makes a version instead of from one careful caller."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await upload(client, workspace, "report.txt")

        # Registering again changes nothing about work already queued.
        async with as_extractor(app, installed.token) as extractor:
            assert (await claim_one(extractor)) is not None
            assert (await claim_one(extractor)) is None

        assert len(await runs_in(identity_database)) == 1
        assert created["extraction_status"] == "pending"


async def test_a_scan_routes_what_it_finds(
    identity_settings: Settings, identity_database: str, tmp_path: Any
) -> None:
    """External arrivals are routed too: detection has one destination, whatever triggered it."""
    from tests.workspace_helpers import scan_pending

    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        root,
    ):
        await install(app, client)
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "data" / "arrived.txt").write_bytes(b"put here by somebody else")

        await client.post(
            f"{API_V1_PREFIX}/workspaces/{workspace}/rescan", json={}, headers=SAME_ORIGIN
        )
        await scan_pending(identity_database, identity_settings)

        assert [run["extractor_id"] for run in await runs_in(identity_database)] == ["pdf-text"]


# -------------------------------------------------------------------------------- the claim


async def test_a_claim_hands_over_everything_needed_to_do_the_work(
    identity_settings: Settings, identity_database: str
) -> None:
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
        assert job["extractor_id"] == "pdf-text"
        assert job["attempt"] == 1
        assert job["generation"] == 1
        assert job["cancel_requested"] is False
        assert job["heartbeat_interval_seconds"] == identity_settings.heartbeat_seconds
        assert job["idempotency_key"].startswith("extract:")
        assert job["file_version"]["id"] == created["version"]
        assert job["file_version"]["content_hash"] == created["content_hash"]
        assert job["file_version"]["size"] == len(CONTENT)
        assert job["file_version"]["is_current"] is True
        assert job["inputs"] == [
            {
                "index": 0,
                "kind": "original",
                "url": f"{EXTRACTOR_API_PREFIX}/jobs/{job['id']}/inputs/0",
                "media_type": "text/plain",
                "size": len(CONTENT),
                "content_hash": created["content_hash"],
                "digest_algorithm": "sha256",
                # Absent for a job over the file's own bytes; a chained job names its asset.
                "asset": None,
                "asset_kind": None,
            }
        ]

        # The run is now running, stamped with the versions of the image that claimed it.
        runs = await runs_in(identity_database)
        assert runs[0]["state"] == "running"
        assert runs[0]["extractor_version"] == "1.0.0"
        assert runs[0]["model_version"] == "1.28"

        status = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")
        assert status.json()["status"] == "pending"


async def test_an_idle_queue_answers_nothing_without_hanging(
    identity_settings: Settings, identity_database: str
) -> None:
    """A bounded wait, so a claim cannot hold a request — or a connection — open forever."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        _workspace,
        _root,
    ):
        installed = await install(app, client)

        async with as_extractor(app, installed.token) as extractor:
            immediate = await extractor.post(CLAIM, json={})
            waited = await extractor.post(f"{CLAIM}?wait=1", json={})
            refused = await extractor.post(f"{CLAIM}?wait=999", json={})

        assert immediate.status_code == 204
        assert waited.status_code == 204
        assert refused.status_code == 422, refused.text


async def test_a_disabled_extractor_is_told_why_it_has_no_work(
    identity_settings: Settings, identity_database: str
) -> None:
    """The difference between "nothing to do" and "you were switched off" is what an operator
    needs when they wonder why nothing is being processed."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await upload(client, workspace, "report.txt")
        await client.patch(
            f"{EXTRACTORS}/{installed.id}", json={"enabled": False}, headers=SAME_ORIGIN
        )

        async with as_extractor(app, installed.token) as extractor:
            response = await extractor.post(CLAIM, json={})

        assert response.status_code == 409
        assert response.json()["type"] == problem_type("extractor-disabled")


async def test_one_extractor_cannot_touch_another_extractors_job(
    identity_settings: Settings, identity_database: str
) -> None:
    """Ownership is the job's kind, so a credential is the whole of the authorisation."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        mine = await install(app, client, "text-plain", accepts={"mime_types": ["text/*"]})
        theirs = await install(app, client, "image-vision", accepts={"mime_types": ["image/*"]})
        await upload(client, workspace, "notes.txt")

        async with as_extractor(app, mine.token) as extractor:
            job = await claim_one(extractor)
        assert job is not None

        async with as_extractor(app, theirs.token) as intruder:
            claimed = await claim_one(intruder)
            beat = await heartbeat(intruder, job)
            finished = await finish(intruder, job)
            bytes_read = await read_input(intruder, job)

        assert claimed is None, "another extractor's job is not claimable work"
        assert beat.status_code == 404
        assert finished.status_code == 404
        assert bytes_read.status_code == 404


# -------------------------------------------------------------------------------- the inputs


async def test_an_extractor_reads_exactly_the_bytes_of_its_job(
    identity_settings: Settings, identity_database: str
) -> None:
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
            whole = await read_input(extractor, job)
            ranged = await read_input(extractor, job, headers={"Range": "bytes=2-6"})
            absent = await read_input(extractor, job, index=1)

        assert whole.status_code == 200
        assert whole.content == CONTENT
        assert whole.headers["etag"] == f'"{created["content_hash"]}"'
        assert ranged.status_code == 206
        assert ranged.content == CONTENT[2:7]
        assert absent.status_code == 404


async def test_a_superseded_version_is_still_readable_from_the_apps_own_copy(
    identity_settings: Settings, identity_database: str
) -> None:
    """A job outlives the version it was created for, and its bytes moved to `versions/` when
    the app mediated the change — so the work can still be done (F-007/FR-9)."""
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
            # A new version of the same path lands while the job is in flight.
            await upload(
                client, workspace, "report.txt", b"a completely different report", replace=True
            )
            served = await read_input(extractor, job)

        assert served.status_code == 200
        assert served.content == CONTENT


# ------------------------------------------------------------------- heartbeat and finishing


async def test_a_heartbeat_extends_the_lease_and_carries_the_answer_to_stop(
    identity_settings: Settings, identity_database: str
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
            beat = await heartbeat(extractor, job)

            # A newer version supersedes the work still queued for the old one.
            await upload(client, workspace, "report.txt", b"newer content entirely", replace=True)
            after_supersession = await heartbeat(extractor, job)

        assert beat.status_code == 200, beat.text
        assert beat.json()["cancel"] is False
        assert beat.json()["lease_expires_at"] > job["lease_expires_at"]
        assert after_supersession.status_code == 200
        assert after_supersession.json()["cancel"] is True


async def test_finishing_a_job_indexes_the_file(
    identity_settings: Settings, identity_database: str
) -> None:
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
            finished = await finish(extractor, job)

        assert finished.status_code == 200, finished.text
        assert finished.json()["state"] == "succeeded"
        assert finished.json()["finished_at"] is not None

        status = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")
        assert status.json()["status"] == "indexed"
        assert status.json()["runs"][0]["state"] == "succeeded"
        assert (await client.get(f"{API_V1_PREFIX}/files/{created['id']}")).json()[
            "extraction_status"
        ] == "indexed"
        assert [job["state"] for job in await jobs_in(identity_database)] == ["succeeded"]


async def test_a_result_from_a_lost_claim_is_refused(
    identity_settings: Settings, identity_database: str
) -> None:
    """The fencing token, from the outside: a worker that lost its lease cannot overwrite the
    run that replaced it (12 § leases & fencing)."""
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
            # The worker stalls; the lease lapses and a successor takes the job over.
            await expire_lease(identity_database, job["id"])
            successor = await claim_one(extractor)
            assert successor is not None
            assert successor["attempt"] == job["attempt"] + 1

            stale_result = await finish(extractor, job)
            stale_beat = await heartbeat(extractor, job)
            stale_error = await report_error(extractor, job, message="too late")

        for response in (stale_result, stale_beat, stale_error):
            assert response.status_code == 409, response.text
            assert response.json()["type"] == problem_type("lease-lost")


async def test_the_same_result_delivered_twice_persists_once(
    identity_settings: Settings, identity_database: str
) -> None:
    """At-least-once delivery, deduplicated on write (05 § job lifecycle)."""
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
            first = await finish(extractor, job)
            second = await finish(extractor, job)

        assert first.status_code == 200
        assert second.status_code == 409
        runs = await runs_in(identity_database)
        assert len(runs) == 1
        assert runs[0]["state"] == "succeeded"


# ------------------------------------------------------------------------------- failing


async def test_a_retryable_failure_comes_back_around(
    identity_settings: Settings, identity_database: str
) -> None:
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
            reported = await report_error(extractor, job, message="the model was not loaded")

        assert reported.status_code == 200, reported.text
        assert reported.json()["state"] == "queued"

        status = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")
        assert status.json()["status"] == "pending", "a retry pending is still pending"
        assert status.json()["runs"][0]["error"] == "the model was not loaded"
        assert status.json()["runs"][0]["finished_at"] is None


async def test_a_file_this_extractor_can_never_read_fails_without_a_retry(
    identity_settings: Settings, identity_database: str
) -> None:
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
            reported = await report_error(
                extractor, job, message="not a PDF at all", retryable=False
            )
            nothing_left = await claim_one(extractor)

        assert reported.json()["state"] == "failed"
        assert nothing_left is None

        status = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")
        assert status.json()["status"] == "failed"
        assert status.json()["runs"][0]["error"] == "not a PDF at all"
        # The file itself is untouched by a failed extractor: it is stored and browsable.
        summary = await client.get(f"{API_V1_PREFIX}/files/{created['id']}")
        assert summary.status_code == 200
        assert summary.json()["extraction_status"] == "failed"


async def test_one_failed_extractor_of_two_is_partial_not_broken(
    identity_settings: Settings, identity_database: str
) -> None:
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        good = await install(app, client, "text-plain")
        bad = await install(app, client, "basic-metadata")
        created = await upload(client, workspace, "report.txt")

        async with as_extractor(app, good.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await finish(extractor, job)
        async with as_extractor(app, bad.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await report_error(extractor, job, message="exiftool is missing", retryable=False)

        status = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")
        assert status.json()["status"] == "partial"
        assert {run["extractor"]: run["state"] for run in status.json()["runs"]} == {
            "text-plain": "succeeded",
            "basic-metadata": "failed",
        }


async def test_extraction_status_needs_no_extractor_credential_to_read(
    identity_settings: Settings, identity_database: str
) -> None:
    """It is the file's owner who asks why their file is not searchable yet."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await install(app, client)
        created = await upload(client, workspace, "report.txt")

        response = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")

        assert response.status_code == 200
        assert response.json()["version"] == created["version"]
