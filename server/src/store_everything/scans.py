"""Scan runs, their durable cursor, and what they report: the data access a traversal needs.

The interesting function here is `next_directory`. A scan's cursor is not an offset into
anything — it is a **table of directories discovered and not yet processed** — so "where was
I?" is a query, and "I finished a directory" is a delete committed with that directory's
registrations. That is what makes a 10 TB import survive `kill -9` at any instant with no
bookkeeping beyond the transaction the database already gives us
([12 § job atomicity](../../../specs/12-reliability.md#job-atomicity)).

Two smaller decisions worth naming:

- **Counters are incremented in SQL** (`column + n`), never read-modify-written in Python, so
  a batch's progress cannot be lost to a concurrent update or a stale read.
- **A finished run keeps its findings.** Conflicts and skipped entries are the user's to
  resolve by renaming something on disk (ADR-0019), so they have to outlive the run that
  found them and be listable afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import operations
from store_everything.ids import new_id
from store_everything.tables import scan_finding, scan_frontier, scan_run

type Trigger = Literal["initial", "scheduled", "manual", "watcher"]
type State = Literal["running", "completed", "failed", "cancelled"]
type FindingKind = Literal["conflict", "skipped"]


class ConcurrentScanError(Exception):
    """Another scan of this workspace is genuinely in flight.

    Rare by construction — the queue's idempotency key gives a workspace one pending scan —
    so this is the honest answer rather than a queue: the operation retries later, by which
    time the other run has finished.
    """

    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"scan run {run_id} is already in progress")
        self.run_id = run_id


#: The operation kind a traversal runs as. Declared here rather than in `scanning` so that
#: `workspaces` can arm a new workspace's first scan without importing the traversal, which
#: imports `workspaces`.
KIND = "workspace.scan"

#: Generous on purpose. A scan is resumable, so an attempt costs a directory rather than the
#: run, and every ordinary deploy releases the lease and spends one. The ceiling exists only
#: so a genuinely broken scan stops instead of looping forever.
MAX_ATTEMPTS = 25


#: The root of a workspace as the frontier spells it. Empty rather than `.` or `/` so that
#: joining it to a name needs no special case.
ROOT = ""


@dataclass(frozen=True, slots=True)
class Run:
    id: UUID
    workspace_id: UUID
    trigger: Trigger
    state: State
    root_path: str
    operation_id: UUID
    directories_scanned: int
    files_seen: int
    files_registered: int
    conflicts: int
    skipped: int
    started_at: datetime
    finished_at: datetime | None
    error: str | None

    @property
    def is_running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class Pending:
    """A directory waiting to be processed, and the folder row that represents it."""

    path: str
    folder_id: UUID


_COLUMNS = (
    scan_run.c.id,
    scan_run.c.workspace_id,
    scan_run.c.trigger,
    scan_run.c.state,
    scan_run.c.root_path,
    scan_run.c.operation_id,
    scan_run.c.directories_scanned,
    scan_run.c.files_seen,
    scan_run.c.files_registered,
    scan_run.c.conflicts,
    scan_run.c.skipped,
    scan_run.c.started_at,
    scan_run.c.finished_at,
    scan_run.c.error,
)

type _Row = tuple[
    UUID,
    UUID,
    Trigger,
    State,
    str,
    UUID,
    int,
    int,
    int,
    int,
    int,
    datetime,
    datetime | None,
    str | None,
]


def _as_run(row: _Row) -> Run:
    return Run(*row)


def _query() -> Select[_Row]:
    return select(*_COLUMNS)


# ------------------------------------------------------------------------------ runs


async def start(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    operation_id: UUID,
    trigger: Trigger,
    root_folder_id: UUID,
    root_path: str = ROOT,
) -> Run:
    """Begin a run, or return the one this operation already began.

    The operation is the identity: a re-claim after a crash finds its own run and continues
    it, while the next scheduled operation starts a fresh one. That is the whole reason
    `operation_id` is unique.

    Before starting, a **stale** run is reconciled: one left `running` by an operation that
    has since been cancelled or dead-lettered would otherwise hold the one-scan-per-workspace
    index forever, and no workspace would ever be scanned again. Recovery is the normal path
    here as everywhere (ADR-0010), so it happens on the way in rather than in a repair tool.
    """
    await _reconcile_stale(connection, workspace_id=workspace_id, operation_id=operation_id)

    inserted = (
        await connection.execute(
            pg_insert(scan_run)
            .values(
                id=new_id(),
                workspace_id=workspace_id,
                trigger=trigger,
                state="running",
                root_path=root_path,
                operation_id=operation_id,
            )
            .on_conflict_do_nothing(index_elements=[scan_run.c.operation_id])
            .returning(*_COLUMNS)
        )
    ).first()
    if inserted is not None:
        created = _as_run(tuple(inserted))
        # The traversal starts from one directory: the run's root.
        await push(connection, run_id=created.id, pending=[Pending(root_path, root_folder_id)])
        return created

    resumed = (
        await connection.execute(_query().where(scan_run.c.operation_id == operation_id))
    ).one()
    return _as_run(tuple(resumed))


async def get(connection: AsyncConnection, run_id: UUID) -> Run | None:
    row = (await connection.execute(_query().where(scan_run.c.id == run_id))).first()
    return None if row is None else _as_run(tuple(row))


async def active(connection: AsyncConnection, workspace_id: UUID) -> Run | None:
    """The workspace's running scan, if one is running. At most one exists by index."""
    row = (
        await connection.execute(
            _query().where(scan_run.c.workspace_id == workspace_id, scan_run.c.state == "running")
        )
    ).first()
    return None if row is None else _as_run(tuple(row))


async def latest(connection: AsyncConnection, workspace_id: UUID, *, limit: int) -> list[Run]:
    """A workspace's scans, newest first — what `import-status` reports."""
    rows = (
        await connection.execute(
            _query()
            .where(scan_run.c.workspace_id == workspace_id)
            .order_by(scan_run.c.started_at.desc())
            .limit(limit)
        )
    ).all()
    return [_as_run(tuple(row)) for row in rows]


async def finish(
    connection: AsyncConnection, *, run_id: UUID, state: State, error: str | None = None
) -> bool:
    """End a run, guarded on it still being the running one."""
    result = await connection.execute(
        update(scan_run)
        .where(scan_run.c.id == run_id, scan_run.c.state == "running")
        .values(state=state, error=error, finished_at=func.now())
    )
    return result.rowcount == 1


async def record_progress(
    connection: AsyncConnection,
    *,
    run_id: UUID,
    directories: int = 0,
    files_seen: int = 0,
    files_registered: int = 0,
    conflicts: int = 0,
    skipped: int = 0,
) -> None:
    """Add a batch's tallies to the run. Incremented in SQL, never read-modify-written."""
    await connection.execute(
        update(scan_run)
        .where(scan_run.c.id == run_id)
        .values(
            directories_scanned=scan_run.c.directories_scanned + directories,
            files_seen=scan_run.c.files_seen + files_seen,
            files_registered=scan_run.c.files_registered + files_registered,
            conflicts=scan_run.c.conflicts + conflicts,
            skipped=scan_run.c.skipped + skipped,
        )
    )


# -------------------------------------------------------------------------- the cursor


async def push(connection: AsyncConnection, *, run_id: UUID, pending: list[Pending]) -> None:
    """Add directories to the frontier. Idempotent, so a replayed batch adds nothing twice."""
    if not pending:
        return
    await connection.execute(
        pg_insert(scan_frontier)
        .values(
            [
                {"run_id": run_id, "path": entry.path, "folder_id": entry.folder_id}
                for entry in pending
            ]
        )
        .on_conflict_do_nothing(index_elements=[scan_frontier.c.run_id, scan_frontier.c.path])
    )


async def next_directory(connection: AsyncConnection, run_id: UUID) -> Pending | None:
    """The next directory to process, or `None` when the frontier is empty.

    Ordered by path so a run's progress is legible to a human watching it, and so two
    identical trees are traversed identically — the determinism ADR-0019's conflict rule
    ("the first in traversal order registers") is stated in terms of.
    """
    row = (
        await connection.execute(
            select(scan_frontier.c.path, scan_frontier.c.folder_id)
            .where(scan_frontier.c.run_id == run_id)
            .order_by(scan_frontier.c.path)
            .limit(1)
        )
    ).first()
    return None if row is None else Pending(path=str(row[0]), folder_id=row[1])


async def complete_directory(connection: AsyncConnection, *, run_id: UUID, path: str) -> None:
    """Take a directory off the frontier.

    Committed in the same transaction as that directory's registrations, which is what makes
    the pair a checkpoint rather than two things that can disagree.
    """
    await connection.execute(
        delete(scan_frontier).where(scan_frontier.c.run_id == run_id, scan_frontier.c.path == path)
    )


async def pending_directories(connection: AsyncConnection, run_id: UUID) -> int:
    return (
        await connection.execute(
            select(func.count()).select_from(scan_frontier).where(scan_frontier.c.run_id == run_id)
        )
    ).scalar_one()


# ------------------------------------------------------------------------- findings


async def report(connection: AsyncConnection, *, run_id: UUID, findings: list[Finding]) -> None:
    """Record what the scan refused to register, and why."""
    if not findings:
        return
    await connection.execute(
        insert(scan_finding).values(
            [
                {
                    "run_id": run_id,
                    "kind": finding.kind,
                    "path": finding.path,
                    "detail": finding.detail,
                }
                for finding in findings
            ]
        )
    )


async def findings_of(
    connection: AsyncConnection, run_id: UUID, *, limit: int, after: int | None = None
) -> list[tuple[int, Finding]]:
    """One page of a run's findings, oldest first, with each row's id as the cursor."""
    query = (
        select(scan_finding.c.id, scan_finding.c.kind, scan_finding.c.path, scan_finding.c.detail)
        .where(scan_finding.c.run_id == run_id)
        .order_by(scan_finding.c.id)
        .limit(limit)
    )
    if after is not None:
        query = query.where(scan_finding.c.id > after)
    rows = (await connection.execute(query)).all()
    return [(int(row[0]), Finding(kind=row[1], path=row[2], detail=row[3])) for row in rows]


async def _reconcile_stale(
    connection: AsyncConnection, *, workspace_id: UUID, operation_id: UUID
) -> None:
    """Fail a run whose owning operation is no longer going to finish it.

    A run stays `running` while its operation is retried — that is how a crash resumes — so
    the only stale case is an operation that reached a terminal state (cancelled, or
    dead-lettered after its attempts) while its run still claims to be in progress.
    """
    existing = await active(connection, workspace_id)
    if existing is None or existing.operation_id == operation_id:
        return

    owner = await operations.get(connection, existing.operation_id)
    if owner is not None and not owner.is_terminal:
        # Two live operations for one workspace. The queue's idempotency key makes this rare;
        # refusing is honest, and this operation will be retried once the other has finished.
        raise ConcurrentScanError(existing.id)

    fate = "gone" if owner is None else owner.state
    await finish(
        connection,
        run_id=existing.id,
        state="failed",
        error=f"superseded: the operation that owned this run is {fate}",
    )


async def ensure_scheduled(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    due_in: timedelta | None = None,
    trigger: Trigger = "scheduled",
    path: str = ROOT,
) -> operations.Operation:
    """Guarantee this workspace has a pending scan. Safe to call at any time.

    Per-workspace rather than per-kind, because every workspace carries its own cadence
    (ADR-0019): one key for the kind would let the first workspace's pending scan silence
    every other workspace's.
    """
    return await operations.ensure_scheduled(
        connection,
        kind=KIND,
        max_attempts=MAX_ATTEMPTS,
        due_in=due_in,
        payload={"trigger": trigger, "path": path},
        priority=operations.PRIORITY_HEAVY,
        subject_type="workspace",
        # The path is part of the identity: a rescan of the whole workspace *is* the pending
        # scheduled run and converges on it, while a rescan of one subtree is different work.
        subject_id=workspace_id,
        scope=path or None,
    )
