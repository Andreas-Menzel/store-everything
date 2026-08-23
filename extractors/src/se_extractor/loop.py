"""The worker loop every extractor runs: register, claim, work, report. Forever.

An author writes a handler and nothing else. What this loop owns is the part that is easy to get
subtly wrong and identical for everybody:

- **registering before working**, and waiting patiently while the core is still starting — a
  container comes up before the instance does, and that is not an error;
- **heartbeating while the handler works**, in a thread, because the work is blocking. The
  heartbeat is also the cancellation channel, so the handler is *told* to stop rather than
  killed (12 § leases & fencing);
- **losing a lease gracefully.** A handler that overran its lease has its writes refused; the
  loop treats that as "somebody else has it now", drops the work and claims again;
- **failing usefully.** An unhandled exception is a retryable failure; raising
  `PermanentFailure` is how a handler says "this input will never work" and skips the retries.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from se_extractor.client import ContractError, ExtractorClient, LeaseLost, Unavailable
from se_extractor.models import Job

_logger = logging.getLogger("se_extractor")

#: How long a claim waits for work before asking again. The core caps it; asking for the cap
#: means an idle extractor makes two requests a minute rather than sixty.
DEFAULT_CLAIM_WAIT = 30

#: How long to wait before retrying an unreachable core, and the ceiling for that backoff.
_RETRY_SECONDS = 2.0
_RETRY_CEILING = 60.0


class PermanentFailure(Exception):  # noqa: N818 - the outcome it names, not a bug
    """Raise from a handler for input this extractor can never process. No retries follow."""


@dataclass
class JobContext:
    """What a handler is given besides the job: the answer to "should I still be doing this?"."""

    client: ExtractorClient
    _cancelled: threading.Event

    @property
    def cancelled(self) -> bool:
        """True once the core has asked for this job to stop — check it in long loops."""
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled


class Cancelled(Exception):  # noqa: N818 - a state, and `CancelledError` is asyncio's
    """The core asked for this job to stop. Not a failure: nothing is reported."""


Handler = Callable[[Job, JobContext], dict[str, Any] | None]
"""A handler does the work and returns its result envelope (or `None` for no outputs)."""


class Worker:
    """One extractor's loop. `run()` blocks; `stop()` ends it after the current job."""

    def __init__(
        self,
        client: ExtractorClient,
        manifest: dict[str, Any],
        handler: Handler,
        *,
        claim_wait: int = DEFAULT_CLAIM_WAIT,
        worker_name: str | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._manifest = manifest
        self._handler = handler
        self._claim_wait = claim_wait
        self._worker_name = worker_name
        self._stopping = threading.Event()
        self._sleep = sleep or self._wait

    def stop(self) -> None:
        """Ask the loop to end. Safe from a signal handler or another thread."""
        self._stopping.set()

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake immediately if we are asked to stop."""
        self._stopping.wait(seconds)

    # ------------------------------------------------------------------ the loop

    def register(self) -> None:
        """Declare the manifest, retrying while the core is not there yet.

        A `ContractError` is *not* retried: a manifest the core refuses will be refused again,
        and an extractor that spins on it forever hides the message that says why.
        """
        backoff = _RETRY_SECONDS
        while not self._stopping.is_set():
            try:
                registered = self._client.register(self._manifest)
            except Unavailable as unreachable:
                _logger.info("core not ready (%s); retrying in %.0fs", unreachable, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, _RETRY_CEILING)
                continue
            if registered.changed:
                _logger.info("registered %s", registered.extractor_id)
            if not registered.enabled:
                _logger.warning(
                    "%s is registered but disabled; no work will be routed to it",
                    registered.extractor_id,
                )
            return

    def run(self, *, once: bool = False) -> None:
        """Register, then work until stopped. `once` returns after a single claim attempt."""
        self.register()
        while not self._stopping.is_set():
            try:
                job = self._client.claim(wait=self._claim_wait, worker=self._worker_name)
            except Unavailable as unreachable:
                _logger.warning("could not claim (%s); retrying", unreachable)
                self._sleep(_RETRY_SECONDS)
                if once:
                    return
                continue
            except ContractError as refused:
                # A disabled extractor lands here, which is a state an operator has to fix.
                _logger.warning("claiming was refused: %s", refused)
                self._sleep(_RETRY_CEILING)
                if once:
                    return
                continue

            if job is not None:
                self._perform(job)
            if once:
                return

    # ------------------------------------------------------------------ one job

    def _perform(self, job: Job) -> None:
        cancelled = threading.Event()
        if job.cancel_requested:
            # Superseded before it was even claimed. Nothing to do and nothing to report.
            _logger.info("job %s was already cancelled; skipping", job.id)
            cancelled.set()
            return

        context = JobContext(client=self._client, _cancelled=cancelled)
        beating = threading.Thread(
            target=self._beat, args=(job, cancelled), name=f"heartbeat-{job.id}", daemon=True
        )
        beating.start()
        try:
            result = self._handler(job, context)
        except Cancelled:
            _logger.info("job %s was cancelled; leaving it", job.id)
            return
        except PermanentFailure as permanent:
            self._report(job, str(permanent) or "permanent failure", retryable=False)
            return
        except Exception as failed:
            _logger.exception("job %s failed", job.id)
            self._report(job, f"{type(failed).__name__}: {failed}", retryable=True)
            return
        finally:
            # Whatever happened, stop heartbeating for a job we are no longer working on.
            cancelled.set()
            beating.join(timeout=5)

        try:
            self._client.submit(job, result)
        except LeaseLost:
            _logger.warning("job %s finished after its lease lapsed; it will be re-run", job.id)
        except (ContractError, Unavailable) as unsubmitted:
            # The work is done and the core did not take it. Saying nothing is right: the lease
            # lapses and the job comes back, which is what at-least-once delivery means.
            _logger.warning("job %s could not be submitted (%s)", job.id, unsubmitted)

    def _report(self, job: Job, message: str, *, retryable: bool) -> None:
        try:
            self._client.report_error(job, message, retryable=retryable)
        except LeaseLost:
            _logger.warning("job %s failed after its lease lapsed", job.id)
        except (ContractError, Unavailable) as unreported:
            _logger.warning("job %s failure could not be reported (%s)", job.id, unreported)

    def _beat(self, job: Job, cancelled: threading.Event) -> None:
        """Keep the lease while the handler works, and pass on a request to stop.

        Half the interval the core asked for, so one lost beat is not a lost lease.
        """
        interval = max(job.heartbeat_interval_seconds / 2, 1.0)
        while not cancelled.wait(interval):
            try:
                beat = self._client.heartbeat(job)
            except LeaseLost:
                _logger.warning("lost the lease on job %s; asking the handler to stop", job.id)
                cancelled.set()
                return
            except (ContractError, Unavailable) as unreachable:
                # A missed beat is survivable; the lease outlives several of them.
                _logger.debug("heartbeat for %s failed (%s)", job.id, unreachable)
                continue
            if beat.cancel:
                _logger.info("job %s was cancelled; asking the handler to stop", job.id)
                cancelled.set()
                return


def run(
    client: ExtractorClient,
    manifest: dict[str, Any],
    handler: Handler,
    *,
    claim_wait: int = DEFAULT_CLAIM_WAIT,
    worker_name: str | None = None,
    once: bool = False,
) -> None:
    """Run one extractor to completion — the whole of a `main()` for most images."""
    Worker(client, manifest, handler, claim_wait=claim_wait, worker_name=worker_name).run(once=once)
