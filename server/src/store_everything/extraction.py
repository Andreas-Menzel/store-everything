"""Routing files to extractors, and the life of one job.

Two things live here, because they are two halves of one sentence — *this version needs
processing, and here is what happened when it was*:

- **routing** (04 § detection/routing): every path that creates a file version converges on
  `route()`, which matches the version against the registered manifests and writes, per match,
  one `operation` row (the queue) and one `extraction_run` row (the record) sharing an id. It
  runs inside the transaction that created the version, so "the row that says the file exists
  commits with the row that says extract it";
- **the job's transitions**, driven by the extractor over the wire (ADR-0020): claim → running,
  heartbeat, then succeeded or failed. Every one of them is the operation layer's transition
  plus the run's, in one transaction, so the queue and the record cannot disagree.

What is deliberately *not* here: an event per job. Queueing work was never an audited action in
this system (a scan, a rollup and a janitor sweep all queue silently), and a successful run is
a row in `extraction_run` — stamped, joined to its outputs, and outliving the queue. Failures
still reach the log through the operation layer's own `operation.dead_lettered`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import operations
from store_everything.extractors import Manifest
from store_everything.operations import PRIORITY_PRESENCE, PRIORITY_SEARCHABILITY, Operation
from store_everything.tables import (
    FIRST_GENERATION,
    TERMINAL_OPERATION_STATES,
    extraction_run,
    extractor,
    file_version,
    operation,
)

_logger = logging.getLogger(__name__)

#: Operation kinds are per extractor, so the claim query can filter to one extractor's work
#: without a payload predicate — and a worker build that has no handler for `extract.*` never
#: claims one by accident (the in-process runner claims only kinds it can run).
KIND_PREFIX = "extract."

#: How many attempts a job gets before it dead-letters. The operation layer's default is a
#: setting; extraction pins its own so that a poison file cannot be retried forever on an
#: instance whose queue is tuned generously (12 § tuning defaults).
MAX_ATTEMPTS = 4

#: Output kinds that only make a file *present* — browsable, with a thumbnail. Everything else
#: makes it findable, which is the slower and more important half (04 § prioritization).
_PRESENCE_OUTPUTS = frozenset({"metadata", "derived_assets"})

type Status = Literal["none", "pending", "indexed", "partial", "failed"]


def kind_of(extractor_id: str) -> str:
    return f"{KIND_PREFIX}{extractor_id}"


def extractor_of(kind: str) -> str | None:
    """The extractor a job kind belongs to, or `None` if it is not an extraction job."""
    return kind[len(KIND_PREFIX) :] if kind.startswith(KIND_PREFIX) else None


@dataclass(frozen=True, slots=True)
class Run:
    """One execution of one extractor over one file version."""

    id: UUID
    extractor_id: str
    file_version_id: UUID
    generation: int
    state: str
    extractor_version: str | None
    model_version: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    created_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_OPERATION_STATES


@dataclass(frozen=True, slots=True)
class VersionFacts:
    """What an extractor is told about the version it is looking at."""

    id: UUID
    content_hash: str
    size_bytes: int
    media_type: str
    media_class: str
    is_current: bool
    file_id: UUID


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A claim: the job, its fencing token, and the version it is about."""

    operation: Operation
    run: Run
    version: VersionFacts
    lease_expires_at: datetime


_RUN_COLUMNS = (
    extraction_run.c.id,
    extraction_run.c.extractor_id,
    extraction_run.c.file_version_id,
    extraction_run.c.generation,
    extraction_run.c.state,
    extraction_run.c.extractor_version,
    extraction_run.c.model_version,
    extraction_run.c.started_at,
    extraction_run.c.finished_at,
    extraction_run.c.error,
    extraction_run.c.created_at,
)

_VERSION_FACTS = (
    file_version.c.id,
    file_version.c.content_hash,
    file_version.c.size_bytes,
    file_version.c.media_type,
    file_version.c.media_class,
    file_version.c.is_current,
    file_version.c.file_id,
)


def _as_run(row: Sequence[Any]) -> Run:
    return Run(*row)  # pyright: ignore[reportArgumentType]


# ----------------------------------------------------------------------------------- routing


async def route(
    connection: AsyncConnection,
    *,
    file_version_id: UUID,
    media_type: str,
    generation: int = FIRST_GENERATION,
) -> list[Run]:
    """Create the jobs this version needs. Returns the runs, whether new or already there.

    Idempotent per *(version, extractor, generation)*: an extractor that already has a run for
    this version is skipped, so a second call cannot double the work — which is what makes it
    safe to call from every path that creates a version rather than from one careful caller.
    """
    existing = {run.extractor_id: run for run in await runs_for(connection, file_version_id)}
    created: list[Run] = []

    for extractor_id, manifest in await _candidates(connection):
        if extractor_id in existing or not _accepts(manifest, media_type):
            continue
        created.append(
            await _create_job(
                connection,
                extractor_id=extractor_id,
                manifest=manifest,
                file_version_id=file_version_id,
                generation=generation,
            )
        )

    return [*existing.values(), *created]


async def _candidates(connection: AsyncConnection) -> list[tuple[str, Manifest]]:
    """Every extractor that could be routed work: registered, enabled, and still understood.

    A stored manifest is re-validated rather than read field by field, so there is exactly one
    interpretation of the contract in this codebase. The cost is a parse per registered
    extractor per new version, which is noise beside hashing the file's bytes.

    An extractor whose stored manifest this core no longer accepts — a rule tightened in an
    upgrade, say — is skipped with a warning instead of failing the upload that triggered
    routing. It re-registers on its next start-up, and until then its facet is missing, which
    is the contract's own promise about a broken extractor (05 § compatibility rules).
    """
    rows = await connection.execute(
        select(extractor.c.id, extractor.c.manifest).where(
            extractor.c.enabled, extractor.c.manifest.isnot(None)
        )
    )

    candidates: list[tuple[str, Manifest]] = []
    for extractor_id, document in rows.all():
        try:
            candidates.append((extractor_id, Manifest.model_validate(document)))
        except ValidationError:
            _logger.warning(
                "extractor %s has a stored manifest this version cannot read; not routing to it",
                extractor_id,
            )
    return candidates


def _accepts(manifest: Manifest, media_type: str) -> bool:
    """Whether this extractor takes an original of this media type.

    Two things it deliberately does not do yet, both because they need results that do not
    exist until derived data does: match `accepts.derived_kinds` (chaining), and evaluate
    `accepts.when` (a predicate over metadata another extractor writes). An extractor carrying
    a predicate is therefore not routed at all rather than routed as if the predicate held —
    "not yet" is the honest answer for `tesseract-ocr` waiting on `needs_ocr`.
    """
    if manifest.accepts.when is not None:
        return False
    return any(_matches(pattern, media_type) for pattern in manifest.accepts.mime_types)


def _matches(pattern: str, media_type: str) -> bool:
    if pattern == "*/*":
        return True
    if pattern.endswith("/*"):
        return media_type.partition("/")[0] == pattern[:-2]
    return pattern == media_type


def _priority(manifest: Manifest) -> int:
    """The core assigns priority from what an extractor produces; extractors never pick it."""
    return (
        PRIORITY_PRESENCE if set(manifest.produces) <= _PRESENCE_OUTPUTS else PRIORITY_SEARCHABILITY
    )


def idempotency_key(
    *,
    file_version_id: UUID,
    extractor_id: str,
    extractor_version: str,
    model_version: str | None,
    generation: int,
) -> str:
    """Deterministic, so re-detecting the same work converges instead of duplicating it (05).

    Readable rather than hashed: this string turns up in the operation table when somebody is
    working out why a job exists, and a digest would answer that question with nothing.
    """
    return ":".join(
        (
            "extract",
            str(file_version_id),
            extractor_id,
            extractor_version,
            model_version or "-",
            str(generation),
        )
    )


async def _create_job(
    connection: AsyncConnection,
    *,
    extractor_id: str,
    manifest: Manifest,
    file_version_id: UUID,
    generation: int,
) -> Run:
    model = manifest.declared_model
    key = idempotency_key(
        file_version_id=file_version_id,
        extractor_id=extractor_id,
        extractor_version=manifest.version,
        model_version=model.version if model is not None else None,
        generation=generation,
    )
    queued = await operations.enqueue(
        connection,
        kind=kind_of(extractor_id),
        max_attempts=MAX_ATTEMPTS,
        priority=_priority(manifest),
        idempotency_key=key,
        payload={
            "file_version": str(file_version_id),
            "generation": generation,
            # Handed back on claim so an extractor can deduplicate its own side of an
            # at-least-once delivery without recomputing what the core keyed the job on.
            "idempotency_key": key,
            "params": {},
        },
        subject_type="file_version",
        subject_id=file_version_id,
    )

    # The run carries the job's id, so an extractor holding one holds the other. `DO NOTHING`
    # covers the case where `enqueue` converged on a job that already exists: then the run does
    # too, and this call is a no-op rather than a constraint violation.
    await connection.execute(
        insert(extraction_run)
        .values(
            id=queued.id,
            extractor_id=extractor_id,
            file_version_id=file_version_id,
            generation=generation,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    found = await get_run(connection, queued.id)
    if found is None:  # pragma: no cover - just inserted, in this transaction
        raise RuntimeError(f"extraction run {queued.id} vanished as it was created")
    return found


async def supersede_pending(connection: AsyncConnection, *, file_version_id: UUID) -> int:
    """Ask the extractors still working on this version to stop. Returns how many were asked.

    Called when a *newer* version of the same file arrives: whatever is still queued for the old
    one is work nobody is waiting for. Cooperative on purpose — cancellation reaches a running
    extractor through its next heartbeat (12 § leases & fencing), and a result that arrives
    anyway is still accepted, because the version it describes still exists and old versions
    stay searchable ([F-007](../../../features/F-007-versioning.md)).
    """
    pending = await connection.execute(
        select(extraction_run.c.id).where(
            extraction_run.c.file_version_id == file_version_id,
            extraction_run.c.state.notin_(TERMINAL_OPERATION_STATES),
        )
    )
    asked = 0
    for (run_id,) in pending.all():
        if await operations.request_cancel(connection, operation_id=run_id):
            asked += 1
    return asked


# ------------------------------------------------------------------------------------ status


async def get_run(connection: AsyncConnection, run_id: UUID) -> Run | None:
    result = await connection.execute(select(*_RUN_COLUMNS).where(extraction_run.c.id == run_id))
    row = result.first()
    return _as_run(tuple(row)) if row is not None else None


async def runs_for(connection: AsyncConnection, file_version_id: UUID) -> list[Run]:
    """Every run of one version, oldest first — the per-file extraction status (04 § status)."""
    result = await connection.execute(
        select(*_RUN_COLUMNS)
        .where(extraction_run.c.file_version_id == file_version_id)
        .order_by(extraction_run.c.created_at, extraction_run.c.extractor_id)
    )
    return [_as_run(tuple(row)) for row in result.all()]


def status_of(runs: Iterable[Run]) -> Status:
    """The coarse status a listing shows, derived rather than stored.

    Derived because it is a function of rows that already exist, and a stored copy would be one
    more thing to keep true across retries, supersessions and reprocessing. The five answers:

    - `none` — nothing runs on this file. Not a failure: no installed extractor accepts it, or
      none is installed at all;
    - `pending` — at least one job is queued or running. This is what a file looks like the
      moment it lands ([F-001/FR-8](../../../features/F-001-upload-and-import.md));
    - `indexed` — every job succeeded;
    - `partial` — everything finished, some succeeded, some did not;
    - `failed` — everything finished and none succeeded.
    """
    return status_from_states([run.state for run in runs])


def status_from_states(states: Sequence[str]) -> Status:
    """`status_of` over bare states, for the batched query that never builds the rows."""
    if not states:
        return "none"
    if any(state not in TERMINAL_OPERATION_STATES for state in states):
        return "pending"
    succeeded = sum(1 for state in states if state == "succeeded")
    if succeeded == len(states):
        return "indexed"
    return "partial" if succeeded else "failed"


async def statuses_of(
    connection: AsyncConnection, file_version_ids: Sequence[UUID]
) -> dict[UUID, Status]:
    """Statuses for a page of files, in one query rather than one per row."""
    if not file_version_ids:
        return {}

    result = await connection.execute(
        select(
            extraction_run.c.file_version_id,
            extraction_run.c.state,
            func.count().label("runs"),
        )
        .where(extraction_run.c.file_version_id.in_(file_version_ids))
        .group_by(extraction_run.c.file_version_id, extraction_run.c.state)
    )

    counted: dict[UUID, list[str]] = {}
    for version_id, state, runs in result.all():
        counted.setdefault(version_id, []).extend([state] * runs)

    # Every id asked about gets an answer, including the ones with no runs at all — a caller
    # rendering a listing must not have to tell "no extractor wanted it" from "absent from this
    # dictionary".
    return {
        version_id: status_from_states(counted.get(version_id, []))
        for version_id in file_version_ids
    }


async def counts_by_state(connection: AsyncConnection, *, extractor_id: str) -> dict[str, int]:
    """How much work one extractor has, by state — queue depth and failure rate (04 § status)."""
    result = await connection.execute(
        select(extraction_run.c.state, func.count())
        .where(extraction_run.c.extractor_id == extractor_id)
        .group_by(extraction_run.c.state)
    )
    return {str(state): int(count) for state, count in result.all()}


# ------------------------------------------------------------------------------- transitions


async def claim(
    connection: AsyncConnection,
    *,
    extractor_id: str,
    extractor_version: str | None,
    model_version: str | None,
    worker: str,
    lease: timedelta,
) -> ClaimedJob | None:
    """Claim one job for this extractor, or `None` if it has none due.

    The run is stamped with the versions of the extractor that *claimed* it, not the ones it was
    created with: an image upgraded between the two is a different program, and the provenance
    has to name the one that actually ran.
    """
    claimed = await operations.claim(
        connection, worker=worker, lease=lease, kinds=(kind_of(extractor_id),)
    )
    if claimed is None:
        # The idle path is where the mirror is caught up, for two reasons: a claim that found a
        # job's attempts spent has just dead-lettered it without any worker reporting that, and
        # an extractor asking for work is exactly when nobody is waiting on latency.
        await reconcile_runs(connection)
        return None

    run = await get_run(connection, claimed.id)
    version = await _version_facts(connection, run.file_version_id if run else None)
    if run is None or version is None:
        # Neither can happen through `route()`, which writes both rows in one transaction. If it
        # ever does, the job is unrunnable — so it fails without a retry rather than being
        # handed to an extractor that cannot be told what to look at.
        _logger.error("job %s has no run or no file version; failing it", claimed.id)
        await operations.fail(
            connection,
            claimed=claimed,
            error="the job has no run or no file version",
            retryable=False,
        )
        return None

    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(
            state="running",
            extractor_version=extractor_version,
            model_version=model_version,
            started_at=func.now(),
            updated_at=func.now(),
            error=None,
        )
    )
    started = await get_run(connection, claimed.id)
    if started is None:  # pragma: no cover - updated in this transaction
        raise RuntimeError(f"extraction run {claimed.id} vanished as it started")

    lease_expires_at = await _lease_expiry(connection, claimed.id)
    return ClaimedJob(
        operation=claimed, run=started, version=version, lease_expires_at=lease_expires_at
    )


async def _version_facts(
    connection: AsyncConnection, file_version_id: UUID | None
) -> VersionFacts | None:
    if file_version_id is None:
        return None
    result = await connection.execute(
        select(*_VERSION_FACTS).where(file_version.c.id == file_version_id)
    )
    row = result.first()
    return VersionFacts(*tuple(row)) if row is not None else None  # pyright: ignore[reportArgumentType]


@dataclass(frozen=True, slots=True)
class Lease:
    """Who holds a job and until when — the two columns `Operation` deliberately omits."""

    owner: str | None
    expires_at: datetime | None


async def lease_of(connection: AsyncConnection, operation_id: UUID) -> Lease:
    """The current lease, read from the row rather than remembered.

    The heartbeat needs the owner string the claim wrote, and the caller does not send it: the
    replica name in it is a diagnostic, while the thing that actually fences a write is the
    attempt (12 § leases & fencing). Reading it back is what lets both be true.
    """
    result = await connection.execute(
        select(operation.c.leased_by, operation.c.lease_expires_at).where(
            operation.c.id == operation_id
        )
    )
    row = result.first()
    return Lease(owner=None, expires_at=None) if row is None else Lease(row[0], row[1])


async def _lease_expiry(connection: AsyncConnection, operation_id: UUID) -> datetime:
    """Read back what the claim set, so the extractor is told the database's time, not ours."""
    expiry = (await lease_of(connection, operation_id)).expires_at
    if expiry is None:  # pragma: no cover - a claim always sets it
        raise RuntimeError(f"operation {operation_id} was claimed without a lease")
    return expiry


async def owned_job(
    connection: AsyncConnection, *, job_id: UUID, extractor_id: str
) -> tuple[Operation, Run] | None:
    """The job and its run, if they exist and belong to this extractor.

    The ownership check is the kind: a job for `pdf-text` is `extract.pdf-text`, so a credential
    bound to another extractor cannot heartbeat, feed or finish it.
    """
    found = await operations.get(connection, job_id)
    if found is None or found.kind != kind_of(extractor_id):
        return None
    run = await get_run(connection, job_id)
    return (found, run) if run is not None else None


async def finish(connection: AsyncConnection, *, claimed: Operation) -> bool:
    """Record a successful run. False when the lease was lost — the caller answers `409`."""
    if not await operations.succeed(connection, claimed=claimed):
        return False
    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(state="succeeded", finished_at=func.now(), updated_at=func.now(), error=None)
    )
    return True


async def abandon(
    connection: AsyncConnection,
    *,
    claimed: Operation,
    error: str,
    retryable: bool,
    base_seconds: float,
    max_seconds: float,
) -> str:
    """Record a failed attempt, and mirror whatever the queue decided onto the run."""
    state = await operations.fail(
        connection,
        claimed=claimed,
        error=error,
        retryable=retryable,
        base_seconds=base_seconds,
        max_seconds=max_seconds,
    )
    finished = None if state == "queued" else func.now()
    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(state=state, error=error, finished_at=finished, updated_at=func.now())
    )
    return state


async def release(connection: AsyncConnection, *, claimed: Operation, worker: str) -> bool:
    """Hand a claimed job back unstarted — a graceful stop, not a failure (12 § SIGTERM)."""
    if not await operations.release(connection, claimed=claimed, worker=worker):
        return False
    await connection.execute(
        update(extraction_run)
        .where(extraction_run.c.id == claimed.id)
        .values(state="queued", started_at=None, updated_at=func.now())
    )
    return True


async def reconcile_runs(connection: AsyncConnection) -> int:
    """Mirror onto the runs whatever the queue decided without a worker there to say it.

    Two kinds of drift, both from a worker that stopped existing rather than stopping politely,
    and both invisible to the queue itself — its claim query treats an expired lease as
    claimable, which *is* the recovery story (ADR-0010). The record is what needs catching up,
    because the per-file status is read far more often than a job is lost:

    - **the job ended without its worker.** A claim that finds a job's attempts spent
      dead-letters it on the spot, so nobody ever reports that failure; a run left saying
      `running` would report a file as in progress forever.
    - **the job is waiting again.** The lease lapsed and the job is claimable, so `running` is
      no longer true of it.

    Returns how many runs it corrected. Idempotent, and safe to call from anywhere.
    """
    ended = await connection.execute(
        update(extraction_run)
        .where(
            extraction_run.c.state.notin_(TERMINAL_OPERATION_STATES),
            extraction_run.c.id == operation.c.id,
            operation.c.state.in_(TERMINAL_OPERATION_STATES),
        )
        .values(
            state=operation.c.state,
            error=func.coalesce(operation.c.error, extraction_run.c.error),
            finished_at=func.coalesce(extraction_run.c.finished_at, func.now()),
            updated_at=func.now(),
        )
    )

    waiting = await connection.execute(
        update(extraction_run)
        .where(
            extraction_run.c.state == "running",
            extraction_run.c.id == operation.c.id,
            or_(
                operation.c.state == "queued",
                and_(
                    operation.c.state == "running",
                    operation.c.lease_expires_at < func.now(),
                ),
            ),
        )
        .values(state="queued", started_at=None, updated_at=func.now())
    )
    return ended.rowcount + waiting.rowcount


__all__ = [
    "KIND_PREFIX",
    "MAX_ATTEMPTS",
    "ClaimedJob",
    "Lease",
    "Run",
    "Status",
    "VersionFacts",
    "abandon",
    "claim",
    "counts_by_state",
    "extractor_of",
    "finish",
    "get_run",
    "idempotency_key",
    "kind_of",
    "lease_of",
    "owned_job",
    "reconcile_runs",
    "release",
    "route",
    "runs_for",
    "status_of",
    "statuses_of",
    "supersede_pending",
]
