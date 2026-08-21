"""The operation layer's guarantees, against a real database.

These are the tests that make ADR-0013's "we own it" affordable: every property the rest of
the system leans on — claim exclusivity, fencing, attempts-on-claim, convergent enqueue,
recovery through lease expiry — is asserted here rather than assumed by the features that
ride on it.

A real PostgreSQL is not optional for this module. `FOR UPDATE SKIP LOCKED`, partial unique
indexes and `now()` semantics *are* the design; a fake would only prove that the fake agrees
with itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from store_everything import events, operations
from store_everything.operations import Operation
from store_everything.tables import operation
from tests.identity_helpers import read_events

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LEASE = timedelta(minutes=5)
WORKER = "test-worker/1"


@pytest_asyncio.fixture
async def engine(identity_database: str) -> AsyncIterator[AsyncEngine]:
    made = create_async_engine(identity_database)
    try:
        yield made
    finally:
        await made.dispose()


async def enqueue(connection: AsyncConnection, **overrides: Any) -> Operation:
    values: dict[str, Any] = {"kind": "test.work", "max_attempts": 4}
    values.update(overrides)
    queued = await operations.enqueue(connection, **values)
    await connection.commit()
    return queued


async def claim(connection: AsyncConnection, *, worker: str = WORKER) -> Operation | None:
    taken = await operations.claim(connection, worker=worker, lease=LEASE, kinds=("test.work",))
    await connection.commit()
    return taken


async def state_of(connection: AsyncConnection, queued: Operation) -> str:
    found = await operations.get(connection, queued.id)
    assert found is not None
    return found.state


async def expire_lease(connection: AsyncConnection, queued: Operation) -> None:
    """Age the lease out, the way a dead worker would."""
    await connection.execute(
        update(operation)
        .where(operation.c.id == queued.id)
        .values(lease_expires_at=text("now() - interval '1 minute'"))
    )
    await connection.commit()


# ------------------------------------------------------------------ enqueue and claim


async def test_enqueued_work_starts_queued_and_due(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        queued = await enqueue(connection, payload={"path": "some/relative/path"}, priority=1)

        assert queued.state == "queued"
        assert queued.attempt == 0
        assert queued.payload == {"path": "some/relative/path"}
        assert await claim(connection) is not None


async def test_a_claim_counts_an_attempt_and_takes_the_lease(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await enqueue(connection)

        taken = await claim(connection)

        assert taken is not None
        assert taken.state == "running"
        assert taken.attempt == 1
        leased = (
            await connection.execute(
                select(operation.c.leased_by, operation.c.lease_expires_at).where(
                    operation.c.id == taken.id
                )
            )
        ).one()
        assert leased[0] == WORKER
        assert leased[1] is not None


async def test_nothing_is_claimable_twice(engine: AsyncEngine) -> None:
    """The property everything else depends on: one operation, one live owner."""
    async with engine.connect() as connection:
        await enqueue(connection)

        first = await claim(connection)
        second = await claim(connection, worker="other/2")

        assert first is not None
        assert second is None


async def test_an_idle_queue_yields_nothing_and_writes_nothing(engine: AsyncEngine) -> None:
    """An idle instance must not keep the disk busy (ADR-0013's named pitfall).

    Asserted through the transaction id, not the WAL position: PostgreSQL assigns an id
    lazily, on a transaction's first *write*, so "no id was assigned" is exactly the claim
    being made. The WAL position would have measured the whole cluster — autovacuum and
    every other test database included — and called that this code's doing.
    """
    async with engine.connect() as connection:
        for _ in range(5):
            assert (
                await operations.claim(connection, worker=WORKER, lease=LEASE, kinds=("test.work",))
                is None
            )

        assigned = (
            await connection.execute(text("SELECT pg_current_xact_id_if_assigned()"))
        ).scalar_one()
        assert assigned is None, "claiming from an empty queue wrote something"


async def test_work_that_is_not_due_yet_is_not_claimed(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await enqueue(connection, due_in=timedelta(hours=1))

        assert await claim(connection) is None


async def test_higher_priority_runs_first(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await enqueue(connection, priority=operations.PRIORITY_HEAVY, payload={"n": "heavy"})
        await enqueue(
            connection, priority=operations.PRIORITY_INTERACTIVE, payload={"n": "interactive"}
        )

        first = await claim(connection)
        second = await claim(connection, worker="other/2")

        assert first is not None and first.payload == {"n": "interactive"}
        assert second is not None and second.payload == {"n": "heavy"}


async def test_only_the_requested_kinds_are_claimed(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await enqueue(connection, kind="other.work")

        assert await claim(connection) is None


async def test_concurrent_claims_never_hand_out_the_same_operation(engine: AsyncEngine) -> None:
    """Ten workers, five operations: exactly five claims, all distinct."""
    async with engine.connect() as connection:
        for index in range(5):
            await enqueue(connection, payload={"index": index})

    async def take(worker: int) -> Operation | None:
        async with engine.connect() as connection:
            return await claim(connection, worker=f"worker/{worker}")

    taken = await asyncio.gather(*(take(worker) for worker in range(10)))
    claimed = [item for item in taken if item is not None]

    assert len(claimed) == 5
    assert len({item.id for item in claimed}) == 5


# ------------------------------------------------------------------ convergent enqueue


async def test_re_enqueuing_pending_work_converges_on_it(engine: AsyncEngine) -> None:
    """A re-scan while a scan is pending must not queue a second scan."""
    async with engine.connect() as connection:
        first = await enqueue(connection, idempotency_key="scan:workspace-1")
        second = await enqueue(connection, idempotency_key="scan:workspace-1")

        assert first.id == second.id
        assert await operations.count_by_state(connection) == {"queued": 1}


async def test_the_key_is_free_again_once_the_work_is_done(engine: AsyncEngine) -> None:
    """Otherwise a workspace could be scanned exactly once, ever."""
    async with engine.connect() as connection:
        first = await enqueue(connection, idempotency_key="scan:workspace-1")
        taken = await claim(connection)
        assert taken is not None
        await operations.succeed(connection, claimed=taken)
        await connection.commit()

        second = await enqueue(connection, idempotency_key="scan:workspace-1")

        assert second.id != first.id


async def test_a_running_operation_still_holds_its_key(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        first = await enqueue(connection, idempotency_key="scan:workspace-1")
        await claim(connection)

        assert (await enqueue(connection, idempotency_key="scan:workspace-1")).id == first.id


# ------------------------------------------------------------------ recovery


async def test_an_expired_lease_is_reclaimable_by_anyone(engine: AsyncEngine) -> None:
    """This branch *is* the crash-recovery path — no start-up sweep exists."""
    async with engine.connect() as connection:
        await enqueue(connection)
        first = await claim(connection)
        assert first is not None

        await expire_lease(connection, first)
        second = await claim(connection, worker="successor/2")

        assert second is not None
        assert second.id == first.id
        # The attempt counted again, which is what eventually stops a poison job.
        assert second.attempt == first.attempt + 1


async def test_a_job_that_never_reports_still_dead_letters(engine: AsyncEngine) -> None:
    """Attempts count on claim, so work that kills its worker converges instead of looping."""
    async with engine.connect() as connection:
        queued = await enqueue(connection, max_attempts=2)

        for _ in range(2):
            taken = await claim(connection)
            assert taken is not None
            await expire_lease(connection, taken)

        assert await claim(connection) is None
        assert await state_of(connection, queued) == "dead_letter"


async def test_dead_lettering_is_audited(engine: AsyncEngine, identity_database: str) -> None:
    """Work the system gave up on has to be findable afterwards."""
    async with engine.connect() as connection:
        await enqueue(connection, max_attempts=1)
        taken = await claim(connection)
        assert taken is not None
        await expire_lease(connection, taken)
        await claim(connection)

    recorded = await read_events(identity_database, action=events.OPERATION_DEAD_LETTERED)

    assert len(recorded) == 1
    assert recorded[0]["details"]["kind"] == "test.work"
    assert recorded[0]["actor_type"] == "system"


async def test_releasing_a_claim_does_not_consume_an_attempt(engine: AsyncEngine) -> None:
    """The SIGTERM path: a graceful stop must not push work towards the dead-letter state."""
    async with engine.connect() as connection:
        await enqueue(connection)
        taken = await claim(connection)
        assert taken is not None

        assert await operations.release(connection, claimed=taken, worker=WORKER)
        await connection.commit()

        again = await claim(connection)
        assert again is not None
        assert again.attempt == 2  # counted on claim, both times
        assert await state_of(connection, again) == "running"


# ------------------------------------------------------------------ fencing


async def test_a_zombie_cannot_record_a_result(engine: AsyncEngine) -> None:
    """The single property that makes a lost worker harmless rather than dangerous."""
    async with engine.connect() as connection:
        await enqueue(connection)
        zombie = await claim(connection)
        assert zombie is not None

        await expire_lease(connection, zombie)
        successor = await claim(connection, worker="successor/2")
        assert successor is not None

        # The zombie wakes up and reports success with its stale attempt.
        assert not await operations.succeed(connection, claimed=zombie, result={"stale": True})
        assert await state_of(connection, successor) == "running"

        assert await operations.succeed(connection, claimed=successor, result={"fresh": True})
        await connection.commit()
        stored = (
            await connection.execute(
                select(operation.c.result).where(operation.c.id == successor.id)
            )
        ).scalar_one()
        assert stored == {"fresh": True}


async def test_a_zombie_heartbeat_is_refused(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await enqueue(connection)
        zombie = await claim(connection)
        assert zombie is not None
        await expire_lease(connection, zombie)
        await claim(connection, worker="successor/2")

        beat = await operations.heartbeat(connection, claimed=zombie, worker=WORKER, lease=LEASE)

        assert not beat.lease_extended
        # Losing the lease must read as "stop", not as "carry on".
        assert beat.cancel_requested


async def test_a_heartbeat_extends_the_lease(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await enqueue(connection)
        taken = await claim(connection)
        assert taken is not None
        before = (
            await connection.execute(
                select(operation.c.lease_expires_at).where(operation.c.id == taken.id)
            )
        ).scalar_one()

        beat = await operations.heartbeat(
            connection, claimed=taken, worker=WORKER, lease=timedelta(minutes=30)
        )
        await connection.commit()

        after = (
            await connection.execute(
                select(operation.c.lease_expires_at).where(operation.c.id == taken.id)
            )
        ).scalar_one()
        assert beat.lease_extended
        assert after > before


# ------------------------------------------------------------------ failure and retry


async def test_a_retryable_failure_goes_back_to_the_queue_with_a_delay(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        await enqueue(connection)
        taken = await claim(connection)
        assert taken is not None

        reached = await operations.fail(connection, claimed=taken, error="network hiccup")
        await connection.commit()

        assert reached == "queued"
        # Not immediately due: backoff is what stops a failing item from spinning.
        assert await claim(connection) is None


async def test_a_permanent_failure_does_not_retry(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        queued = await enqueue(connection)
        taken = await claim(connection)
        assert taken is not None

        reached = await operations.fail(
            connection, claimed=taken, error="payload is nonsense", retryable=False
        )
        await connection.commit()

        assert reached == "failed"
        assert await state_of(connection, queued) == "failed"
        assert await claim(connection) is None


async def test_the_last_attempt_dead_letters_instead_of_retrying(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        queued = await enqueue(connection, max_attempts=1)
        taken = await claim(connection)
        assert taken is not None

        reached = await operations.fail(connection, claimed=taken, error="still broken")
        await connection.commit()

        assert reached == "dead_letter"
        assert await state_of(connection, queued) == "dead_letter"


async def test_backoff_grows_and_is_jittered() -> None:
    """Jitter is load-bearing: without it, items that failed together retry together."""
    delays = [
        operations.retry_delay(attempt, base_seconds=10, max_seconds=3600).total_seconds()
        for attempt in range(1, 8)
    ]

    assert all(delay >= 10 for delay in delays)
    assert all(delay <= 3600 for delay in delays)
    assert max(delays) > min(delays)
    repeated = {
        operations.retry_delay(5, base_seconds=10, max_seconds=3600).total_seconds()
        for _ in range(20)
    }
    assert len(repeated) > 1


# ------------------------------------------------------------------ cancellation


async def test_cancelling_pending_work_ends_it_outright(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        queued = await enqueue(connection)

        assert await operations.request_cancel(connection, operation_id=queued.id)
        await connection.commit()

        assert await state_of(connection, queued) == "cancelled"
        assert await claim(connection) is None


async def test_cancelling_running_work_reaches_it_through_the_heartbeat(
    engine: AsyncEngine,
) -> None:
    """Cancellation is a durable flag, so it survives the restart of either side."""
    async with engine.connect() as connection:
        await enqueue(connection)
        taken = await claim(connection)
        assert taken is not None

        assert await operations.request_cancel(connection, operation_id=taken.id)
        await connection.commit()

        beat = await operations.heartbeat(connection, claimed=taken, worker=WORKER, lease=LEASE)
        assert beat.lease_extended
        assert beat.cancel_requested

        assert await operations.mark_cancelled(connection, claimed=taken)
        await connection.commit()
        assert await state_of(connection, taken) == "cancelled"


async def test_cancelling_something_finished_changes_nothing(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await enqueue(connection)
        taken = await claim(connection)
        assert taken is not None
        await operations.succeed(connection, claimed=taken)
        await connection.commit()

        assert not await operations.request_cancel(connection, operation_id=taken.id)
        assert await state_of(connection, taken) == "succeeded"


# ------------------------------------------------------------------ schedules


async def test_ensure_scheduled_keeps_exactly_one_pending_run(engine: AsyncEngine) -> None:
    """Safe to call on every start-up, and it restores a chain that stopped re-arming."""
    async with engine.connect() as connection:
        for _ in range(3):
            await operations.ensure_scheduled(connection, kind="janitor", max_attempts=3)
        await connection.commit()

        assert await operations.count_by_state(connection, kind="janitor") == {"queued": 1}
