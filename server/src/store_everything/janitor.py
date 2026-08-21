"""Debris collection. Crashes leak by design; this is what makes that safe.

Every crash-only system leaks (ADR-0010): a process killed between writing bytes and
committing the row that references them leaves a file nobody will ever look up. The design
accepts that because the debris is **identifiable** — staging files carry the id of the
operation that wrote them — and **collected**, which is this module.

Two rules keep collection from becoming its own hazard:

- **The grace window.** A staging file is collected only once its operation is terminal (or
  gone) *and* the file is older than the window. Without the age check the janitor could
  delete a file an operation is still writing, in the gap between its `open()` and its
  row-commit.
- **Never collect what you cannot account for.** Unreferenced blobs are deletable only
  against a list of what *is* referenced. No such list exists until file versions do, and
  "no references found" must therefore mean "skip", not "delete everything" — so blob
  collection stays off until a reference source is registered.

Filesystem work runs in a thread, never on the event loop: a sweep over a staging directory
is thousands of `stat` calls, and the worker shares its loop with heartbeats that must not
be late. The pattern is deliberate and applies to every later caller of `filestore`.

The janitor is an ordinary leased operation, not a special background thread: claimed,
heartbeated and retried like any other work, and it re-arms its own schedule.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from store_everything import filestore, operations
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.runner import Job

_logger = logging.getLogger(__name__)

KIND = "maintenance.janitor"

ReferenceSource = Callable[[], Iterable[str]]
"""Returns every blob digest something still points at. Registered by the feature that
creates the references; until one exists, blobs are never collected."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A staging file old enough to consider, and the operation that wrote it."""

    path: Path
    operation_id: UUID | None


def staging_roots(settings: Settings) -> tuple[Path, ...]:
    """The app-owned staging areas.

    Workspace staging (`.workspace/staging/`) joins this list when workspaces exist
    (ADR-0018); it is deliberately not guessed at here.
    """
    return (settings.versions_root / "staging", settings.derived_root / "staging")


def _aged_candidates(roots: tuple[Path, ...], *, grace: timedelta) -> tuple[list[Candidate], int]:
    """Staging entries past the grace window, with how many were looked at. Blocking."""
    cutoff = time.time() - grace.total_seconds()
    candidates: list[Candidate] = []
    inspected = 0

    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_file() or not filestore.is_staging_entry(entry):
                continue
            inspected += 1
            try:
                if entry.stat().st_mtime >= cutoff:
                    continue
            except FileNotFoundError:
                # Collected by a concurrent janitor between listing and stat. Fine: the work
                # is done either way.
                continue
            candidates.append(Candidate(entry, filestore.operation_of_staging_entry(entry)))

    return candidates, inspected


def _remove_all(paths: Iterable[Path]) -> int:
    return sum(1 for path in paths if filestore.remove(path))


def _collect_blobs(root: Path, referenced: set[str], *, grace: timedelta) -> int:
    """Unlink unreferenced blobs past the grace window. Blocking."""
    store = BlobStore(root)
    cutoff = time.time() - grace.total_seconds()
    removed = 0

    for digest in store.iter_digests():
        if digest in referenced:
            continue
        try:
            # Age matters even for an unreferenced blob: it may have been written seconds ago
            # by an operation whose row has not committed yet (bytes first, then the row).
            if store.path_for(digest).stat().st_mtime >= cutoff:
                continue
        except FileNotFoundError:
            continue
        if store.remove(digest):
            removed += 1
    return removed


async def collect(
    job: Job,
    *,
    settings: Settings,
    references: ReferenceSource | None = None,
) -> dict[str, Any]:
    """Sweep debris older than the grace window. Idempotent, and safe to run concurrently."""
    grace = timedelta(hours=settings.janitor_grace_hours)

    candidates, inspected = await asyncio.to_thread(
        _aged_candidates, staging_roots(settings), grace=grace
    )

    # One query for every candidate rather than one per file: a 10 TB import can leave a lot
    # of staging behind, and the janitor must not turn that into a round-trip storm.
    owners = [candidate.operation_id for candidate in candidates if candidate.operation_id]
    states = await operations.states_of(job.connection, owners)

    doomed = [
        candidate.path
        for candidate in candidates
        # An unrecognised name has only its age to go by, and that has already passed. An
        # operation that no longer exists cannot come back for its file — terminal rows are
        # pruned (12 § queue hygiene) — so absence counts as terminal.
        if candidate.operation_id is None
        or states.get(candidate.operation_id, "succeeded") in operations.TERMINAL_STATES
    ]
    collected = await asyncio.to_thread(_remove_all, doomed)

    blobs_collected = 0
    if references is None:
        # Not an error: it is the honest answer before anything references a blob. Collecting
        # against an empty reference list would empty the store holding the only copy of
        # every superseded version.
        _logger.debug("blob collection skipped: no reference source is registered")
    else:
        blobs_collected = await asyncio.to_thread(
            _collect_blobs, settings.versions_root, set(references()), grace=grace
        )

    await operations.ensure_scheduled(
        job.connection,
        kind=KIND,
        max_attempts=3,
        due_in=timedelta(minutes=settings.janitor_interval_minutes),
        priority=operations.PRIORITY_HEAVY,
    )
    return {
        "staging_inspected": inspected,
        "staging_collected": collected,
        "blobs_collected": blobs_collected,
    }
