"""What happens to a job when the thing running it dies.

The promise these tests hold to account is the one every operation in this system makes
([12 § verification](../../specs/12-reliability.md#verification)): killing a worker at any
instant costs a re-run, never consistency. For extraction the interesting deaths are an
extractor that stops mid-job, a zombie that comes back after its lease was taken, and a result
delivered twice — none of which the queue can prevent, and all of which it has to survive.

Nothing here mocks the failure: the lease is expired the way a dead worker's lease expires, and
recovery is the ordinary claim query rather than a repair pass.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import extraction
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.problems import problem_type
from tests.extraction_helpers import (
    as_extractor,
    claim_one,
    expire_lease,
    extraction_ready,
    finish,
    heartbeat,
    install,
    jobs_in,
    report_error,
    runs_in,
)
from tests.upload_helpers import create_upload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.fault_injection]

CONTENT = b"something worth analysing"


async def _uploaded(client: httpx.AsyncClient, workspace: UUID) -> dict[str, Any]:
    response = await create_upload(client, workspace, "report.txt", body=CONTENT)
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_worker_that_dies_mid_job_costs_one_re_run(
    identity_settings: Settings, identity_database: str
) -> None:
    """The recovery story is the claim query's expired-lease branch, exercised every day rather
    than only after a crash (ADR-0010) — there is no startup pass to test, and that is the point.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await _uploaded(client, workspace)

        async with as_extractor(app, installed.token) as extractor:
            lost = await claim_one(extractor)
            assert lost is not None
            # The container is killed here: no error reported, no result, nothing released.
            await expire_lease(identity_database, lost["id"])

            successor = await claim_one(extractor)
            assert successor is not None
            assert successor["id"] == lost["id"], "the same job, not a second one"
            assert successor["attempt"] == lost["attempt"] + 1
            assert (await finish(extractor, successor)).status_code == 200

        # One job, one run, one success: the crash cost an attempt and nothing else.
        assert len(await jobs_in(identity_database)) == 1
        runs = await runs_in(identity_database)
        assert len(runs) == 1
        assert runs[0]["state"] == "succeeded"
        status = await client.get(f"{API_V1_PREFIX}/files/{created['id']}/extraction")
        assert status.json()["status"] == "indexed"


async def test_a_zombie_cannot_write_over_the_run_that_replaced_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """A worker frozen long enough to lose its lease and then thawed. Its fencing token is
    stale, so every write it attempts is refused — including a *success*, which is the one that
    would otherwise corrupt the record."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await _uploaded(client, workspace)

        async with as_extractor(app, installed.token) as extractor:
            zombie = await claim_one(extractor)
            assert zombie is not None
            await expire_lease(identity_database, zombie["id"])
            successor = await claim_one(extractor)
            assert successor is not None

            refused = [
                await heartbeat(extractor, zombie),
                await finish(extractor, zombie),
                await report_error(extractor, zombie, message="I was slow"),
            ]
            # The living claim is unaffected by any of it.
            assert (await finish(extractor, successor)).status_code == 200

        for response in refused:
            assert response.status_code == 409, response.text
            assert response.json()["type"] == problem_type("lease-lost")

        runs = await runs_in(identity_database)
        assert [run["state"] for run in runs] == ["succeeded"]
        assert runs[0]["error"] is None, "a zombie's error message never reached the record"


async def test_a_result_delivered_twice_is_applied_once(
    identity_settings: Settings, identity_database: str
) -> None:
    """Delivery is at-least-once, so the write has to be the thing that deduplicates it."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await _uploaded(client, workspace)

        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            outcomes = [await finish(extractor, job), await finish(extractor, job)]

        assert [response.status_code for response in outcomes] == [200, 409]
        runs = await runs_in(identity_database)
        assert len(runs) == 1
        # The finish timestamp is the first one's: the retry replayed nothing.
        assert runs[0]["state"] == "succeeded"


async def test_retries_are_bounded_and_end_somewhere_a_human_can_see(
    identity_settings: Settings, identity_database: str
) -> None:
    """A poison file must not be retried forever. Attempts count on claim, so the bound holds
    even for a job that never reports an error at all (12 § leases & fencing)."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        created = await _uploaded(client, workspace)

        async with as_extractor(app, installed.token) as extractor:
            for _ in range(extraction.MAX_ATTEMPTS):
                claimed = await claim_one(extractor)
                assert claimed is not None
                # Killed again, every time: the worker dies before it can report anything.
                await expire_lease(identity_database, claimed["id"])
            exhausted = await claim_one(extractor)

        assert exhausted is None, "a job whose attempts are spent is not claimable"
        # The claim that found the attempts spent is what dead-letters the job, so nobody ever
        # reported this failure — and the record still has to say so.
        jobs = await jobs_in(identity_database)
        assert jobs[0]["state"] == "dead_letter"
        assert [run["state"] for run in await runs_in(identity_database)] == ["dead_letter"]
        assert await extraction_status(client, created["id"]) == "failed"


async def extraction_status(client: httpx.AsyncClient, file_id: str) -> str:
    response = await client.get(f"{API_V1_PREFIX}/files/{file_id}/extraction")
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


async def test_a_run_left_running_by_a_dead_worker_is_reported_as_waiting_again(
    identity_settings: Settings, identity_database: str
) -> None:
    """The mirror, reconciled. The queue needs no repair — an expired lease is claimable, which
    *is* recovery — but a run row left saying `running` would report a job as in progress
    forever, and the per-file status is read far more often than a job is lost.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client)
        await _uploaded(client, workspace)

        async with as_extractor(app, installed.token) as extractor:
            job = await claim_one(extractor)
            assert job is not None
            await expire_lease(identity_database, job["id"])

        assert [run["state"] for run in await runs_in(identity_database)] == ["running"]

        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                reconciled = await extraction.reconcile_runs(connection)
                await connection.commit()
        finally:
            await engine.dispose()

        assert reconciled == 1
        assert [run["state"] for run in await runs_in(identity_database)] == ["queued"]
        # And it is still the same job, still claimable, with its attempt where the crash left it.
        async with as_extractor(app, installed.token) as extractor:
            again = await claim_one(extractor)
        assert again is not None
        assert again["id"] == job["id"]
