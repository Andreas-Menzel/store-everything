"""The worker loop: claim an operation, run its handler, heartbeat, transition.

This is the only place that turns queued intent into work. It is deliberately small, because
everything that makes the model safe lives in `operations.py`: the loop's job is to keep a
lease alive while a handler runs, and to translate the handler's outcome into one guarded
transition.

Three properties are worth stating, since each is a decision rather than an accident:

- **Polling is the durable path.** A claim writes nothing when nothing is claimable, so an
  idle instance is genuinely idle — the failure mode ADR-0013 calls out by name is a
  `NOTIFY` on a timer keeping the disk busy forever. A doorbell can be added later as an
  optimization over this loop; it can never replace it.
- **The heartbeat is also the cancellation channel.** The handler runs as a task while the
  loop renews the lease; when the renewal reports a cancellation, or fails because the lease
  was lost, the task is cancelled. A worker that has lost its lease must stop immediately —
  its writes would be fenced out anyway, so continuing only wastes work.
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
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from store_everything import operations
from store_everything.config import Settings
from store_everything.db import migrations_are_current
from store_everything.faults import fault_point
from store_everything.operations import Operation

_logger = logging.getLogger(__name__)


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


class PermanentFailureError(Exception):
    """Raised by a handler when retrying cannot help — a malformed payload, a missing root.

    Everything else is treated as retryable, which is the right default: a transient fault
    that is mistaken for permanent loses work, while a permanent fault mistaken for
    transient merely dead-letters a few attempts later.
    """


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

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.worker_poll_seconds
                )
            except TimeoutError:
                continue
        return False

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
        while not self._stopping.is_set():
            if not await self.run_once():
                # Nothing to do: wait, but wake immediately on shutdown rather than
                # sleeping through it.
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._settings.worker_poll_seconds
                    )
                except TimeoutError:
                    continue

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

        async with self._engine.connect() as connection:
            work = asyncio.create_task(handler(Job(operation=claimed, connection=connection)))
            keeper = asyncio.create_task(self._keep_lease(claimed, work))
            try:
                result = await work
            except asyncio.CancelledError:
                await self._on_cancelled(claimed, log)
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
                keeper.cancel()

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

    async def _keep_lease(self, claimed: Operation, work: asyncio.Task[Any]) -> None:
        """Renew the lease until the work finishes — and stop the work when told to."""
        while not work.done():
            await asyncio.sleep(self._heartbeat_every.total_seconds())
            if work.done():
                return
            async with self._engine.connect() as connection:
                beat = await operations.heartbeat(
                    connection, claimed=claimed, worker=self._worker, lease=self._lease
                )
                await connection.commit()
            if not beat.lease_extended or beat.cancel_requested:
                _logger.info(
                    "stopping work on request or lost lease",
                    extra={
                        "operation": str(claimed.id),
                        "lease_extended": beat.lease_extended,
                        "cancel_requested": beat.cancel_requested,
                    },
                )
                work.cancel()
                return

    async def _on_cancelled(self, claimed: Operation, log: dict[str, Any]) -> None:
        async with self._engine.connect() as connection:
            await operations.mark_cancelled(connection, claimed=claimed)
            await connection.commit()
        _logger.info("operation cancelled", extra=log)

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
