"""The registry that maps operation kinds to the code that runs them.

Kept separate from the runner so that adding a capability is adding a handler, and so the
worker's dependencies are visible in one place rather than discovered by import side effects.

Handlers are built *with* the settings they need rather than reaching for globals, which
keeps them testable against a temporary directory instead of `/var/lib`.

`instance.heartbeat` is a periodic no-op that proves the layer works end to end on a real
instance; `maintenance.janitor` collects the debris a crash-only system leaks by design
(12 § debris & the janitor); `workspace.provision` turns a requested workspace into a real
directory tree (ADR-0018); `workspace.scan` walks a tree and registers what is in it
(ADR-0019). Every later feature adds its kind here.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from store_everything import files, janitor, operations, scanning, workspaces
from store_everything.config import Settings
from store_everything.runner import Handler, Job

_logger = logging.getLogger(__name__)

#: A recurring, effect-free operation. It exists so that a fresh instance exercises the whole
#: path — claim, lease, transition, re-arm — instead of proving it only in tests.
HEARTBEAT = "instance.heartbeat"

HEARTBEAT_INTERVAL = timedelta(minutes=15)


#: The kinds that must always have a pending run, re-asserted on every start-up. A
#: deliberately separate list from `registry`: most kinds are enqueued by the change that
#: needs them (a workspace being created, a file being uploaded) and having no pending row is
#: their normal state.
SCHEDULED_KINDS = (HEARTBEAT, janitor.KIND)


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


def registry(settings: Settings) -> dict[str, Handler]:
    """Every operation kind this build can execute, bound to what it needs."""

    async def sweep(job: Job) -> dict[str, Any]:
        # The reference source exists as of the version write path (F-007): every digest a
        # restorable version still points at is off limits, and the janitor refuses to collect
        # blobs at all without one — `versions/` holds the only copy of a superseded original.
        return await janitor.collect(job, settings=settings, references=files.restorable_digests)

    async def walk(job: Job) -> dict[str, Any]:
        # A workspace's root and cadence are on its row; the blob store's root is not, and
        # reconciliation has to ask it whether a superseded version's bytes are held.
        return await scanning.scan(job, settings=settings)

    return {
        HEARTBEAT: instance_heartbeat,
        janitor.KIND: sweep,
        # Provisioning needs no settings: a workspace's root is on its row.
        workspaces.KIND: workspaces.provision,
        scanning.KIND: walk,
    }


async def install_schedules(engine: AsyncEngine, settings: Settings) -> None:
    """Make sure the recurring operations exist. Safe to call on every start-up.

    Idempotent by the same mechanism features use: a singleton idempotency key over pending
    rows, so racing workers converge on one row instead of queueing a run each.
    """
    async with engine.connect() as connection:
        for kind in SCHEDULED_KINDS:
            await operations.ensure_scheduled(
                connection,
                kind=kind,
                max_attempts=3,
                priority=operations.PRIORITY_HEAVY,
            )
        # Per-workspace schedules cannot be a fixed list: they are asserted over the
        # workspaces that exist, which is also how a chain broken by a dead-letter is
        # restored (12 § operation inventory).
        armed = await scanning.ensure_all_scheduled(connection)
        await connection.commit()
    if armed:
        _logger.info("scan schedules asserted", extra={"workspaces": armed})
