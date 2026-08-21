"""Walking a workspace's tree and registering what is in it.

This is [F-001](../../../features/F-001-upload-and-import.md)'s import: point a workspace at a
directory — a NAS share with a decade of photos in it — and the app registers every file
without moving, renaming or modifying anything. The same operation is the re-scan, and
[ADR-0019](../../../decisions/ADR-0019-source-tree-semantics.md)'s three triggers (the hourly
schedule, a manual request, later a watcher event) are all just reasons for it to run now.

**The shape is a crawler's, because a 10 TB import cannot be one transaction.** One batch is
one directory: list it, register its files, discover its subdirectories, and delete its own
frontier row — committed together, so a `kill -9` costs at most that directory's work and the
next claim resumes from the frontier
([12 § job atomicity](../../../specs/12-reliability.md#job-atomicity)). No in-memory state
survives a batch, which is what makes "resumable" a property of the schema rather than a hope.

Four rules govern what the traversal does with what it finds, and all four refuse rather than
repair — the user's files are never touched to make the app's model tidier:

1. **Symlinks are skipped, never followed** — recorded in the report and turned into nothing
   (F-001/FR-12). The tree behind a symlinked directory is never traversed, so loops cannot
   arise, and every path is re-resolved and re-checked for containment before it is opened.
2. **Siblings colliding on the comparison key are conflicts.** The first in the deterministic
   traversal order registers; the rest are reported with both spellings (F-001/FR-11). This is
   the macOS-over-SMB case: one visible name, two byte sequences.
3. **A name the policy refuses is skipped and reported** — over 255 bytes, or carrying a
   control character. Failing predictably beats failing at the filesystem's whim.
4. **`.workspace` is ours and invisible.** It is skipped silently rather than reported: it is
   not a fact about the user's tree (F-001/FR-13, ADR-0018).

A directory that is *gone* stays on the frontier deliberately: popping it yields an empty
listing, which is how a subtree deleted on disk is noticed at any depth without a special
case. Acting on that — trashing what is missing, versioning what changed — is the next chunk;
this one registers what is there and stamps what it saw.

**Known limit:** one directory's entries are listed and sorted in memory, so a single
directory with millions of entries is a memory cost. Sorting is not optional — rule 2 is
stated in terms of a deterministic order — and chunking a directory would need a cursor
*within* a directory, which no real tree has yet demanded.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import (
    events,
    files,
    filestore,
    folders,
    mediatypes,
    names,
    scans,
    workspaces,
)
from store_everything.events import Actor
from store_everything.faults import fault_point
from store_everything.runner import Job, PermanentFailureError

_logger = logging.getLogger(__name__)

#: Re-exported for readability at the call sites in this module.
KIND = scans.KIND


@dataclass(frozen=True, slots=True)
class Entry:
    """One regular file as the filesystem described it."""

    name: str
    size: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class Listing:
    """What one directory looked like. Produced off the event loop, consumed on it."""

    directories: tuple[str, ...] = ()
    files: tuple[Entry, ...] = ()
    findings: tuple[scans.Finding, ...] = ()
    #: The directory is not there any more. Its contents are *gone*, which is a fact.
    missing: bool = False
    #: The directory is there and could not be read. Its contents are *unknown*, which is not
    #: the same fact, and must never be reconciled as if everything in it had been deleted.
    unreadable: bool = False


@dataclass(slots=True)
class Tally:
    """What one batch changed, for the run's counters."""

    files_seen: int = 0
    files_registered: int = 0
    conflicts: int = 0
    skipped: int = 0
    findings: list[scans.Finding] = field(default_factory=list)


# --------------------------------------------------------------------- the filesystem


def _child_path(parent: str, name: str) -> str:
    return name if parent == scans.ROOT else f"{parent}/{name}"


def inspect(root: Path, relative: str) -> Listing:
    """List one directory, sorted, without following a single symlink. **Blocking.**

    Containment is re-verified here even though the path came from our own frontier: a
    directory that became a symlink to somewhere else between two scans is exactly the case
    lexical checks miss (ADR-0019, the File Browser CVEs).
    """
    try:
        target = filestore.resolve_within(root, Path(relative)) if relative else root.resolve()
    except filestore.ContainmentError:
        return Listing(
            findings=(
                scans.Finding("skipped", relative, "this path resolves outside the workspace"),
            ),
            unreadable=True,
        )

    try:
        with os.scandir(target) as scanning:
            entries = sorted(scanning, key=lambda entry: entry.name)
    except FileNotFoundError:
        return Listing(missing=True)
    except NotADirectoryError:
        return Listing(findings=(scans.Finding("skipped", relative, "no longer a directory"),))
    except PermissionError as denied:
        return Listing(
            findings=(scans.Finding("skipped", relative, f"cannot be read: {denied.strerror}"),),
            unreadable=True,
        )

    return classify(relative, entries)


class DirectoryEntry(Protocol):
    """The part of `os.DirEntry` the classifier uses.

    A protocol rather than the concrete type for one reason that matters: the collision rule
    — two sibling names the app cannot tell apart — can only be *created* on a filesystem
    that is case-sensitive and byte-preserving, so on a developer's macOS it is untestable
    against a real directory. The rule is too important to be covered only on CI's ext4.
    """

    @property
    def name(self) -> str: ...

    def is_symlink(self) -> bool: ...

    def is_dir(self, *, follow_symlinks: bool = True) -> bool: ...

    def is_file(self, *, follow_symlinks: bool = True) -> bool: ...

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result: ...


def classify(relative: str, entries: Sequence[DirectoryEntry]) -> Listing:
    """Sort one directory's entries into what to register, recurse into, and report.

    Order is the caller's: the entries arrive sorted, and "the first in the deterministic
    traversal order registers" (ADR-0019) is stated in terms of that.
    """
    directories: list[str] = []
    files_found: list[Entry] = []
    findings: list[scans.Finding] = []
    at_root = relative == scans.ROOT
    #: Comparison key → the name that claimed it, so a collision can name both spellings.
    claimed: dict[str, str] = {}

    for entry in entries:
        path = _child_path(relative, entry.name)

        # Never dereferenced, whatever it points at — including a dangling link.
        if entry.is_symlink():
            findings.append(scans.Finding("skipped", path, "symbolic links are never followed"))
            continue
        if at_root and names.comparison_key(entry.name) == names.comparison_key(
            names.CONTROL_DIRECTORY
        ):
            # Ours, not the user's. Silently invisible rather than reported as a fact about
            # their tree (F-001/FR-13).
            continue

        # Names arrive from the filesystem as-is, which is how they are stored — the key is
        # derived from them (ADR-0019). Normalization happens only on the API side.
        try:
            names.validate_name(entry.name)
        except names.InvalidNameError as invalid:
            findings.append(scans.Finding("skipped", path, f"unusable name: {invalid.reason}"))
            continue

        key = names.comparison_key(entry.name)
        if key in claimed:
            findings.append(
                scans.Finding(
                    "conflict",
                    path,
                    f"collides with {claimed[key]!r}: the two names differ only in case "
                    "or in Unicode normalization, so the app cannot tell them apart",
                )
            )
            continue
        claimed[key] = entry.name

        try:
            if entry.is_dir(follow_symlinks=False):
                directories.append(entry.name)
                continue
            if not entry.is_file(follow_symlinks=False):
                findings.append(scans.Finding("skipped", path, "not a regular file or directory"))
                continue
            stat = entry.stat(follow_symlinks=False)
        except OSError as failure:
            # Vanished mid-listing, or unreadable. Either way it is not registrable now, and
            # the next pass reconciles: scans are convergent, not snapshot-perfect.
            findings.append(
                scans.Finding("skipped", path, f"could not be read: {failure.strerror}")
            )
            continue

        files_found.append(
            Entry(
                name=entry.name,
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            )
        )

    return Listing(
        directories=tuple(directories),
        files=tuple(files_found),
        findings=tuple(findings),
    )


def _digest(root: Path, relative: str) -> str:
    """Hash a file, re-checking containment before opening it. **Blocking.**"""
    return filestore.digest_of_file(filestore.resolve_within(root, Path(relative)))


# ------------------------------------------------------------------------- the batch


async def process_directory(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    workspace: workspaces.Workspace,
    pending: scans.Pending,
) -> Tally:
    """Register one directory's contents. The caller commits, which is the checkpoint."""
    listing = await asyncio.to_thread(inspect, workspace.root_path, pending.path)
    tally = Tally(findings=list(listing.findings))
    tally.conflicts = sum(1 for finding in listing.findings if finding.kind == "conflict")
    tally.skipped = sum(1 for finding in listing.findings if finding.kind == "skipped")

    if listing.missing or listing.unreadable:
        # Nothing to register. What a *missing* directory implies about the files we know are
        # in it belongs to reconciliation; an *unreadable* one implies nothing at all.
        return tally

    folder = await folders.get(connection, pending.folder_id)
    if folder is None:
        # The folder was removed underneath us — a concurrent delete, once deletion exists.
        # Its frontier row goes with it; there is nothing to file into.
        return tally

    discovered: list[scans.Pending] = []
    for name in listing.directories:
        child = await folders.ensure_child(
            connection,
            workspace_id=workspace.id,
            parent=folder,
            name=name,
            actor=Actor.system(),
        )
        discovered.append(scans.Pending(_child_path(pending.path, name), child.id))
    await scans.push(connection, run_id=run.id, pending=discovered)

    for entry in listing.files:
        tally.files_seen += 1
        if await _register(
            connection,
            run=run,
            workspace=workspace,
            folder_id=folder.id,
            path=pending.path,
            entry=entry,
        ):
            tally.files_registered += 1

    return tally


async def _register(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    workspace: workspaces.Workspace,
    folder_id: UUID,
    path: str,
    entry: Entry,
) -> bool:
    """Register a file the app has not seen, or stamp one it already knows. Returns whether
    it registered.

    Stamping is what makes the *next* pass able to tell "still there" from "gone" with one
    indexed comparison, so every file the scan sees is stamped whether or not anything else
    about it changed. Deciding that a known file **changed** is reconciliation's job, and this
    chunk deliberately leaves an existing row alone otherwise — it does not read the bytes of
    a file it already knows, which is what keeps an hourly pass over 10 TB cheap.
    """
    known = await files.find_in_folder(connection, folder_id=folder_id, name=entry.name)
    if known is not None:
        await files.mark_seen(connection, file_id=known.id, seen_at=run.started_at)
        return False

    relative = _child_path(path, entry.name)
    try:
        content_hash = await asyncio.to_thread(_digest, workspace.root_path, relative)
    except (OSError, filestore.ContainmentError) as unreadable:
        _logger.info(
            "skipped a file that could not be hashed",
            extra={"workspace": str(workspace.id), "path": relative, "reason": str(unreadable)},
        )
        return False

    await files.register(
        connection,
        workspace_id=workspace.id,
        folder_id=folder_id,
        name=entry.name,
        content_hash=content_hash,
        size_bytes=entry.size,
        media_type=mediatypes.detect(entry.name, None),
        modified_at=entry.modified_at,
        origin="external",
        last_seen_at=run.started_at,
        actor=Actor.system(),
    )
    return True


# ---------------------------------------------------------------------- the operation


async def scan(job: Job) -> dict[str, Any]:
    """Traverse a workspace, one committed directory at a time.

    Every batch commits on its own — this is 12 § job atomicity's declared exception to "one
    transaction per operation" — so the operation's own success transition covers only the
    last one. Everything before it is already durable, which is the point.
    """
    workspace_id = job.operation.subject_id
    if workspace_id is None:
        raise PermanentFailureError(f"{KIND} needs a workspace as its subject")

    workspace = await workspaces.get(job.connection, workspace_id)
    if workspace is None:
        return {"workspace": str(workspace_id), "outcome": "gone"}
    if not workspace.is_active:
        # Provisioning arms the first scan, so this means the workspace never finished being
        # built. Retrying is right: the provisioning operation is still working on it.
        raise OSError(f"workspace {workspace_id} is not active yet")

    root_path = str(job.payload.get("path") or scans.ROOT)
    try:
        root_folder = await _root_folder(job.connection, workspace_id=workspace.id, path=root_path)
    except names.InvalidNameError as invalid:
        raise PermanentFailureError(
            f"{KIND} was given an unusable path: {invalid.reason}"
        ) from invalid
    if root_folder is None:
        raise PermanentFailureError(f"{KIND} was asked to scan an unknown path: {root_path!r}")

    run = await scans.start(
        job.connection,
        workspace_id=workspace.id,
        operation_id=job.operation.id,
        trigger=job.payload.get("trigger", "scheduled"),
        root_folder_id=root_folder.id,
        root_path=root_path,
    )
    await job.connection.commit()

    processed = 0
    while True:
        pending = await scans.next_directory(job.connection, run.id)
        if pending is None:
            break

        tally = await process_directory(
            job.connection, run=run, workspace=workspace, pending=pending
        )
        await scans.report(job.connection, run_id=run.id, findings=tally.findings)
        await scans.record_progress(
            job.connection,
            run_id=run.id,
            directories=1,
            files_seen=tally.files_seen,
            files_registered=tally.files_registered,
            conflicts=tally.conflicts,
            skipped=tally.skipped,
        )
        await scans.complete_directory(job.connection, run_id=run.id, path=pending.path)
        # The checkpoint: this directory's registrations, its discoveries and its removal from
        # the frontier become durable together. The fault points bracket exactly that seam,
        # because it is the one place a crash could apply a batch without recording it — or
        # record it twice (12 § verification).
        fault_point("scan.after-batch")
        await job.connection.commit()
        fault_point("scan.after-commit")
        processed += 1

    finished = await scans.get(job.connection, run.id)
    await scans.finish(job.connection, run_id=run.id, state="completed")
    await _rearm(job.connection, workspace=workspace)
    await events.record(
        job.connection,
        action=events.WORKSPACE_SCANNED,
        resource_type=events.RESOURCE_WORKSPACE,
        resource_id=workspace.id,
        actor=Actor.system(),
        details={
            "run": str(run.id),
            "trigger": run.trigger,
            "path": root_path,
            "directories": processed,
            "files_seen": 0 if finished is None else finished.files_seen,
            "files_registered": 0 if finished is None else finished.files_registered,
            "conflicts": 0 if finished is None else finished.conflicts,
            "skipped": 0 if finished is None else finished.skipped,
        },
    )
    return {
        "workspace": str(workspace.id),
        "run": str(run.id),
        "outcome": "completed",
        "directories": processed,
        "files_registered": 0 if finished is None else finished.files_registered,
    }


async def _root_folder(
    connection: AsyncConnection, *, workspace_id: UUID, path: str
) -> folders.Folder | None:
    if not path:
        return await folders.root_of(connection, workspace_id)
    return await folders.resolve(
        connection, workspace_id=workspace_id, segments=names.split_path(path)
    )


async def _rearm(connection: AsyncConnection, *, workspace: workspaces.Workspace) -> None:
    """Queue this workspace's next scheduled scan, in the transaction that completes this one.

    A subtree rescan re-arms the *workspace* schedule too: the schedule is a property of the
    workspace, not of the run that happened to notice it was due.
    """
    await scans.ensure_scheduled(
        connection,
        workspace_id=workspace.id,
        due_in=timedelta(minutes=workspace.scan_interval_minutes),
    )


async def ensure_all_scheduled(connection: AsyncConnection) -> int:
    """Re-assert a scan schedule for every active workspace. Returns how many were armed.

    The floor under the self-re-arming chain (12 § operation inventory): a run queues its own
    successor, and if a run ever dead-letters that chain stops. Called at start-up and from
    the janitor's periodic sweep, so a workspace cannot end up silently never scanned again.
    """
    armed = 0
    for workspace in await workspaces.list_active(connection):
        await scans.ensure_scheduled(connection, workspace_id=workspace.id)
        armed += 1
    return armed
