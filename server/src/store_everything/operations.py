"""The operation layer: durable intent, guarded transitions, leases and fencing.

This is the module ADR-0013 commits to owning. Everything effectful in this system runs
through it — uploads, scans, moves, purges, janitor sweeps, extraction jobs — because the
alternative is per-feature job handling, and two mechanisms that must agree is the class of
bug this architecture exists to avoid.

Four rules carry the whole design (12-reliability.md § operation records):

1. **Durable intent first.** The row exists before the first side effect, so a crash leaves
   evidence of what was being attempted rather than a half-finished mystery.
2. **Every transition is a compare-and-swap** on `(id, state, attempt)`. Zero rows affected
   means somebody else advanced or superseded the operation, and the caller must discard its
   work rather than write it.
3. **Leases, not locks.** A claim is instant; ownership is a lease extended by heartbeats.
   An expired lease is re-claimable by anyone, and *that* branch is the recovery path — it
   runs every ordinary day, not only after a crash.
4. **`attempt` is the fencing token.** It is carried on every write-back, so a zombie worker
   that outlived its lease is rejected on write and can never clobber the re-run.

Attempts are counted **on claim**. A job that OOM-kills its worker never reports an error, so
counting failures would let one pathological item retry forever; counting claims dead-letters
it after `max_attempts` regardless of how it died.

All time comparisons use database time (`now()` in SQL). Application clocks never participate
in a lease or schedule decision — with several processes competing for claims, a skewed clock
would otherwise hand out overlapping leases.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Self
from uuid import UUID

from sqlalchemy import Select, and_, func, literal, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import TERMINAL_OPERATION_STATES, operation

type State = Literal[
    "queued", "running", "succeeded", "failed", "dead_letter", "cancelled", "superseded"
]

#: The states a claim can still pick up. Everything else is done with.
CLAIMABLE_STATES = ("queued", "running")

TERMINAL_STATES = frozenset(TERMINAL_OPERATION_STATES)

#: Priority classes (04 § prioritization). Named so call sites read as intent, not numbers.
PRIORITY_INTERACTIVE = 0
PRIORITY_PRESENCE = 1
PRIORITY_SEARCHABILITY = 2
PRIORITY_HEAVY = 3
PRIORITY_REPROCESSING = 4

#: How many times a convergent enqueue re-reads after its insert conflicted. Each turn either
#: finds a row to converge on or leaves the key free for the next insert, so two is already
#: generous — the third exists only so that an unlucky third party cannot make this the caller's
#: problem, which is the whole point: the caller's transaction is somebody's upload.
_CONVERGE_ATTEMPTS = 3

_COLUMNS = (
    operation.c.id,
    operation.c.kind,
    operation.c.state,
    operation.c.priority,
    operation.c.attempt,
    operation.c.max_attempts,
    operation.c.payload,
    operation.c.cancel_requested,
    operation.c.subject_type,
    operation.c.subject_id,
)


@dataclass(frozen=True, slots=True)
class Operation:
    """A claimed or queued operation, as much of the row as a caller needs."""

    id: UUID
    kind: str
    state: State
    priority: int
    attempt: int
    max_attempts: int
    payload: dict[str, Any]
    cancel_requested: bool
    subject_type: str | None
    subject_id: UUID | None

    @classmethod
    def of(cls, row: tuple[Any, ...]) -> Self:
        return cls(*row)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempt)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """The answer to "may I keep going?" — and the only channel that says "stop"."""

    lease_extended: bool
    cancel_requested: bool


def retry_delay(attempt: int, *, base_seconds: float, max_seconds: float) -> timedelta:
    """Exponential backoff with full jitter.

    Jitter is load-bearing rather than decorative: without it a batch of items that failed
    together retries together forever, and the thundering herd is self-sustaining.
    """
    ceiling = min(max_seconds, base_seconds * (2 ** max(0, attempt - 1)))
    return timedelta(seconds=random.uniform(base_seconds, max(base_seconds, ceiling)))  # noqa: S311 - jitter, not cryptography


async def enqueue(
    connection: AsyncConnection,
    *,
    kind: str,
    max_attempts: int,
    payload: dict[str, Any] | None = None,
    priority: int = PRIORITY_SEARCHABILITY,
    idempotency_key: str | None = None,
    due_in: timedelta | None = None,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    converge_with_running: bool = True,
) -> Operation:
    """Record the intent to do something, in the caller's transaction.

    Same-transaction enqueue is the property that makes this layer worth owning: the row
    that says "extract this file" commits with the row that says the file exists, so there
    is no window in which one is true and the other is not.

    With an `idempotency_key`, re-enqueuing work that is still pending **converges on the
    existing operation** instead of creating a second one — which is what makes a re-scan
    during a running scan harmless.

    `converge_with_running` separates two questions that look alike. Work identity ("is this
    scan already happening?") must consider the run in flight, so it defaults to true. A
    recurring operation queueing its own successor must *not*: it **is** the running row, and
    treating that as a conflict would break the chain it is extending.
    """
    due = func.now() if due_in is None else func.now() + _interval(due_in)

    insertion = pg_insert(operation).values(
        id=new_id(),
        kind=kind,
        state="queued",
        priority=priority,
        max_attempts=max_attempts,
        next_due_at=due,
        idempotency_key=idempotency_key,
        payload=dict(payload or {}),
        subject_type=subject_type,
        subject_id=subject_id,
    )

    if idempotency_key is None:
        created = (await connection.execute(insertion.returning(*_COLUMNS))).one()
        return Operation.of(tuple(created))

    if converge_with_running:
        running = (
            await connection.execute(
                select(*_COLUMNS).where(
                    operation.c.idempotency_key == idempotency_key,
                    operation.c.state == "running",
                )
            )
        ).first()
        if running is not None:
            return Operation.of(tuple(running))

    # The unique index is partial (queued rows only), so the conflict target names the same
    # predicate; `DO NOTHING` then means "somebody queued this between the check and here",
    # and their row is the one to return.
    #
    # Both statements can lose the same race, and this used to end in `.one()` raising
    # `NoResultFound`: the insert conflicts with a queued row, a worker claims that row before
    # the look-up runs, and the look-up — filtered to `queued` — finds nothing. `ON CONFLICT DO
    # NOTHING` takes no lock on the row it declined to touch, so nothing prevents it. The
    # exception then aborted the *caller's* transaction, and the callers are every upload and
    # every scan batch. So the pair is retried: a row that has started running is converged with
    # exactly as the check above would have done a moment later, and if the key is free again the
    # next insert takes it.
    conflict_free = insertion.on_conflict_do_nothing(
        index_elements=[operation.c.idempotency_key],
        index_where=text("idempotency_key IS NOT NULL AND state = 'queued'"),
    ).returning(*_COLUMNS)
    pending = ("queued", "running") if converge_with_running else ("queued",)
    for _ in range(_CONVERGE_ATTEMPTS):
        inserted = (await connection.execute(conflict_free)).first()
        if inserted is not None:
            return Operation.of(tuple(inserted))
        existing = (
            await connection.execute(
                select(*_COLUMNS)
                .where(
                    operation.c.idempotency_key == idempotency_key,
                    operation.c.state.in_(pending),
                )
                # Queued before running: converging on work that has not started beats
                # converging on work that may already have read what this call is about to write.
                .order_by(operation.c.state.desc())
                .limit(1)
            )
        ).first()
        if existing is not None:
            return Operation.of(tuple(existing))
    raise RuntimeError(  # pragma: no cover - the key would have to churn on every attempt
        f"could not converge on the operation holding {idempotency_key!r}"
    )


def _claimable() -> Select[tuple[UUID]]:
    """Queued work that is due, plus running work whose lease has expired.

    The second branch *is* the recovery story: nothing sweeps stale leases on start-up
    because every claim already looks for them.
    """
    return (
        select(operation.c.id)
        .where(
            or_(
                and_(operation.c.state == "queued", operation.c.next_due_at <= func.now()),
                and_(
                    operation.c.state == "running",
                    operation.c.lease_expires_at < func.now(),
                ),
            )
        )
        .order_by(operation.c.priority, operation.c.next_due_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


async def claim(
    connection: AsyncConnection,
    *,
    worker: str,
    lease: timedelta,
    kinds: tuple[str, ...] | None = None,
) -> Operation | None:
    """Take ownership of one operation, or return `None` when there is nothing to do.

    `FOR UPDATE SKIP LOCKED` is only the claim *instant* — holding a row lock for the
    duration of the work would mean a transaction open for hours, which dies on every deploy
    and bloats vacuum. What the worker leaves with is a lease it must renew.

    Nothing is written when nothing is claimable, so an idle instance is genuinely idle.
    """
    candidate = _claimable()
    if kinds is not None:
        candidate = candidate.where(operation.c.kind.in_(kinds))

    claimed = (
        await connection.execute(
            update(operation)
            .where(operation.c.id == candidate.scalar_subquery())
            .values(
                state="running",
                leased_by=worker,
                lease_expires_at=func.now() + _interval(lease),
                attempt=operation.c.attempt + 1,
                started_at=func.coalesce(operation.c.started_at, func.now()),
                updated_at=func.now(),
            )
            .returning(*_COLUMNS)
        )
    ).first()

    if claimed is None:
        return None

    taken = Operation.of(tuple(claimed))
    if taken.attempt > taken.max_attempts:
        # Counting on claim is what catches the job that never reports anything: this
        # attempt is one too many, so it dead-letters instead of running again.
        await _dead_letter(connection, taken)
        return None
    return taken


def _interval(value: timedelta) -> Any:
    return text(f"interval '{value.total_seconds()} seconds'")


async def _dead_letter(connection: AsyncConnection, taken: Operation) -> None:
    await connection.execute(
        update(operation)
        .where(operation.c.id == taken.id, operation.c.attempt == taken.attempt)
        .values(
            state="dead_letter",
            leased_by=None,
            lease_expires_at=None,
            finished_at=func.now(),
            updated_at=func.now(),
            error=func.coalesce(
                operation.c.error, literal_column("'attempts exhausted without a result'")
            ),
        )
    )
    # Worth an audit record: a dead-lettered operation is work the system has given up on,
    # and somebody has to be able to find out that it did.
    await events.record(
        connection,
        action=events.OPERATION_DEAD_LETTERED,
        resource_type=events.RESOURCE_OPERATION,
        resource_id=taken.id,
        actor=Actor.system(),
        details={"kind": taken.kind, "attempts": taken.attempt},
    )


async def heartbeat(
    connection: AsyncConnection, *, claimed: Operation, worker: str, lease: timedelta
) -> Heartbeat:
    """Extend the lease and ask whether to continue.

    A heartbeat that cannot extend the lease means the operation was reclaimed or
    superseded while this worker was busy. The worker must abort immediately: its lease is
    gone, so any write it still attempts will be fenced out anyway.
    """
    extended = (
        await connection.execute(
            update(operation)
            .where(
                operation.c.id == claimed.id,
                operation.c.state == "running",
                operation.c.attempt == claimed.attempt,
                operation.c.leased_by == worker,
            )
            .values(lease_expires_at=func.now() + _interval(lease), updated_at=func.now())
            .returning(operation.c.cancel_requested)
        )
    ).first()

    if extended is None:
        return Heartbeat(lease_extended=False, cancel_requested=True)
    return Heartbeat(lease_extended=True, cancel_requested=bool(extended[0]))


async def _finish(
    connection: AsyncConnection,
    *,
    claimed: Operation,
    state: State,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> bool:
    """Guarded terminal transition. `False` means the caller lost the race and must not act."""
    changed = (
        await connection.execute(
            update(operation)
            .where(
                operation.c.id == claimed.id,
                operation.c.state == "running",
                operation.c.attempt == claimed.attempt,
            )
            .values(
                state=state,
                result=result,
                error=error,
                leased_by=None,
                lease_expires_at=None,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(operation.c.id)
        )
    ).first()
    return changed is not None


async def succeed(
    connection: AsyncConnection, *, claimed: Operation, result: dict[str, Any] | None = None
) -> bool:
    """Mark the work done, in the same transaction as its effects.

    Because the transition is guarded, the effects and the state agree: a zombie's late
    success finds `attempt` moved on, changes nothing, and its transaction is rolled back
    by the caller.
    """
    return await _finish(connection, claimed=claimed, state="succeeded", result=result)


async def fail(
    connection: AsyncConnection,
    *,
    claimed: Operation,
    error: str,
    retryable: bool = True,
    base_seconds: float = 10.0,
    max_seconds: float = 3600.0,
) -> State:
    """Record a failure, and either schedule a retry or give up.

    Returns the state actually reached, so a caller can log the difference between "will be
    tried again" and "this needs a human".
    """
    if not retryable:
        await _finish(connection, claimed=claimed, state="failed", error=error)
        return "failed"

    if claimed.attempts_left <= 0:
        await connection.execute(
            update(operation)
            .where(
                operation.c.id == claimed.id,
                operation.c.state == "running",
                operation.c.attempt == claimed.attempt,
            )
            .values(error=error, updated_at=func.now())
        )
        await _dead_letter(connection, claimed)
        return "dead_letter"

    delay = retry_delay(claimed.attempt, base_seconds=base_seconds, max_seconds=max_seconds)
    await connection.execute(
        update(operation)
        .where(
            operation.c.id == claimed.id,
            operation.c.state == "running",
            operation.c.attempt == claimed.attempt,
        )
        .values(
            state="queued",
            error=error,
            leased_by=None,
            lease_expires_at=None,
            next_due_at=func.now() + _interval(delay),
            updated_at=func.now(),
        )
    )
    return "queued"


async def release(connection: AsyncConnection, *, claimed: Operation, worker: str) -> bool:
    """Hand a claim back without consuming an attempt — the SIGTERM path.

    A graceful stop is an optimization, never a correctness mechanism: releasing lets a
    successor pick the work up at once instead of waiting out the lease. The attempt already
    counted on claim, so this is deliberately *not* a retry.
    """
    released = (
        await connection.execute(
            update(operation)
            .where(
                operation.c.id == claimed.id,
                operation.c.state == "running",
                operation.c.attempt == claimed.attempt,
                operation.c.leased_by == worker,
            )
            .values(
                state="queued",
                leased_by=None,
                lease_expires_at=None,
                next_due_at=func.now(),
                updated_at=func.now(),
            )
            .returning(operation.c.id)
        )
    ).first()
    return released is not None


async def request_cancel(connection: AsyncConnection, *, operation_id: UUID) -> bool:
    """Ask an operation to stop. Pending work is cancelled outright; running work is told.

    Cancellation is a durable flag rather than a signal, so it survives the restart of both
    the requester and the worker.
    """
    cancelled_outright = (
        await connection.execute(
            update(operation)
            .where(operation.c.id == operation_id, operation.c.state == "queued")
            .values(
                state="cancelled",
                cancel_requested=True,
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .returning(operation.c.id)
        )
    ).first()
    if cancelled_outright is not None:
        return True

    flagged = (
        await connection.execute(
            update(operation)
            .where(operation.c.id == operation_id, operation.c.state == "running")
            .values(cancel_requested=True, updated_at=func.now())
            .returning(operation.c.id)
        )
    ).first()
    return flagged is not None


async def mark_cancelled(connection: AsyncConnection, *, claimed: Operation) -> bool:
    """The worker's acknowledgement that it stopped when asked."""
    return await _finish(
        connection, claimed=claimed, state="cancelled", error="cancelled on request"
    )


async def ensure_scheduled(
    connection: AsyncConnection,
    *,
    kind: str,
    max_attempts: int,
    due_in: timedelta | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = PRIORITY_HEAVY,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    scope: str | None = None,
) -> Operation:
    """Guarantee exactly one pending operation of a recurring kind.

    Periodic work re-arms itself: a run enqueues its successor in the transaction that
    completes it, so the chain cannot break between the two. This call is the floor under
    that chain — it is safe to make on every start-up, and it is what restores the schedule
    after a run dead-letters and stops re-arming.

    With a `subject_id` the schedule is **per subject** rather than per kind: every workspace
    carries its own scan cadence (ADR-0019), so "one pending run" has to mean one *per
    workspace* — a single key for the kind would let the first workspace's pending scan
    silence every other workspace's. `scope` narrows it further, for work that concerns one
    part of a subject.
    """
    key = f"schedule:{kind}" if subject_id is None else f"schedule:{kind}:{subject_id}"
    if scope is not None:
        # A narrower slice of the same subject — a subtree rescan beside the workspace's own
        # schedule — is different work and needs its own pending row.
        key = f"{key}:{scope}"
    return await enqueue(
        connection,
        kind=kind,
        max_attempts=max_attempts,
        payload=payload,
        priority=priority,
        idempotency_key=key,
        subject_type=subject_type,
        subject_id=subject_id,
        due_in=due_in,
        # The caller may *be* the running instance of this schedule, queueing its successor.
        converge_with_running=False,
    )


async def expedite(
    connection: AsyncConnection,
    *,
    operation_id: UUID,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Make a queued operation due no later than now. Returns whether it was still queued.

    This is the whole mechanism behind "run it now": a manual re-scan and (later) a watcher
    event do not start a parallel traversal, they pull the pending one forward
    (12 § durable schedules, lossy doorbells). A running operation is left alone — it is
    already doing the work being asked for.

    `payload` merges into the row's own, which is how the *reason* travels: a request that
    converges on a pending scheduled run would otherwise leave that run reporting itself as
    scheduled, and "why did this happen?" is exactly what the record is for.

    Due-ness moves to the *earlier* of the two times rather than to `now()`. An already-due
    row must not be pushed back — that would let repeated requests starve work that is
    waiting — and it still has to take the payload, because a request while the queue is
    backed up (or the worker is stopped) is exactly when someone presses the button.
    """
    values: dict[str, Any] = {
        "next_due_at": func.least(operation.c.next_due_at, func.now()),
        "updated_at": func.now(),
    }
    if payload is not None:
        values["payload"] = operation.c.payload.op("||")(literal(payload, JSONB))

    result = await connection.execute(
        update(operation)
        .where(operation.c.id == operation_id, operation.c.state == "queued")
        .values(**values)
    )
    return result.rowcount == 1


async def get(connection: AsyncConnection, operation_id: UUID) -> Operation | None:
    found = (
        await connection.execute(select(*_COLUMNS).where(operation.c.id == operation_id))
    ).first()
    return Operation.of(tuple(found)) if found is not None else None


async def count_by_state(connection: AsyncConnection, *, kind: str | None = None) -> dict[str, int]:
    """Queue depth per state — the shape the status API and the tests both want."""
    query = select(operation.c.state, func.count()).group_by(operation.c.state)
    if kind is not None:
        query = query.where(operation.c.kind == kind)
    rows = (await connection.execute(query)).all()
    return {str(state): int(count) for state, count in rows}


async def states_of(connection: AsyncConnection, ids: Sequence[UUID]) -> dict[UUID, str]:
    """The states of many operations in one query.

    For callers that hold a pile of ids — the janitor looking at a directory full of staging
    files — and must not turn that into one round trip each.
    """
    if not ids:
        return {}
    rows = (
        await connection.execute(
            select(operation.c.id, operation.c.state).where(operation.c.id.in_(list(ids)))
        )
    ).all()
    return {row[0]: str(row[1]) for row in rows}
