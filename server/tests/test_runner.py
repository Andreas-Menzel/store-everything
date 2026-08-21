"""The worker loop: does a handler's outcome become the operation's state, and nothing else?

The interesting cases are the unhappy ones. A handler that raises must not leave its partial
writes behind; a handler that is cancelled must not be recorded as succeeded; a worker that
loses its lease mid-flight must not overwrite the successor's work.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from store_everything import handlers, operations, workspaces
from store_everything.config import Settings
from store_everything.runner import Job, PermanentFailureError, Runner
from store_everything.tables import app_user, operation
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

KIND = "test.work"


@pytest_asyncio.fixture
async def engine(identity_database: str) -> AsyncIterator[AsyncEngine]:
    made = create_async_engine(identity_database)
    try:
        yield made
    finally:
        await made.dispose()


def settings_for(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": database_url,
        # Short enough that a test can observe a heartbeat, still lease > heartbeat.
        "lease_seconds": 4,
        "heartbeat_seconds": 1,
        "worker_concurrency": 1,
        "worker_poll_seconds": 0.05,
        "retry_base_seconds": 0.01,
        "retry_max_seconds": 0.02,
    }
    values.update(overrides)
    return make_settings(**values)


async def enqueue(engine: AsyncEngine, **overrides: Any) -> operations.Operation:
    values: dict[str, Any] = {"kind": KIND, "max_attempts": 4}
    values.update(overrides)
    async with engine.connect() as connection:
        queued = await operations.enqueue(connection, **values)
        await connection.commit()
    return queued


async def state_of(engine: AsyncEngine, queued: operations.Operation) -> str:
    async with engine.connect() as connection:
        found = await operations.get(connection, queued.id)
    assert found is not None
    return found.state


async def result_of(engine: AsyncEngine, queued: operations.Operation) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(operation.c.result).where(operation.c.id == queued.id))
        ).scalar_one()


async def test_a_handler_runs_and_its_result_is_recorded(
    engine: AsyncEngine, identity_database: str
) -> None:
    queued = await enqueue(engine, payload={"double": 21})

    async def handler(job: Job) -> dict[str, Any]:
        return {"answer": job.payload["double"] * 2}

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})

    assert await runner.run_once()
    assert await state_of(engine, queued) == "succeeded"
    assert await result_of(engine, queued) == {"answer": 42}


async def test_an_idle_queue_reports_nothing_to_do(
    engine: AsyncEngine, identity_database: str
) -> None:
    async def handler(job: Job) -> None:
        raise AssertionError("must not run")

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})

    assert not await runner.run_once()


async def test_a_handlers_writes_commit_with_the_success_transition(
    engine: AsyncEngine, identity_database: str
) -> None:
    """The property the whole layer exists for: effects and state are one fact."""
    queued = await enqueue(engine)

    async def handler(job: Job) -> None:
        from store_everything import identity
        from store_everything.events import Actor

        await identity.create_user(
            job.connection,
            email="made-by-handler@example.com",
            display_name="Handler",
            password="a-long-enough-password",
            role="member",
            actor=Actor.system(),
        )

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    assert await runner.run_once()

    assert await state_of(engine, queued) == "succeeded"
    async with engine.connect() as connection:
        made = (
            await connection.execute(
                select(func.count())
                .select_from(app_user)
                .where(app_user.c.email == "made-by-handler@example.com")
            )
        ).scalar_one()
    assert made == 1


async def test_a_failing_handler_leaves_no_partial_writes(
    engine: AsyncEngine, identity_database: str
) -> None:
    """A half-applied operation is worse than a retried one."""
    queued = await enqueue(engine)

    async def handler(job: Job) -> None:
        from store_everything import identity
        from store_everything.events import Actor

        await identity.create_user(
            job.connection,
            email="never-committed@example.com",
            display_name="Ghost",
            password="a-long-enough-password",
            role="member",
            actor=Actor.system(),
        )
        raise RuntimeError("failed after writing")

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    assert await runner.run_once()

    assert await state_of(engine, queued) == "queued"  # awaiting retry
    async with engine.connect() as connection:
        leaked = (
            await connection.execute(
                select(func.count())
                .select_from(app_user)
                .where(app_user.c.email == "never-committed@example.com")
            )
        ).scalar_one()
    assert leaked == 0


async def test_a_permanent_failure_is_not_retried(
    engine: AsyncEngine, identity_database: str
) -> None:
    queued = await enqueue(engine)

    async def handler(job: Job) -> None:
        raise PermanentFailureError("this payload can never work")

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    assert await runner.run_once()

    assert await state_of(engine, queued) == "failed"


async def test_a_cancelled_operation_stops_and_is_recorded_as_cancelled(
    engine: AsyncEngine, identity_database: str
) -> None:
    """Cancellation travels through the heartbeat, which is why it survives restarts."""
    queued = await enqueue(engine)
    started = asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await asyncio.sleep(30)  # cancelled long before this returns

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    work = asyncio.create_task(runner.run_once())

    await asyncio.wait_for(started.wait(), timeout=5)
    async with engine.connect() as connection:
        assert await operations.request_cancel(connection, operation_id=queued.id)
        await connection.commit()

    assert await asyncio.wait_for(work, timeout=10)
    assert await state_of(engine, queued) == "cancelled"


async def test_losing_the_lease_stops_the_work_without_clobbering_the_successor(
    engine: AsyncEngine, identity_database: str
) -> None:
    """A zombie must abort, and its late success must not overwrite the re-run."""
    queued = await enqueue(engine)
    started = asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await asyncio.sleep(30)

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    work = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=5)

    # Somebody else reclaims the expired lease while this worker is still busy.
    async with engine.connect() as connection:
        await connection.execute(
            update(operation)
            .where(operation.c.id == queued.id)
            .values(lease_expires_at=text("now() - interval '1 minute'"))
        )
        await connection.commit()
        successor = await operations.claim(
            connection, worker="successor/2", lease=timedelta(minutes=5)
        )
        await connection.commit()
    assert successor is not None

    assert await asyncio.wait_for(work, timeout=10)
    # The successor still owns it: the zombie's transition was fenced out.
    assert await state_of(engine, queued) == "running"


async def test_stopping_ends_the_loop(engine: AsyncEngine, identity_database: str) -> None:
    async def handler(job: Job) -> None:
        return None

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    loop = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.2)

    runner.stop()

    await asyncio.wait_for(loop, timeout=5)


async def test_the_loop_drains_a_backlog(engine: AsyncEngine, identity_database: str) -> None:
    for index in range(5):
        await enqueue(engine, payload={"index": index})
    done: list[int] = []
    drained = asyncio.Event()

    async def handler(job: Job) -> None:
        done.append(job.payload["index"])
        if len(done) == 5:
            drained.set()

    runner = Runner(engine, settings_for(identity_database, worker_concurrency=3), {KIND: handler})
    loop = asyncio.create_task(runner.run_forever())
    await asyncio.wait_for(drained.wait(), timeout=20)
    runner.stop()
    await asyncio.wait_for(loop, timeout=5)

    assert sorted(done) == [0, 1, 2, 3, 4]
    async with engine.connect() as connection:
        assert await operations.count_by_state(connection, kind=KIND) == {"succeeded": 5}


async def test_released_claims_do_not_burn_attempts(
    engine: AsyncEngine, identity_database: str
) -> None:
    queued = await enqueue(engine)
    async with engine.connect() as connection:
        taken = await operations.claim(connection, worker="worker/1", lease=timedelta(minutes=5))
        await connection.commit()
    assert taken is not None

    async def handler(job: Job) -> None:
        return None

    runner = Runner(engine, settings_for(identity_database), {KIND: handler}, worker="worker/1")
    await runner.release_all([taken])

    assert await state_of(engine, queued) == "queued"


# ------------------------------------------------------------------ the shipped handler


async def test_the_instance_heartbeat_re_arms_its_own_schedule(
    engine: AsyncEngine, identity_database: str
) -> None:
    """Periodic work chains itself; `ensure_scheduled` is only the floor under the chain.

    Only this schedule is installed. Several kinds queued in one transaction share a
    `next_due_at` to the microsecond, so which one a single claim picks is arbitrary — a test
    that installed all of them would pass or fail on that coin flip.
    """
    settings = settings_for(identity_database)
    async with engine.connect() as connection:
        await operations.ensure_scheduled(connection, kind=handlers.HEARTBEAT, max_attempts=3)
        await connection.commit()

    runner = Runner(engine, settings, handlers.registry(settings))
    assert await runner.run_once()

    async with engine.connect() as connection:
        depth = await operations.count_by_state(connection, kind=handlers.HEARTBEAT)
    # One finished run, and exactly one pending successor.
    assert depth == {"succeeded": 1, "queued": 1}


async def test_installing_schedules_twice_leaves_one_pending_run_per_kind(
    engine: AsyncEngine, identity_database: str
) -> None:
    """Start-up re-asserts the schedules, so this happens on every restart."""
    settings = settings_for(identity_database)

    await handlers.install_schedules(engine, settings)
    await handlers.install_schedules(engine, settings)

    async with engine.connect() as connection:
        for kind in handlers.SCHEDULED_KINDS:
            assert await operations.count_by_state(connection, kind=kind) == {"queued": 1}, kind
        # An on-demand kind has no standing schedule, and must not acquire one here.
        assert await operations.count_by_state(connection, kind=workspaces.KIND) == {}


# ------------------------------------------------------- starting before the schema exists


async def test_the_worker_waits_for_a_pending_migration(fresh_database: str) -> None:
    """A fresh install starts the stack before migrations run; that must not crash a worker.

    `fresh_database` is deliberately *not* migrated: this is the state an operator is in
    between `docker compose up` and `docker compose run --rm migrations`.
    """
    engine = create_async_engine(fresh_database)
    try:
        runner = Runner(engine, settings_for(fresh_database), {})
        waiting = asyncio.create_task(runner.wait_until_ready())
        await asyncio.sleep(0.3)

        assert not waiting.done(), "the worker should still be waiting, not failed or ready"

        runner.stop()
        assert await asyncio.wait_for(waiting, timeout=5) is False
    finally:
        await engine.dispose()


async def test_the_worker_starts_once_the_schema_is_current(
    engine: AsyncEngine, identity_database: str
) -> None:
    runner = Runner(engine, settings_for(identity_database), {})

    assert await asyncio.wait_for(runner.wait_until_ready(), timeout=10) is True


async def test_the_worker_waits_for_an_unreachable_database() -> None:
    """The other ordinary startup race: the worker up before PostgreSQL accepts connections."""
    from tests.conftest import UNREACHABLE_DATABASE_URL

    engine = create_async_engine(UNREACHABLE_DATABASE_URL)
    try:
        runner = Runner(engine, settings_for(UNREACHABLE_DATABASE_URL), {})
        waiting = asyncio.create_task(runner.wait_until_ready())
        await asyncio.sleep(0.3)

        assert not waiting.done()

        runner.stop()
        assert await asyncio.wait_for(waiting, timeout=5) is False
    finally:
        await engine.dispose()
