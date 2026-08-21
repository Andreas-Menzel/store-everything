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
- **An open upload session is not debris**, however old its staging file looks: a client has
  days to come back for it (ADR-0017). So this sweep first expires the sessions that are
  genuinely past their deadline, and then leaves every still-open one alone.

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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import filestore, operations, scanning, uploads, workspaces
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.runner import Job

_logger = logging.getLogger(__name__)

KIND = "maintenance.janitor"

ReferenceSource = Callable[[AsyncConnection], Awaitable[Iterable[str]]]
"""Returns every blob digest something still points at, given a connection to ask.

Takes the connection because the answer is a query — `files.restorable_digests` — and the
sweep must read it *inside* the job's transaction, immediately before deciding what is
unreferenced. A snapshot taken any earlier could miss a version created while the sweep was
walking the store."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A staging file old enough to consider, and the operation that wrote it."""

    path: Path
    operation_id: UUID | None


def staging_roots(settings: Settings) -> tuple[Path, ...]:
    """The app-owned staging areas — the ones that need no database to find."""
    return (settings.versions_root / "staging", settings.derived_root / "staging")


async def all_staging_roots(connection: AsyncConnection, settings: Settings) -> tuple[Path, ...]:
    """Every staging area on this instance, app-owned and per workspace.

    A workspace stages inside the user's own tree, because committing a write has to be a
    rename on the destination filesystem (ADR-0018) — so those paths are known only to the
    database, and debris there is as much this module's business as debris on the app volume.
    """
    return staging_roots(settings) + await workspaces.staging_roots(connection)


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

    # Before deciding what is debris, decide what has stopped being live. An upload past its
    # deadline is the only thing here that expires on a clock rather than on a state change.
    sessions_expired = await uploads.expire_due(job.connection)
    # The floor under every workspace's scan schedule. A run re-arms itself, so this matters
    # only when that chain broke — a dead-lettered scan — but the cost is one query per sweep
    # and the alternative is a workspace that silently stops being scanned.
    await scanning.ensure_all_scheduled(job.connection)

    roots = await all_staging_roots(job.connection, settings)
    candidates, inspected = await asyncio.to_thread(_aged_candidates, roots, grace=grace)

    # One query for every candidate rather than one per file: a 10 TB import can leave a lot
    # of staging behind, and the janitor must not turn that into a round-trip storm.
    owners = [candidate.operation_id for candidate in candidates if candidate.operation_id]
    states = await operations.states_of(job.connection, owners)
    # Staging in a workspace is named after an upload session rather than an operation, and an
    # open one is live data: without this the sweep would eat a paused upload's bytes.
    live_sessions = await uploads.open_ids(job.connection, owners)

    doomed = [
        candidate.path
        for candidate in candidates
        if candidate.operation_id not in live_sessions
        # An unrecognised name has only its age to go by, and that has already passed. An
        # operation that no longer exists cannot come back for its file — terminal rows are
        # pruned (12 § queue hygiene) — so absence counts as terminal.
        and (
            candidate.operation_id is None
            or states.get(candidate.operation_id, "succeeded") in operations.TERMINAL_STATES
        )
    ]
    collected = await asyncio.to_thread(_remove_all, doomed)

    blobs_collected = 0
    if references is None:
        # Not an error: it is the honest answer before anything references a blob. Collecting
        # against an empty reference list would empty the store holding the only copy of
        # every superseded version.
        _logger.debug("blob collection skipped: no reference source is registered")
    else:
        referenced = set(await references(job.connection))
        blobs_collected = await asyncio.to_thread(
            _collect_blobs, settings.versions_root, referenced, grace=grace
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
        "sessions_expired": sessions_expired,
    }
