"""The worker loop: claim an operation, run its handler, heartbeat, transition.

This is the only place that turns queued intent into work. It is deliberately small, because
everything that makes the model safe lives in `operations.py`: the loop's job is to keep a
lease alive while a handler runs, and to translate the handler's outcome into one guarded
transition.

Four properties are worth stating, since each is a decision rather than an accident:

- **Polling is the durable path.** A claim writes nothing when nothing is claimable, so an
  idle instance is genuinely idle — the failure mode ADR-0013 calls out by name is a
  `NOTIFY` on a timer keeping the disk busy forever. A doorbell can be added later as an
  optimization over this loop; it can never replace it.
- **The heartbeat is also the cancellation channel.** The handler runs as a task while the
  loop renews the lease; when the renewal reports a cancellation, or fails because the lease
  was lost, the task is cancelled. A worker that has lost its lease must stop immediately —
  its writes would be fenced out anyway, so continuing only wastes work. Because that loop
  is the only channel, it may not die of a transient fault: a renewal that cannot reach the
  database is retried while the lease it renews could still be alive, and only an elapsed
  window counts as a lost lease.
- **Only a cancellation somebody asked for is recorded as `cancelled`.** A worker cancelled
  from outside — shutdown, a sibling task failing the group — stops its handler and gives
  the claim back instead; a worker that lost its lease writes nothing at all. Both leave
  work that can be re-claimed, which is the floor `kill -9` already guarantees, and a
  terminal state written on the way out would sit below it (ADR-0010).
- **SIGTERM stops claiming and releases what is held.** That is an optimization for restart
  speed, never a correctness mechanism: a `kill -9` at any instant is equally safe, just
  slower to notice, because the lease expires and the claim query's reclaim branch picks the
  work up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from store_everything import operations
from store_everything.config import Settings
from store_everything.db import migrations_are_current
from store_everything.faults import fault_point
from store_everything.operations import Operation

_logger = logging.getLogger(__name__)

#: Ceiling for the claim loop's error backoff. Not a tuning knob: it only paces retries
#: against a database that is down, where the useful range is "often enough to notice".
_MAX_LOOP_BACKOFF_SECONDS = 60.0

#: How long a cancelled handler is given to actually stop before the worker moves on
#: without it. Exceeding this is a handler bug, so it is logged rather than waited out —
#: and it is safe, because whatever the handler still writes is fenced by `attempt`.
_CANCEL_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Job:
    """What a handler is given: the claimed operation and its own database connection.

    The connection is the handler's transaction. Whatever it writes commits together with
    the operation's terminal transition, so effects and state can never disagree.
    """

    operation: Operation
    connection: AsyncConnection

    @property
    def payload(self) -> dict[str, Any]:
        return self.operation.payload

    @property
    def attempt(self) -> int:
        """The fencing token. Any write-back outside this connection must carry it."""
        return self.operation.attempt


Handler = Callable[[Job], Coroutine[Any, Any, dict[str, Any] | None]]
"""A handler returns an optional small result to record, or raises to fail the operation."""


@dataclass(slots=True)
class _Renewal:
    """Shared between an execution and its lease-keeper: why the work was stopped.

    Written *before* the cancellation is delivered, so the executor never has to infer from
    a `CancelledError` whether a user asked for this or the lease simply went away — two
    situations with opposite correct answers.
    """

    stopped: Literal["cancel_requested", "lease_lost"] | None = None


class PermanentFailureError(Exception):
    """Raised by a handler when retrying cannot help — a malformed payload, a missing root.

    Everything else is treated as retryable, which is the right default: a transient fault
    that is mistaken for permanent loses work, while a permanent fault mistaken for
    transient merely dead-letters a few attempts later.
    """


def _cancelled_from_outside() -> bool:
    """Whether *this* task was cancelled, as opposed to something it was awaiting.

    Needed because `await task` cannot tell the two apart: cancelling a task forwards the
    cancellation into the future it is waiting on, so an in-flight handler ends up
    `cancelled()` either way — whether its own lease-keeper stopped it or the process is
    shutting down. `Task.cancelling()` counts cancellations requested *on this task*, which
    is exactly the distinction, and it is what decides whether the cancellation must keep
    travelling outwards.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def worker_name() -> str:
    """Identifies the lease holder. Host and pid, so a stale lease is traceable to a process."""
    return f"{socket.gethostname()}/{os.getpid()}"


class Runner:
    """Claims and executes operations until stopped."""

    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        handlers: dict[str, Handler],
        *,
        worker: str | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._handlers = handlers
        self._worker = worker or worker_name()
        self._lease = timedelta(seconds=settings.lease_seconds)
        self._heartbeat_every = timedelta(seconds=settings.heartbeat_seconds)
        self._stopping = asyncio.Event()

    @property
    def worker(self) -> str:
        return self._worker

    def stop(self) -> None:
        """Stop claiming. In-flight work finishes or is released."""
        self._stopping.set()

    async def wait_until_ready(self) -> bool:
        """Block until the database is reachable and its schema matches this build.

        A fresh install starts the stack *before* applying migrations — that is the
        documented order, since migrations are a deliberate step (Q20) — so "the schema is
        not there yet" is an ordinary startup state, not a failure. Crashing here would put
        the worker in a restart loop printing a stack trace per attempt, which tells an
        operator nothing they did not already know from `/readyz`.

        Returns `False` if the process was asked to stop while waiting.
        """
        announced = False
        while not self._stopping.is_set():
            try:
                if await migrations_are_current(self._engine):
                    return True
                reason = "migrations are pending"
            except SQLAlchemyError:
                reason = "the database is unreachable"

            if not announced:
                # Once, not once per poll: this is a normal state that can last as long as
                # the operator takes to run the migration step.
                _logger.info(
                    "waiting before claiming work", extra={"reason": reason, "worker": self._worker}
                )
                announced = True

            await self._pause(self._settings.worker_poll_seconds)
        return False

    async def _pause(self, seconds: float) -> None:
        """Wait — but wake immediately on shutdown rather than sleeping through it."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def run_forever(self) -> None:
        """Claim and run, with as many operations in flight as configured."""
        _logger.info(
            "worker started",
            extra={
                "worker": self._worker,
                "concurrency": self._settings.worker_concurrency,
                "kinds": sorted(self._handlers),
            },
        )
        async with asyncio.TaskGroup() as group:
            for _ in range(self._settings.worker_concurrency):
                group.create_task(self._claim_loop())
        _logger.info("worker stopped", extra={"worker": self._worker})

    async def _claim_loop(self) -> None:
        """Claim and run until stopped, surviving the database going away underneath.

        A failure here is the *worker's* — a dropped connection, an exhausted pool — not an
        operation's, and it must not end the loop: every loop in the process is a sibling in
        one `TaskGroup`, which cancels all of them the moment any task raises. One blip would
        therefore stop the whole process from claiming anything until somebody restarted it,
        while the work it should be doing sat queued. So the loop backs off and tries again,
        which is the answer this layer gives everywhere else: recovery is the ordinary path.
        """
        failures = 0
        while not self._stopping.is_set():
            if _cancelled_from_outside():
                # A handler that swallows its `CancelledError` absorbs the cancellation
                # entirely — `await work` never raises, so the loop would carry on claiming
                # work for a process that is trying to shut down, and the shutdown would wait
                # on it forever. Whoever cancelled us gets the answer they asked for.
                raise asyncio.CancelledError
            try:
                busy = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                _logger.warning(
                    "claim loop failed; backing off",
                    extra={"worker": self._worker, "consecutive_failures": failures},
                    exc_info=True,
                )
                await self._pause(self._backoff(failures))
                continue
            failures = 0
            if not busy:
                await self._pause(self._settings.worker_poll_seconds)

    def _backoff(self, failures: int) -> float:
        """How long to wait after a *worker* failure, jittered and capped.

        Starts at the idle poll interval — a blip should cost about what an idle tick costs —
        and doubles up to a ceiling that keeps a worker from hammering a database that is
        down, while still noticing its return without an operator poking anything.
        """
        return operations.retry_delay(
            failures,
            base_seconds=self._settings.worker_poll_seconds,
            max_seconds=_MAX_LOOP_BACKOFF_SECONDS,
        ).total_seconds()

    async def run_once(self) -> bool:
        """Claim and run at most one operation. `False` means the queue had nothing due."""
        kinds = tuple(sorted(self._handlers))
        async with self._engine.connect() as connection:
            claimed = await operations.claim(
                connection, worker=self._worker, lease=self._lease, kinds=kinds
            )
            # The claim — and a dead-letter decision taken inside it — must be durable
            # before the work starts, or a crash would lose the attempt count.
            await connection.commit()

        if claimed is None:
            return False

        await self._execute(claimed)
        return True

    async def _execute(self, claimed: Operation) -> None:
        handler = self._handlers[claimed.kind]
        log = {"operation": str(claimed.id), "kind": claimed.kind, "attempt": claimed.attempt}

        renewal = _Renewal()
        async with self._engine.connect() as connection:
            work = asyncio.create_task(handler(Job(operation=claimed, connection=connection)))
            keeper = asyncio.create_task(self._keep_lease(claimed, work, renewal))
            try:
                result = await work
            except asyncio.CancelledError:
                # Two independent questions, and this path used to answer neither: *why* the
                # work stopped, and whether the cancellation is still travelling outwards.
                if renewal.stopped is not None:
                    await self._on_stopped(claimed, renewal, log)
                else:
                    # Nobody asked this operation to stop — the process is going down, or a
                    # sibling task failed the group. The handler is still running against a
                    # connection about to close, and the operation is still ours: stop it and
                    # hand the claim back. Recording `cancelled` here, as this path used to
                    # for every arrival, makes a partial failure worse than `kill -9`, which
                    # at least leaves the work reclaimable (ADR-0010).
                    await self._stop_work(work, log)
                    await self._release(claimed, log)
                if _cancelled_from_outside():
                    raise
                return
            except PermanentFailureError as permanent:
                await connection.rollback()
                await self._transition_failure(claimed, str(permanent), retryable=False, log=log)
                return
            # Any fault in a handler is the operation's failure, not the worker's.
            except Exception as failure:
                await connection.rollback()
                _logger.warning("operation failed", extra=log, exc_info=True)
                await self._transition_failure(claimed, repr(failure), retryable=True, log=log)
                return
            finally:
                await self._stop_keeper(keeper, log)

            # The handler's writes and the success transition commit together: that is what
            # makes "the effects happened" and "the operation succeeded" the same fact. The
            # fault points bracket that seam, because it is the one place where a crash could
            # plausibly apply effects without recording them (12 § verification).
            fault_point("operation.after-handler")
            if await operations.succeed(connection, claimed=claimed, result=result):
                fault_point("operation.after-success-transition")
                await connection.commit()
                fault_point("operation.after-commit")
                _logger.info("operation succeeded", extra=log)
            else:
                # The lease was lost mid-flight; the re-run owns this work now.
                await connection.rollback()
                _logger.warning("operation superseded before it could be recorded", extra=log)

    async def _keep_lease(
        self, claimed: Operation, work: asyncio.Task[Any], renewal: _Renewal
    ) -> None:
        """Renew the lease until the work finishes — and stop the work when told to.

        This loop is the work's only cancellation channel, so it may not die of a transient
        fault. A renewal that cannot reach the database is *ignorance*, not a verdict: it is
        retried while the lease it renews could still be alive. Only an elapsed window means
        the lease is gone — from that instant another claim may already own the row — and
        then the work is stopped, because a worker without a lease has nothing to write with.

        The window is measured on the monotonic clock even though every lease *decision* is
        made in SQL (`operations`). The difference matters: this is one worker deciding when
        to stop trusting itself, which is local and conservative, not two workers' claims
        being adjudicated against each other.
        """
        interval = self._heartbeat_every.total_seconds()
        expires_at = time.monotonic() + self._lease.total_seconds()
        while not work.done():
            await asyncio.sleep(interval)
            if work.done():
                return
            try:
                beat = await self._beat(claimed)
            except Exception:
                if time.monotonic() < expires_at:
                    _logger.warning(
                        "heartbeat failed; retrying inside the lease window",
                        extra={"operation": str(claimed.id)},
                        exc_info=True,
                    )
                    continue
                _logger.warning(
                    "the lease window elapsed with no heartbeat; stopping the work",
                    extra={"operation": str(claimed.id)},
                    exc_info=True,
                )
                renewal.stopped = "lease_lost"
                work.cancel()
                return
            if not beat.lease_extended or beat.cancel_requested:
                _logger.info(
                    "stopping work on request or lost lease",
                    extra={
                        "operation": str(claimed.id),
                        "lease_extended": beat.lease_extended,
                        "cancel_requested": beat.cancel_requested,
                    },
                )
                # A cancellation that was *asked for* is the only one that ends as
                # `cancelled`; a lost lease leaves the row to whoever holds it now.
                renewal.stopped = "cancel_requested" if beat.lease_extended else "lease_lost"
                work.cancel()
                return
            expires_at = time.monotonic() + self._lease.total_seconds()

    async def _beat(self, claimed: Operation) -> operations.Heartbeat:
        async with self._engine.connect() as connection:
            beat = await operations.heartbeat(
                connection, claimed=claimed, worker=self._worker, lease=self._lease
            )
            await connection.commit()
        return beat

    async def _stop_keeper(self, keeper: asyncio.Task[None], log: dict[str, Any]) -> None:
        """End the lease-keeper and look at how it ended.

        Awaiting it is the point: a keeper task that is created, cancelled and never awaited
        can fail without anyone finding out, which is precisely how it used to leave a
        handler running with nobody renewing its lease.
        """
        keeper.cancel()
        try:
            await keeper
        except asyncio.CancelledError:
            return
        except Exception:
            _logger.warning("lease keeper failed", extra=log, exc_info=True)

    async def _on_stopped(self, claimed: Operation, renewal: _Renewal, log: dict[str, Any]) -> None:
        """Record — or deliberately not record — work the keeper stopped.

        Only one of the two reasons is a cancellation:

        - **the user asked**: the heartbeat carried `cancel_requested`, so `cancelled` is the
          truth, and the transition is guarded like every other;
        - **the lease is gone**: whoever holds it now owns this work, so nothing terminal may
          be written. It would either be fenced out (harmless) or — if no one has reclaimed
          the row yet, which is exactly the case when the database was simply unreachable —
          bury work that nobody cancelled. Handing the claim back is guarded on still holding
          it, so it is a no-op in the first case and an immediate re-claim in the second.
        """
        if renewal.stopped == "cancel_requested":
            async with self._engine.connect() as connection:
                await operations.mark_cancelled(connection, claimed=claimed)
                await connection.commit()
            _logger.info("operation cancelled", extra=log)
            return

        await self._release(claimed, log)

    async def _stop_work(self, work: asyncio.Task[Any], log: dict[str, Any]) -> None:
        """Make sure the handler is not left running against a connection about to close.

        Usually a no-op, and deliberately so: a cancellation aimed at this task is delivered
        *into* whatever it is awaiting, so by the time it surfaces here the handler has almost
        always taken it already. What remains is the handler that swallowed it — which nothing
        here can fix, so it is named in the log and left behind, safe because anything it
        still writes is fenced by `attempt`.
        """
        work.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(work), timeout=_CANCEL_GRACE_SECONDS)
        if not work.done():
            _logger.warning("handler did not stop when cancelled", extra=log)

    async def _release(self, claimed: Operation, log: dict[str, Any]) -> None:
        """Give the claim back, best effort — the lease expiring is the fallback.

        Guarded on still holding the lease (`operations.release`), so this is a no-op when
        the work was reclaimed and an immediate re-claim when it was not. If the release
        itself cannot be written, nothing is lost: the lease runs out and the claim query's
        reclaim branch picks the operation up, which is the recovery path that runs every
        ordinary day (12 § leases & fencing).
        """
        try:
            async with self._engine.connect() as connection:
                released = await operations.release(
                    connection, claimed=claimed, worker=self._worker
                )
                await connection.commit()
        except Exception:
            _logger.warning(
                "could not hand the claim back; its lease will expire instead",
                extra=log,
                exc_info=True,
            )
            return
        _logger.info("operation stopped without a result", extra={**log, "released": released})

    async def _transition_failure(
        self, claimed: Operation, error: str, *, retryable: bool, log: dict[str, Any]
    ) -> None:
        async with self._engine.connect() as connection:
            reached = await operations.fail(
                connection,
                claimed=claimed,
                error=error,
                retryable=retryable,
                base_seconds=self._settings.retry_base_seconds,
                max_seconds=self._settings.retry_max_seconds,
            )
            await connection.commit()
        _logger.warning("operation not completed", extra={**log, "state": reached})

    async def release_all(self, held: list[Operation]) -> None:
        """Give claims back on a graceful stop so a successor resumes without waiting."""
        async with self._engine.connect() as connection:
            for claimed in held:
                await operations.release(connection, claimed=claimed, worker=self._worker)
            await connection.commit()
