"""The worker loop: does a handler's outcome become the operation's state, and nothing else?

The interesting cases are the unhappy ones. A handler that raises must not leave its partial
writes behind; a handler that is cancelled must not be recorded as succeeded; a worker that
loses its lease mid-flight must not overwrite the successor's work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
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


# ---------------------------------------------------------------- supervision of the loop
#
# Everything above asks what a *handler's* outcome does to its operation. These ask the
# opposite question — what a fault in the *worker* does to work that was fine — because the
# answers used to be wrong in ways no handler test could see: a cancelled runner buried live
# operations in a terminal state, one dropped connection stopped every claim loop in the
# process, and a single failed heartbeat silently ended the only channel that could stop a
# job (review findings A4, A5).


async def _finishes_within(task: asyncio.Task[Any], *, seconds: float) -> bool:
    """Whether a task ends on its own — without awaiting it, so a stuck one is reportable."""
    for _ in range(int(seconds * 10)):
        if task.done():
            return True
        await asyncio.sleep(0.1)
    return False


async def _stubborn(job: Job, started: asyncio.Event) -> None:
    """A handler with the one bug the worker cannot fix: it swallows its own cancellation.

    Bounded at two rounds, so the task cannot outlive the test that created it — an unbounded
    one hangs the event loop's own shutdown, which is the very failure mode being tested.
    """
    started.set()
    for _ in range(2):
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(1)


async def test_cancelling_the_runner_does_not_mark_its_operation_cancelled(
    engine: AsyncEngine, identity_database: str
) -> None:
    """A shutdown, or a sibling task failing the group, must not bury live work.

    `cancelled` is terminal: nothing retries it, so a partial failure that lands there is
    strictly worse than `kill -9`, which leaves the operation reclaimable. The claim is handed
    back instead, and the handler is actually stopped rather than left running against a
    connection about to close.
    """
    queued = await enqueue(engine)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.set()
            raise

    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=5)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    await asyncio.wait_for(stopped.wait(), timeout=5)
    assert await state_of(engine, queued) == "queued"


async def test_a_claim_loop_survives_a_database_blip(
    engine: AsyncEngine, identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transient failure used to cancel every loop in the process through the TaskGroup."""
    queued = await enqueue(engine)
    done = asyncio.Event()

    async def handler(job: Job) -> None:
        done.set()

    runner = Runner(engine, settings_for(identity_database, worker_concurrency=2), {KIND: handler})

    attempts = 0
    real_run_once = runner.run_once

    async def flaky_run_once() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OperationalError("SELECT 1", None, Exception("connection closed"))
        return await real_run_once()

    monkeypatch.setattr(runner, "run_once", flaky_run_once)

    loop = asyncio.create_task(runner.run_forever())
    await asyncio.wait_for(done.wait(), timeout=20)
    runner.stop()
    await asyncio.wait_for(loop, timeout=5)

    assert attempts > 2
    assert await state_of(engine, queued) == "succeeded"


async def test_a_failed_heartbeat_does_not_kill_the_cancellation_channel(
    engine: AsyncEngine, identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease-keeper is the only way to stop a running job, so it may not die of a blip.

    Without the retry the keeper ended on the first error, leaving a zombie: nothing renewing
    the lease, and a cancel request that never arrives however long the handler runs.
    """
    queued = await enqueue(engine)
    started = asyncio.Event()
    beats = 0
    real_heartbeat = operations.heartbeat

    async def flaky_heartbeat(*args: Any, **kwargs: Any) -> operations.Heartbeat:
        nonlocal beats
        beats += 1
        if beats == 1:
            raise OperationalError("UPDATE operation", None, Exception("connection closed"))
        return await real_heartbeat(*args, **kwargs)

    async def handler(job: Job) -> None:
        started.set()
        await asyncio.sleep(30)  # only a working cancellation channel ends this

    monkeypatch.setattr(operations, "heartbeat", flaky_heartbeat)
    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=5)

    async with engine.connect() as connection:
        assert await operations.request_cancel(connection, operation_id=queued.id)
        await connection.commit()

    assert await asyncio.wait_for(running, timeout=15)
    assert beats > 1, "the keeper stopped at the first failed heartbeat"
    assert await state_of(engine, queued) == "cancelled"


async def test_a_lease_that_cannot_be_renewed_stops_the_work_and_frees_the_claim(
    engine: AsyncEngine, identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the window really does elapse, the lease is gone — but nobody asked to cancel.

    So the work stops (a worker without a lease has nothing to write with) and the operation
    goes back to `queued` rather than to the terminal `cancelled` that would bury it.
    """
    queued = await enqueue(engine)
    started = asyncio.Event()

    async def dead_heartbeat(*args: Any, **kwargs: Any) -> operations.Heartbeat:
        raise OperationalError("UPDATE operation", None, Exception("connection closed"))

    async def handler(job: Job) -> None:
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(operations, "heartbeat", dead_heartbeat)
    settings = settings_for(identity_database, lease_seconds=2, heartbeat_seconds=1)
    runner = Runner(engine, settings, {KIND: handler})
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=5)

    assert await asyncio.wait_for(running, timeout=15)
    assert await state_of(engine, queued) == "queued"


async def test_a_swallowed_cancellation_still_stops_the_worker(
    engine: AsyncEngine, identity_database: str
) -> None:
    """The loop re-asserts a cancellation its handler absorbed, rather than claiming more.

    Otherwise a shutdown waits forever on a worker that was told to stop, finished the job it
    was on, and cheerfully went looking for the next one.
    """
    await enqueue(engine)
    started = asyncio.Event()

    runner = Runner(
        engine,
        settings_for(identity_database),
        {KIND: lambda job: _stubborn(job, started)},
    )
    loop = asyncio.create_task(runner.run_forever())
    await asyncio.wait_for(started.wait(), timeout=5)

    loop.cancel()
    # Polled rather than awaited: a loop that ignored the cancellation cannot be awaited at
    # all (asyncio drops a second `cancel()` on the floor), and a regression here has to read
    # as a failure rather than as a hung suite.
    ended = await _finishes_within(loop, seconds=6)
    runner.stop()  # so a loop that ignored it still ends, and the fixtures can tear down
    with contextlib.suppress(asyncio.CancelledError, TimeoutError, ExceptionGroup):
        await asyncio.wait_for(loop, timeout=5)

    assert ended, "the worker kept claiming work after it was cancelled"


async def test_a_release_that_fails_still_lets_the_shutdown_through(
    engine: AsyncEngine, identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handing the claim back is best effort — the lease expiring is the documented fallback.

    What must not happen is the cleanup failure swallowing the cancellation it was cleaning
    up after: the operation stays `running` until its lease lapses, and the worker still stops.
    """
    queued = await enqueue(engine)
    started = asyncio.Event()

    async def handler(job: Job) -> None:
        started.set()
        await asyncio.sleep(30)

    async def broken_release(*args: Any, **kwargs: Any) -> bool:
        raise OperationalError("UPDATE operation", None, Exception("connection closed"))

    monkeypatch.setattr(operations, "release", broken_release)
    runner = Runner(engine, settings_for(identity_database), {KIND: handler})
    running = asyncio.create_task(runner.run_once())
    await asyncio.wait_for(started.wait(), timeout=5)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=5)

    assert await state_of(engine, queued) == "running"  # reclaimed when the lease lapses


async def test_a_keeper_that_fails_is_reported_and_loses_no_work(
    engine: AsyncEngine,
    identity_database: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bug in the lease-keeper must surface in the log, not in the operation's outcome."""
    queued = await enqueue(engine)

    async def exploding_keeper(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("keeper is broken")

    async def handler(job: Job) -> dict[str, Any]:
        return {"done": True}

    monkeypatch.setattr(Runner, "_keep_lease", exploding_keeper)
    runner = Runner(engine, settings_for(identity_database), {KIND: handler})

    with caplog.at_level(logging.WARNING, logger="store_everything.runner"):
        assert await runner.run_once()

    assert await state_of(engine, queued) == "succeeded"
    assert any("lease keeper failed" in record.message for record in caplog.records)


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
