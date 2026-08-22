"""What a folder's subtree adds up to — kept current by a queue, never by a lock on the root.

[F-015/FR-8](../../../features/F-015-folders.md) asks for three numbers per folder: a direct file
count, a recursive file count, and a recursive size. They are produced three different ways, on
purpose.

**The direct count is not an aggregate at all.** It is one indexed count over `file`, so it is
computed at read time and is always exact. Nothing to maintain, nothing to drift.

**The recursive pair rides a queue.** Every change writes a `folder_delta` row on the same
connection, in the same transaction as the change — the [events](events.py) pattern applied to
arithmetic instead of audit. A rollup run later claims a batch and adds it to the folders it
belongs to. Doing it synchronously was rejected in FR-8 for a specific reason: every upload
anywhere in a workspace would have to update the workspace root's row, so an import would
serialise itself on one row.

**Addition commutes, and everything simple here follows from that.** The queue needs no cursor,
no ordering and no exactly-once delivery: a batch is deleted and added in *one statement*, so a
crash re-applies nothing and loses nothing, and two drains can never see the same row. That is
also why this is a second outbox rather than a fourth consumer of the event log — a cursor over
the log can be overtaken by a transaction that commits behind it, and a delta skipped that way
is a number that stays wrong until the next drift sweep notices.

**One rule keeps the closure honest.** A delta names a folder, and the drain expands it over that
folder's ancestors *as they stand at drain time*. A folder move rewrites exactly that. So a drain
and a folder move are **mutually exclusive per workspace** (`lock`), and a move reads the moved
subtree's total from `folder_aggregate` rather than from ground truth. Both orders are then
correct:

- drain first → the cached total already includes the delta → the move shifts it along;
- move first → the delta expands over the new closure → it lands where the file now lives.

There is no third case, because a move cannot begin in the middle of a drain. Reading ground
truth in the move would double-count anything still queued, which is the trap: the more careful
looking option is the wrong one.

Uploads, scans and file moves never take that lock. They insert a row and move on, which is what
keeps FR-8's promise about import throughput.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Select,
    and_,
    any_,
    delete,
    func,
    insert,
    literal,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import operations
from store_everything.faults import fault_point
from store_everything.runner import Job, PermanentFailureError
from store_everything.tables import (
    file,
    file_version,
    folder,
    folder_aggregate,
    folder_closure,
    folder_delta,
    workspace,
)

_logger = logging.getLogger(__name__)

KIND = "workspace.rollup"

#: Deltas claimed per statement. Large because the work per row is one integer addition and the
#: coalescing happens in the `GROUP BY`: a thousand uploads into one folder become one update of
#: that folder's row, not a thousand.
BATCH = 1000

#: Batches per run. A cap rather than "until empty" so one run cannot be held open forever by a
#: writer that keeps feeding it — it re-arms itself instead, and the lock is released between
#: batches so a move waiting behind it gets its turn.
BATCHES_PER_RUN = 50

#: Folders whose numbers are checked against ground truth per run. Small: this is a rotating
#: sweep, not an audit, and the point is that every folder comes around eventually.
VERIFY_BATCH = 20

MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Totals:
    """A folder's three numbers, and how much to trust the last two."""

    direct_files: int
    total_files: int
    total_bytes: int
    as_of: datetime
    pending: bool
    """Whether a change beneath this folder is still queued. Live at read time, so a client can
    poll until it is false instead of guessing at the window."""


@dataclass(frozen=True, slots=True)
class Drift:
    """A folder whose stored numbers disagreed with ground truth, and by how much."""

    folder_id: UUID
    stored: tuple[int, int]
    truth: tuple[int, int]


def _lock_key(workspace_id: UUID) -> int:
    """A `bigint` for `pg_advisory_xact_lock`, from the second half of the id.

    The *second* half deliberately: ids are UUIDv7, so the first eight bytes are a millisecond
    timestamp and a counter, and two workspaces created in the same millisecond would collide
    often. The trailing eight are random. A collision would only over-serialise two unrelated
    workspaces, never corrupt anything, but it is free to avoid.
    """
    return int.from_bytes(workspace_id.bytes[8:], "big", signed=True)


async def lock(connection: AsyncConnection, *workspace_ids: UUID) -> None:
    """Hold these workspaces' rollups still until this transaction ends.

    Taken by the drain and by a folder move — the only two places where the closure and the
    aggregates have to agree — and by nothing on the upload path. Transaction-scoped, so it is
    released by the commit rather than by remembering to.

    Sorted, because a cross-workspace move takes two: any fixed order between them is enough to
    make a deadlock impossible, and there is no reason for it to be a clever one.
    """
    for workspace_id in sorted(set(workspace_ids)):
        await connection.execute(select(func.pg_advisory_xact_lock(_lock_key(workspace_id))))


# --------------------------------------------------------------------------- writing deltas


async def record(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    folder_id: UUID,
    files: int = 0,
    size_bytes: int = 0,
) -> None:
    """Queue an amount to add to this folder **and to every one of its ancestors**.

    Called from the transaction that made the change, so the two commit together or neither does.
    A file move is two of these — `-1` at the old folder, `+1` at the new one — and needs no
    common-ancestor arithmetic at all: the expansions cancel at every ancestor they share.
    """
    if files == 0 and size_bytes == 0:
        return
    await connection.execute(
        insert(folder_delta).values(
            workspace_id=workspace_id,
            folder_id=folder_id,
            file_count=files,
            size_bytes=size_bytes,
        )
    )


async def schedule(connection: AsyncConnection, workspace_id: UUID) -> None:
    """Ask for a rollup of this workspace, once per transaction that changed something.

    Converges on the one pending row per workspace, so a thousand uploads arm one run rather
    than a thousand. `ensure_scheduled` deliberately does *not* converge with a **running** run:
    that run may already have claimed its batch, and a delta written after that has to be somebody
    else's work.

    Forgetting this call costs latency and never correctness — the delta is durable, and the
    janitor arms every workspace hourly as the floor under exactly this.
    """
    await operations.ensure_scheduled(
        connection,
        kind=KIND,
        max_attempts=MAX_ATTEMPTS,
        priority=operations.PRIORITY_PRESENCE,
        subject_type="workspace",
        subject_id=workspace_id,
    )


async def initialise(connection: AsyncConnection, folder_id: UUID) -> None:
    """Give a new folder its aggregate row. Empty, and exactly right: it holds nothing yet.

    `verified_at` stays NULL, which is what puts a brand-new folder at the front of the rotating
    sweep's queue.
    """
    await connection.execute(
        pg_insert(folder_aggregate)
        .values(folder_id=folder_id)
        .on_conflict_do_nothing(index_elements=[folder_aggregate.c.folder_id])
    )


async def stored(connection: AsyncConnection, folder_id: UUID) -> tuple[int, int]:
    """This subtree's totals **as the aggregates currently say** — files, bytes.

    What a folder move shifts between the two parent chains. Deliberately not ground truth: a
    delta still in the queue is not in this number *and* is not in the old parent's number, and
    it will expand over the closure the move is about to rewrite. Ground truth would count it
    twice.
    """
    row = (
        await connection.execute(
            select(folder_aggregate.c.total_files, folder_aggregate.c.total_bytes).where(
                folder_aggregate.c.folder_id == folder_id
            )
        )
    ).first()
    return (0, 0) if row is None else (row[0], row[1])


async def shift(
    connection: AsyncConnection,
    *,
    folder_id: UUID,
    from_parent: UUID,
    from_workspace: UUID,
    to_parent: UUID,
    to_workspace: UUID,
) -> None:
    """Move a subtree's totals from one parent chain to another. O(depth), as FR-8 requires.

    The caller holds the lock and has already rewritten the closure. Two delta rows do the whole
    job: the expansion stops at each parent's own chain, so the moved subtree's own numbers are
    untouched and the folders above the common ancestor see `+n` and `-n` cancel.

    A cross-workspace move also **re-tags the deltas still queued for the moved subtree**. They
    were filed under the source workspace, but the folders they name now belong to the
    destination, and a drain must only ever update aggregates in the workspace whose lock it
    holds.
    """
    files, size_bytes = await stored(connection, folder_id)

    if from_workspace != to_workspace:
        await connection.execute(
            update(folder_delta)
            .where(
                folder_delta.c.workspace_id == from_workspace,
                folder_delta.c.folder_id.in_(_subtree(folder_id)),
            )
            .values(workspace_id=to_workspace)
        )

    # Both parents are real folders: only the workspace root has none, and it cannot be moved
    # and cannot stop being a root (F-015/FR-1).
    await record(
        connection,
        workspace_id=from_workspace,
        folder_id=from_parent,
        files=-files,
        size_bytes=-size_bytes,
    )
    await record(
        connection,
        workspace_id=to_workspace,
        folder_id=to_parent,
        files=files,
        size_bytes=size_bytes,
    )


def _subtree(folder_id: UUID) -> Select[tuple[UUID]]:
    """Every folder at or under this one."""
    return select(folder_closure.c.descendant_id).where(folder_closure.c.ancestor_id == folder_id)


# --------------------------------------------------------------------------- draining


def _drain_statement(workspace_id: UUID, *, limit: int) -> Any:
    """Claim a batch and apply it. One statement, because it has to be one transaction.

    `DELETE … RETURNING` feeding the update means the rows are gone and the numbers are up in the
    same commit: a crash between the two is not a state this can reach. `SKIP LOCKED` is belt to
    the lock's braces — drains of one workspace are already serialised.

    It has to be a single `UPDATE`, too. Every folder is its own ancestor at depth 0, so two
    data-modifying CTEs against `folder_aggregate` would always overlap, and PostgreSQL would
    silently drop the second write to a row the first had already touched.
    """
    claimable = (
        select(folder_delta.c.id)
        .where(folder_delta.c.workspace_id == workspace_id)
        .order_by(folder_delta.c.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = (
        delete(folder_delta)
        .where(folder_delta.c.id.in_(claimable))
        .returning(
            folder_delta.c.folder_id,
            folder_delta.c.file_count,
            folder_delta.c.size_bytes,
        )
        .cte("claimed")
    )
    # The closure join is the ancestor expansion and the `GROUP BY` is FR-8's coalescing: one row
    # per affected folder however many deltas contributed to it.
    rolled = (
        select(
            folder_closure.c.ancestor_id.label("folder_id"),
            func.sum(claimed.c.file_count).label("total_files"),
            func.sum(claimed.c.size_bytes).label("total_bytes"),
        )
        .select_from(
            claimed.join(folder_closure, folder_closure.c.descendant_id == claimed.c.folder_id)
        )
        .group_by(folder_closure.c.ancestor_id)
        .cte("rolled")
    )
    # An `UPDATE`, deliberately not an upsert. PostgreSQL evaluates a table's `CHECK` constraints
    # against the row an `INSERT … ON CONFLICT` *proposes*, before it notices the conflict — so an
    # ordinary `-1` would be refused by `total_files >= 0` even though the resolved row is a `0`.
    # Adding to the row that is there is also simply what this means. A folder whose row is
    # missing keeps its delta unapplied, and the sweep repairs it from ground truth: no row reads
    # as `verified_at IS NULL`, which is the front of that queue.
    return (
        update(folder_aggregate)
        .where(folder_aggregate.c.folder_id == rolled.c.folder_id)
        .values(
            total_files=folder_aggregate.c.total_files + rolled.c.total_files,
            total_bytes=folder_aggregate.c.total_bytes + rolled.c.total_bytes,
        )
        # The affected folders come back so the caller can count them. `rowcount` would answer for
        # an `UPDATE`, but SQLAlchemy leaves it undefined for other statements, and one rule for
        # reading a result beats two.
        .returning(folder_aggregate.c.folder_id)
    )


async def drain(connection: AsyncConnection, *, workspace_id: UUID, limit: int = BATCH) -> int:
    """Apply one batch. Returns the number of folders whose totals moved.

    Zero is the caller's signal to stop, and it means one of two things: the queue was empty, or
    what it held named folders with no aggregate row to add to. Both mean there is nothing more
    this run can usefully do — the second is a bug the sweep repairs, not something to spin on.

    `SKIP LOCKED` cannot make a non-empty queue look empty: a drain holds the workspace lock, so
    no other drain is running, and a writer's own uncommitted row is invisible here anyway.
    """
    affected = (await connection.execute(_drain_statement(workspace_id, limit=limit))).all()
    return len(affected)


# --------------------------------------------------------------------------- ground truth


async def ground_truth(
    connection: AsyncConnection, folder_ids: Sequence[UUID]
) -> dict[UUID, tuple[int, int]]:
    """Count and size these subtrees from the files themselves — files, bytes, per folder.

    The definition of the numbers, in one place: **live** files and the size of each one's
    current version. Superseded versions and trashed files are storage that
    `/stats/storage` reports under its own categories ([09](../../../specs/09-previews.md)),
    not content the folder holds.

    Used by the rotating sweep and by the tests that assert convergence; migration 0010 spells
    the same query out again for its backfill, because a migration must not depend on code that
    keeps changing.
    """
    if not folder_ids:
        return {}
    rows = await connection.execute(
        select(
            folder_closure.c.ancestor_id,
            func.count(file.c.id),
            func.coalesce(func.sum(file_version.c.size_bytes), 0),
        )
        .select_from(
            folder_closure.outerjoin(
                file,
                and_(
                    file.c.folder_id == folder_closure.c.descendant_id,
                    file.c.state == "live",
                ),
            ).outerjoin(
                file_version,
                and_(
                    file_version.c.file_id == file.c.id,
                    file_version.c.is_current.is_(True),
                ),
            )
        )
        # One array parameter rather than an `IN` list of them, the `files.stamp_seen` rule: a
        # caller asking about every folder in a large workspace must not hit PostgreSQL's bind
        # parameter limit.
        .where(
            folder_closure.c.ancestor_id
            == any_(literal(list(folder_ids), ARRAY(PGUUID(as_uuid=True))))
        )
        .group_by(folder_closure.c.ancestor_id)
    )
    return {row[0]: (row[1], row[2]) for row in rows}


def _has_pending_delta(folder_column: Any) -> Any:
    """ "Is a change beneath this folder still queued?" — driven from the closure into the queue."""
    return (
        select(literal(1))
        .select_from(
            folder_delta.join(
                folder_closure, folder_closure.c.descendant_id == folder_delta.c.folder_id
            )
        )
        .where(folder_closure.c.ancestor_id == folder_column)
        .exists()
    )


async def totals(connection: AsyncConnection, folder_id: UUID) -> Totals:
    """Everything a folder read reports about its size, in one round trip.

    The direct count is exact; the recursive pair carries `as_of` and `pending`. A folder with no
    aggregate row reports zeros rather than failing: the row is created with the folder and
    backfilled by migration 0010, so its absence is a bug for the sweep to fix, not a reason to
    refuse a read.
    """
    direct = (
        select(func.count())
        .select_from(file)
        .where(file.c.folder_id == folder_id, file.c.state == "live")
        .scalar_subquery()
    )
    row = (
        await connection.execute(
            select(
                direct,
                func.coalesce(folder_aggregate.c.total_files, 0),
                func.coalesce(folder_aggregate.c.total_bytes, 0),
                workspace.c.aggregates_as_of,
                _has_pending_delta(folder.c.id),
            )
            .select_from(
                folder.join(workspace, workspace.c.id == folder.c.workspace_id).outerjoin(
                    folder_aggregate, folder_aggregate.c.folder_id == folder.c.id
                )
            )
            .where(folder.c.id == folder_id)
        )
    ).one()
    return Totals(
        direct_files=row[0],
        total_files=row[1],
        total_bytes=row[2],
        as_of=row[3],
        pending=row[4],
    )


async def verify(
    connection: AsyncConnection, *, workspace_id: UUID, limit: int = VERIFY_BATCH
) -> list[Drift]:
    """Check a rotating batch against ground truth, correct what disagrees, and say so.

    FR-8's drift check. Least-recently-verified first, so every folder comes around and a new one
    (or one an upsert had to invent) is looked at immediately.

    Folders with a queued change beneath them are **skipped**, because for those a difference is
    lag rather than drift and "correcting" it would double-count the delta still to come. The
    caller holds the workspace lock, so the closure cannot move underneath this.

    Drift means a bug, and this repairs it: ground truth is authoritative, the folder has nothing
    pending, and serving a number known to be wrong helps nobody. The warning is the flag.
    """
    candidates = (
        await connection.execute(
            select(folder.c.id)
            .select_from(
                folder.outerjoin(folder_aggregate, folder_aggregate.c.folder_id == folder.c.id)
            )
            .where(folder.c.workspace_id == workspace_id, ~_has_pending_delta(folder.c.id))
            .order_by(folder_aggregate.c.verified_at.nullsfirst(), folder.c.id)
            .limit(limit)
        )
    ).all()
    checked = [row[0] for row in candidates]
    if not checked:
        return []

    truth = await ground_truth(connection, checked)
    current = {
        row[0]: (row[1], row[2])
        for row in await connection.execute(
            select(
                folder_aggregate.c.folder_id,
                folder_aggregate.c.total_files,
                folder_aggregate.c.total_bytes,
            ).where(
                folder_aggregate.c.folder_id == any_(literal(checked, ARRAY(PGUUID(as_uuid=True))))
            )
        )
    }

    drifted: list[Drift] = []
    for folder_id in checked:
        expected = truth.get(folder_id, (0, 0))
        held = current.get(folder_id)
        if held == expected:
            continue
        drifted.append(Drift(folder_id, held or (0, 0), expected))
        await connection.execute(
            pg_insert(folder_aggregate)
            .values(folder_id=folder_id, total_files=expected[0], total_bytes=expected[1])
            .on_conflict_do_update(
                index_elements=[folder_aggregate.c.folder_id],
                set_={"total_files": expected[0], "total_bytes": expected[1]},
            )
        )
        _logger.warning(
            "folder aggregate drifted from ground truth and was corrected",
            extra={
                "folder": str(folder_id),
                "workspace": str(workspace_id),
                "stored": held,
                "truth": expected,
            },
        )

    await connection.execute(
        update(folder_aggregate)
        .where(folder_aggregate.c.folder_id == any_(literal(checked, ARRAY(PGUUID(as_uuid=True)))))
        .values(verified_at=func.now())
    )
    return drifted


# --------------------------------------------------------------------------- the operation


async def roll_up(job: Job) -> dict[str, Any]:
    """Drain this workspace's queue, then verify a few folders. The `workspace.rollup` handler.

    Each batch commits on its own — the scan's pattern (12 § job atomicity) — for a reason
    specific to this operation: the workspace lock is transaction-scoped, so committing between
    batches is what stops a long backlog from holding a folder move behind it. Every batch is
    correct on its own, so an interleaved move between two of them is not a special case.
    """
    workspace_id = job.operation.subject_id
    if workspace_id is None:
        raise PermanentFailureError(f"{KIND} needs a workspace as its subject")

    applied = 0
    batches = 0
    while batches < BATCHES_PER_RUN:
        await lock(job.connection, workspace_id)
        written = await drain(job.connection, workspace_id=workspace_id, limit=BATCH)
        if written == 0:
            break
        applied += written
        batches += 1
        fault_point("rollup.after-batch")
        await job.connection.commit()

    # The lock is still held from the last (empty) claim, which is what makes this stamp mean
    # something: nothing can queue a delta and have it drained before the timestamp lands.
    settled = batches < BATCHES_PER_RUN
    if settled:
        await job.connection.execute(
            update(workspace)
            .where(workspace.c.id == workspace_id)
            .values(aggregates_as_of=func.now())
        )
    else:
        # More work than one run should hold a transaction open for. Re-arm rather than continue:
        # the next run starts with a fresh lease and lets anything waiting on the lock through.
        await schedule(job.connection, workspace_id)

    drifted = await verify(job.connection, workspace_id=workspace_id)
    return {
        "folders_updated": applied,
        "batches": batches,
        "settled": settled,
        "drift_corrected": len(drifted),
    }


async def schedule_all(connection: AsyncConnection) -> int:
    """Arm a rollup for every workspace. The floor under `schedule`, called by the janitor.

    Two jobs at once: it restores the chain if a run dead-lettered and stopped re-arming, and it
    is how a workspace nothing has changed still gets its folders verified — the drift sweep
    rides the rollup, and the rollup is otherwise only armed by a change.
    """
    rows = (await connection.execute(select(workspace.c.id))).all()
    for row in rows:
        await schedule(connection, row[0])
    return len(rows)
