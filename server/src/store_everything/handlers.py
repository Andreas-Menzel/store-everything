"""The registry that maps operation kinds to the code that runs them.

Kept separate from the runner so that adding a capability is adding a handler, and so the
worker's dependencies are visible in one place rather than discovered by import side effects.

It is deliberately near-empty in this change: the operation layer arrives before the
operations do, because every later feature is specified to ride it (ADR-0013) and retrofitting
a substrate under working features is how substrates end up bypassed. The `heartbeat` kind is
the exception that proves the layer works end to end — a periodic no-op that claims, runs,
re-arms its own schedule, and shows up in the queue-depth counters.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from store_everything import operations
from store_everything.config import Settings
from store_everything.runner import Handler, Job

#: A recurring, effect-free operation. It exists so that a fresh instance exercises the whole
#: path — claim, lease, transition, re-arm — instead of proving it only in tests.
HEARTBEAT = "instance.heartbeat"

HEARTBEAT_INTERVAL = timedelta(minutes=15)


async def instance_heartbeat(job: Job) -> dict[str, Any]:
    """Re-arm the schedule and report the queue's shape.

    The successor is enqueued in the transaction that completes this run, so the chain
    cannot break between the two — and `ensure_scheduled` is the floor under it if a run
    ever dead-letters and stops re-arming.
    """
    depth = await operations.count_by_state(job.connection)
    await operations.ensure_scheduled(
        job.connection,
        kind=HEARTBEAT,
        max_attempts=3,
        due_in=HEARTBEAT_INTERVAL,
        priority=operations.PRIORITY_HEAVY,
    )
    return {"queue_depth": depth}


def registry() -> dict[str, Handler]:
    """Every operation kind this build can execute."""
    return {HEARTBEAT: instance_heartbeat}


async def install_schedules(engine: AsyncEngine, settings: Settings) -> None:
    """Make sure the recurring operations exist. Safe to call on every start-up.

    Idempotent by the same mechanism features use: a singleton idempotency key over pending
    rows, so racing workers converge on one row instead of queueing a run each.
    """
    async with engine.connect() as connection:
        await operations.ensure_scheduled(
            connection,
            kind=HEARTBEAT,
            max_attempts=3,
            priority=operations.PRIORITY_HEAVY,
        )
        await connection.commit()
