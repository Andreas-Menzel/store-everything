"""What a re-scan concludes about files it already knew.

`scanning` walks the tree and checkpoints; this decides what the tree *means*.
[F-001/FR-6](../../../features/F-001-upload-and-import.md) allows exactly four conclusions
about a file the app already holds:

| On disk | Conclusion |
|---|---|
| same size and mtime | nothing — not even a read |
| size or mtime differ, same hash | refresh the recorded timestamp, so the next pass is cheap |
| a different hash | a new current version, and the predecessor loses its bytes |
| absent from a directory that *was* read | a trash entry, badged "removed outside the app" |

The third and fourth rows are the ones with teeth. A predecessor is marked `restorable: false`
unless the app happens to hold those bytes already
([F-007/FR-9](../../../features/F-007-versioning.md)) — nothing was snapshotted, because the
overwrite happened on the storage before the app knew of it. A trash entry is
[F-014/FR-10](../../../features/F-014-deletion-and-trash.md): never a silent drop from the
index.

And one conclusion about content the app has not seen at that path before: if its hash matches a
file whose own path is gone, that file **moved** — same UUID, no deletion, no registration
(02 § file). Everything the app attaches to a file hangs off its UUID, so recognising a move is
what makes an external rename cost nothing.

Two rules keep this from being dangerous, because the failure mode is destroying the index of a
storage that was merely unreachable:

1. **Only an absence concludes a deletion** (F-001/FR-18). A file is trashed only when a
   directory that was successfully listed — or confirmed gone — did not mention its name. An
   entry the scan saw and refused (a symlink, the loser of a name collision, something it could
   not `stat`) keeps the file live and is reported as a finding instead.
2. **"I could not look" is never "it is not there"** (F-001/FR-16). A directory the scan could
   not read is recorded in `scan_blocked`, and its whole subtree is excluded from the sweep.

**Nothing here writes a byte.** Reconciliation reads the tree and writes rows: the bytes of a
file changed on the storage were overwritten before the app ever saw them, which is exactly why
the predecessor is not restorable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import (
    aggregates,
    events,
    files,
    filestore,
    folders,
    mediatypes,
    names,
    scans,
    trash,
    workspaces,
)
from store_everything.blobs import BlobStore
from store_everything.events import Actor
from store_everything.tables import file as file_table
from store_everything.tables import folder as folder_table
from store_everything.tables import trash_entry

_logger = logging.getLogger(__name__)

#: How many vanished files one sweep batch trashes before committing. Large enough that deleting
#: a folder of ten thousand photos is a handful of transactions, small enough that a crash costs
#: one of them.
SWEEP_BATCH = 500

#: How many same-content candidates a move is willing to consider. A tree can hold thousands of
#: byte-identical files; the ones that matter are the few this run has not seen, and a cap keeps
#: a pathological case from turning one registration into thousands of `lstat` calls.
CANDIDATE_LIMIT = 32


@dataclass(frozen=True, slots=True)
class Entry:
    """One regular file as the filesystem described it."""

    name: str
    size: int
    modified_at: datetime


@dataclass(slots=True)
class Outcome:
    """What reconciling one directory concluded, for the run's counters."""

    seen: int = 0
    registered: int = 0
    changed: int = 0
    moved: int = 0
    restored: int = 0


@dataclass(frozen=True, slots=True)
class Candidate:
    """A live file whose content matches an entry, and whose own path is gone."""

    file: files.File
    version: files.Version
    path: str


# ------------------------------------------------------------------ reading the disk


def _digest(root: Path, relative: str) -> str:
    """Hash a file, re-checking containment before opening it. **Blocking.**"""
    return filestore.digest_of_file(filestore.resolve_within(root, Path(relative)))


def is_gone(root: Path, relative: str) -> bool:
    """Whether nothing is at this path any more. **Blocking.**

    Public because it is a rule, not a detail: it is the whole difference between recognising a
    move and handing one file's identity to another file's content. Only two errors mean *gone*
    — nothing is there, or something on the way stopped being a directory. Anything else
    (permission denied, a path that now resolves outside the workspace) means the app could not
    look, and a move may no more be concluded from a failure to look than a deletion may
    (F-001/FR-16).
    """
    try:
        resolved = filestore.resolve_within(root, Path(relative))
    except filestore.ContainmentError:
        return False
    try:
        os.lstat(resolved)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _unchanged(version: files.Version, entry: Entry) -> bool:
    """The stat-scan's question: is there any reason to read this file at all?

    Size *and* timestamp, because either alone is too weak: an edit that preserves length is
    ordinary in a document, and a restore from backup preserves the timestamp. A version with
    no recorded timestamp — nothing writes one today — is treated as worth reading.
    """
    return (
        version.modified_at is not None
        and version.size_bytes == entry.size
        and version.modified_at == entry.modified_at
    )


# ---------------------------------------------------------------- the move heuristic


async def moved_from(
    connection: AsyncConnection,
    *,
    workspace: workspaces.Workspace,
    started_at: datetime,
    name: str,
    content_hash: str,
) -> tuple[Candidate, str] | None:
    """The file this content moved away from, and which rule matched — or `None` if it is new.

    Most specific first, because content alone is not always unique:

    1. **Hash.** One candidate whose path is gone: that file moved.
    2. **Hash and name.** Several candidates — byte-identical files — so prefer the one that
       *kept its name*. Sibling names are unique within a folder, so a directory renamed on the
       storage re-matches every one of its files this way, however many of them share content.
    3. **The oldest registration.** Still several: same content, same name, different vanished
       directories. The choice is arbitrary because the candidates are indistinguishable to a
       person too, so it is made deterministically and recorded in the event. Matching is still
       better than losing identity, which would cost the tags and grants a later phase hangs on
       it.
    """
    candidates = await files.candidates_with_content(
        connection,
        workspace_id=workspace.id,
        content_hash=content_hash,
        unseen_since=started_at,
        limit=CANDIDATE_LIMIT,
    )
    if not candidates:
        return None

    absent: list[Candidate] = []
    for candidate in candidates:
        path = await files.path_of(connection, candidate.file)
        if await asyncio.to_thread(is_gone, workspace.root_path, path):
            absent.append(Candidate(candidate.file, candidate.version, path))
    if not absent:
        return None
    if len(absent) == 1:
        return absent[0], "hash"

    key = names.comparison_key(name)
    same_name = [found for found in absent if names.comparison_key(found.file.name) == key]
    if len(same_name) == 1:
        return same_name[0], "hash+name"

    # Candidates arrive oldest-registration-first, so this is stable across retries.
    shortlist = same_name or absent
    return shortlist[0], f"oldest-of-{len(shortlist)}"


# ------------------------------------------------------------------ one directory


async def directory(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    workspace: workspaces.Workspace,
    folder_id: UUID,
    path: str,
    entries: tuple[Entry, ...],
    mentioned: tuple[str, ...],
    store: BlobStore,
) -> Outcome:
    """Apply one directory's listing to the rows the app holds for it.

    The stamp comes first, and deliberately: a file this run has already seen is not a candidate
    for anything that moved, so stamping up front is both the record of "still there" and the
    cheapest way to keep the move search from considering files that are plainly present.
    """
    await files.stamp_seen(
        connection, folder_id=folder_id, name_keys=mentioned, seen_at=run.started_at
    )

    known = await files.in_folder(connection, folder_id)
    live: dict[str, files.Known] = {}
    trashed: dict[str, files.Known] = {}
    for holding in known:
        key = names.comparison_key(holding.file.name)
        # Ordered oldest first, so on a repeated key the newest row wins — the same answer
        # `files.find_in_folder` gives.
        (live if holding.file.is_live else trashed)[key] = holding

    outcome = Outcome()
    for entry in entries:
        outcome.seen += 1
        key = names.comparison_key(entry.name)
        relative = f"{path}/{entry.name}" if path else entry.name

        existing = live.get(key)
        if existing is not None:
            if _unchanged(existing.version, entry):
                continue
            digest = await _hash(workspace, relative)
            if digest is None:
                continue
            if digest == existing.version.content_hash:
                # A touch, or a restore from backup: the same bytes wearing a new timestamp.
                await files.refresh_observed_mtime(
                    connection, version_id=existing.version.id, modified_at=entry.modified_at
                )
                continue
            await _new_version(
                connection, existing=existing, entry=entry, digest=digest, store=store
            )
            outcome.changed += 1
            continue

        digest = await _hash(workspace, relative)
        if digest is None:
            continue

        gone = trashed.get(key)
        if gone is not None and gone.version.content_hash == digest:
            # The deletion the app recorded has been undone on the storage (F-014/FR-10).
            await trash.reactivate(
                connection,
                found=gone.file,
                path=relative,
                actor=Actor.system(),
                reason="the content reappeared on the storage",
                seen_at=run.started_at,
            )
            outcome.restored += 1
            continue

        source = await moved_from(
            connection,
            workspace=workspace,
            started_at=run.started_at,
            name=entry.name,
            content_hash=digest,
        )
        if source is not None:
            candidate, match = source
            await files.relocate(
                connection,
                found=candidate.file,
                folder_id=folder_id,
                name=entry.name,
                modified_at=entry.modified_at,
                version_id=candidate.version.id,
                seen_at=run.started_at,
                actor=Actor.system(),
                detected="external",
                match=match,
                from_path=candidate.path,
            )
            if candidate.file.folder_id != folder_id:
                # Counted now or not at all: the row has just stopped saying where it came from,
                # and a folder that lost a majority of its files to one directory is a folder that
                # was renamed (F-015/FR-7).
                await scans.record_relocation(
                    connection,
                    run_id=run.id,
                    from_folder_id=candidate.file.folder_id,
                    to_folder_id=folder_id,
                    files=1,
                )
            outcome.moved += 1
            continue

        await files.register(
            connection,
            workspace_id=workspace.id,
            folder_id=folder_id,
            name=entry.name,
            content_hash=digest,
            size_bytes=entry.size,
            media_type=mediatypes.detect(entry.name, None),
            modified_at=entry.modified_at,
            origin="external",
            last_seen_at=run.started_at,
            actor=Actor.system(),
        )
        outcome.registered += 1

    return outcome


async def _hash(workspace: workspaces.Workspace, relative: str) -> str | None:
    """The file's digest, or `None` if it could not be read — which decides nothing.

    A file that vanished between the listing and this read, or that the app may not open, is
    left exactly as it is. The next pass reconciles it; scans are convergent, not
    snapshot-perfect.
    """
    try:
        return await asyncio.to_thread(_digest, workspace.root_path, relative)
    except (OSError, filestore.ContainmentError) as unreadable:
        _logger.info(
            "skipped a file that could not be hashed",
            extra={"workspace": str(workspace.id), "path": relative, "reason": str(unreadable)},
        )
        return None


async def _new_version(
    connection: AsyncConnection,
    *,
    existing: files.Known,
    entry: Entry,
    digest: str,
    store: BlobStore,
) -> None:
    """Record content that changed on the storage as a new current version."""
    # Normally false — the bytes were overwritten before the app could copy them (F-007/FR-9).
    # Asked rather than assumed, because an earlier app-mediated write may have snapshotted
    # exactly this content, and then history really is intact.
    held = await asyncio.to_thread(store.contains, existing.version.content_hash)
    await files.add_version(
        connection,
        found=existing.file,
        content_hash=digest,
        size_bytes=entry.size,
        media_type=mediatypes.detect(entry.name, None),
        modified_at=entry.modified_at,
        origin="external",
        actor=Actor.system(),
        predecessor_restorable=held,
    )


# ---------------------------------------------------------------------- the sweep


async def sweep_batch(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    root_folder_id: UUID,
    store: BlobStore,
    actor: Actor,
    limit: int = SWEEP_BATCH,
) -> int:
    """Trash one batch of files the run did not see. Returns how many.

    Needs no cursor of its own: the query asks for `live` rows, and every row it handles stops
    being one, so a crash mid-sweep resumes by re-running the same query. A run interrupted
    before its frontier empties never reaches this at all — the traversal has to be finished for
    "did not see" to mean anything.
    """
    missing = await files.unseen_under(
        connection,
        root_folder_id=root_folder_id,
        run_id=run.id,
        started_at=run.started_at,
        limit=limit,
    )
    if not missing:
        return 0

    # One thread hop for the whole batch: a folder of ten thousand deleted photos would
    # otherwise be ten thousand of them.
    digests = {known.version.content_hash for known in missing}
    held = await asyncio.to_thread(lambda: {digest: store.contains(digest) for digest in digests})

    for known in missing:
        await trash.record(
            connection,
            found=known.file,
            path=await files.path_of(connection, known.file),
            origin="detected_on_disk",
            batch_id=run.id,
            actor=actor,
            # Almost always false, and honest either way: the app holds these bytes only if some
            # earlier app-mediated write happened to snapshot exactly this content.
            restorable=held[known.version.content_hash],
        )
    return len(missing)


# ------------------------------------------------------------------- folder identity


@dataclass(frozen=True, slots=True)
class Claim:
    """A vanished folder, the new directory its content turned up in, and on what evidence."""

    source: folders.Folder
    destination: folders.Folder
    evidence: str
    matched: int
    known: int


@dataclass(slots=True)
class Verdicts:
    """What the identity pass concluded, for the run's counters."""

    transferred: int = 0
    ambiguous: int = 0


async def _known_content(
    connection: AsyncConnection,
    *,
    folder_id: UUID,
    run_id: UUID,
    started_at: datetime,
    moves: list[scans.Relocation],
) -> tuple[int, int]:
    """What this folder held when the run started: files, then child folders.

    The denominator the majority is measured against. Both halves are needed because what left is
    no longer filed here and what stayed never appears in the relocation counts.

    "When the run started" is the whole of it, and it is not the same as "ever". A trashed row
    keeps the folder it was in, so counting every state counted deletions from months ago — and a
    folder whose history of deletions outnumbers what it currently holds could then never reach a
    majority, so an external rename of it was permanently undetectable and its grants and tags
    were orphaned every time. What *this* run's sweep trashed does belong here: those files were
    in the folder when it started and are evidence about it, which is why they are added back
    rather than filtered out with the rest.
    """
    trashed_by_this_run = select(trash_entry.c.file_id).where(
        trash_entry.c.file_id == file_table.c.id, trash_entry.c.batch_id == run_id
    )
    stayed_files = (
        await connection.execute(
            select(func.count())
            .select_from(file_table)
            .where(
                file_table.c.folder_id == folder_id,
                file_table.c.created_at < started_at,
                or_(file_table.c.state == "live", trashed_by_this_run.exists()),
            )
        )
    ).scalar_one()
    stayed_folders = (
        await connection.execute(
            select(func.count())
            .select_from(folder_table)
            .where(folder_table.c.parent_id == folder_id, folder_table.c.created_at < started_at)
        )
    ).scalar_one()
    return (
        stayed_files + sum(move.files for move in moves),
        stayed_folders + sum(move.folders for move in moves),
    )


def _majority(
    moves: list[scans.Relocation], *, known_files: int, known_folders: int
) -> tuple[UUID, str, int, int] | None:
    """The one destination a majority of this folder's content went to, and which kind it was.

    Two independent readings of the same question, because a directory holding nothing but
    subdirectories has no files to vouch for it (F-015/FR-7) and one holding nothing but files has
    no subdirectories. A strict majority is unique, so each reading yields at most one answer —
    and if the two disagree, that disagreement *is* the ambiguity.
    """
    by_files = [move for move in moves if known_files and move.files * 2 > known_files]
    by_folders = [move for move in moves if known_folders and move.folders * 2 > known_folders]
    if by_files and by_folders and by_files[0].to_folder_id != by_folders[0].to_folder_id:
        return None
    if by_files:
        return by_files[0].to_folder_id, "files", by_files[0].files, known_files
    if by_folders:
        return by_folders[0].to_folder_id, "folders", by_folders[0].folders, known_folders
    return None


async def _claim(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    root_folder_id: UUID,
    source_id: UUID,
    moves: list[scans.Relocation],
) -> Claim | None:
    """Whether this folder's content is evidence that its directory was renamed, not deleted.

    Every refusal here is a rule rather than a guard:

    - the workspace root is not a directory that can be renamed away ([F-015/FR-1]);
    - a folder outside the run's own root is one this traversal never looked for, and a subtree
      rescan concludes nothing about the rest of the workspace;
    - a folder whose directory this run *did* account for is not a rename at all — its files
      simply moved somewhere else;
    - a destination that already existed is a place files were moved **into**, not a directory
      that appeared in the shape of one that went away;
    - a destination inside the source's own subtree would make the tree its own ancestor.
    """
    source = await folders.get(connection, source_id)
    if source is None or source.is_root:
        return None
    if not await folders.contains(connection, ancestor_id=root_folder_id, descendant_id=source_id):
        return None
    if not await folders.vanished(
        connection, folder_id=source_id, run_id=run.id, started_at=run.started_at
    ):
        return None

    known_files, known_folders = await _known_content(
        connection,
        folder_id=source_id,
        run_id=run.id,
        started_at=run.started_at,
        moves=moves,
    )
    verdict = _majority(moves, known_files=known_files, known_folders=known_folders)
    if verdict is None:
        return None
    destination_id, evidence, matched, known = verdict

    destination = await folders.get(connection, destination_id)
    if destination is None or destination.created_at < run.started_at:
        return None
    if await folders.contains(  # pragma: no cover - insurance, see below
        connection, ancestor_id=source_id, descendant_id=destination_id
    ):
        # A directory cannot exist inside one that is gone, so on a tree that agrees with itself
        # this cannot happen. It is checked anyway because the cost of being wrong is a folder
        # that is its own ancestor, and a corrupted closure is not a recoverable state.
        return None
    return Claim(source, destination, evidence, matched, known)


async def _ambiguous(
    connection: AsyncConnection,
    *,
    source: folders.Folder,
    reason: str,
    moves: list[scans.Relocation],
) -> None:
    """Record that a folder's content scattered, or that two folders' content converged.

    FR-7's audit event. The new identity is already there — it was created by the traversal — so
    nothing is undone here; what is written is *why* the old one was not reused, which is what a
    later review surface ([Q24](../../../OPEN-QUESTIONS.md)) would read.
    """
    await events.record(
        connection,
        action=events.FOLDER_IDENTITY_AMBIGUOUS,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=source.id,
        actor=Actor.system(),
        details={
            "workspace": str(source.workspace_id),
            "path": await folders.path_of(connection, source),
            "reason": reason,
            "candidates": [
                {
                    "folder": str(move.to_folder_id),
                    "files": move.files,
                    "folders": move.folders,
                }
                for move in moves
            ],
        },
    )


async def _absorb(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    into: folders.Folder,
    discarded: folders.Folder,
) -> list[tuple[UUID, UUID]]:
    """Empty one folder into another, merging children the two both have a name for.

    Two rows describing one directory cannot simply be poured together: sibling uniqueness
    (F-015/FR-6) refuses a child that would land on a name the survivor already holds, and the
    refusal takes the whole identity pass with it. That happens for a perfectly ordinary tree.
    A renamed directory with an **empty** subdirectory in it is enough — FR-7 cannot match an
    empty directory to anything, so the old row stays where it is while the traversal registers
    a namesake under the new one — and so is a subdirectory this run called ambiguous.

    The pair is the same directory, and the evidence is better than anything the deepest-first
    pass had: the two parents are known to be one directory, so a child of the same comparison
    key under each of them is one child. The older row wins, keeping the UUID that grants and
    tags hang off, and takes the disk's spelling of the name. Recursively, because an empty
    subdirectory can have empty subdirectories of its own.

    Returns the pairs it merged, deepest first. Their events are written by the caller, once the
    transfer has finished moving the chain they hang off: a path read here still resolves through
    the directory name that is about to be replaced.
    """
    merged: list[tuple[UUID, UUID]] = []
    for stale, fresh in await folders.namesakes(
        connection, parent_id=into.id, other_id=discarded.id
    ):
        merged.extend(await _absorb(connection, run=run, into=stale, discarded=fresh))
        # The same handover the transfer itself does, and for the same two reasons: `folder_delta`
        # and `folder_aggregate` both cascade from the row that is about to go.
        await aggregates.repoint(connection, from_folder=fresh.id, to_folder=stale.id)
        await aggregates.inherit(connection, heir=stale.id, from_folder=fresh.id)
        await folders.discard(connection, fresh.id)
        if stale.name != fresh.name:
            # Same comparison key, different spelling: the disk is the source of truth for how a
            # name is written, and the rename event says the app merely recognised it.
            stale = await folders.reposition(
                connection,
                found=stale,
                parent=into,
                name=fresh.name,
                actor=Actor.system(),
                detected="external",
            )
        await folders.stamp_seen(connection, folder_ids=[stale.id], seen_at=run.started_at)
        merged.append((stale.id, fresh.id))
    await folders.absorb(connection, into=into, discarded=discarded)
    return merged


async def _record_merge(connection: AsyncConnection, *, survivor: UUID, discarded: UUID) -> None:
    """FR-11's record of a namesake merge — written after the chain above it has settled."""
    found = await folders.get(connection, survivor)
    if found is None:  # pragma: no cover - the survivor is the row that was kept
        return
    await events.record(
        connection,
        action=events.FOLDER_IDENTITY_MERGED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=found.id,
        actor=Actor.system(),
        details={
            "workspace": str(found.workspace_id),
            "path": await folders.path_of(connection, found),
            "discarded": str(discarded),
            "reason": "a namesake child of a directory whose identity transferred",
            "detected": "external",
        },
    )


async def _transfer(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    workspace: workspaces.Workspace,
    claim: Claim,
) -> None:
    """Give the vanished folder its directory's new place, and delete the row that stood in.

    The order is the whole of it. The stand-in is emptied and **deleted before** the survivor is
    moved, because until it is gone the two would be siblings under one name — which sibling
    uniqueness (F-015/FR-6) rightly refuses.

    Three things about the rollup queue, and each of them is a way this could go quietly wrong
    ([F-015/FR-8](../../../features/F-015-folders.md)):

    - the stand-in's queued changes are handed over before it is deleted, **and so are its
      applied ones** — `folder_aggregate` cascades on deletion just as `folder_delta` does, so
      whichever side of the drain the stand-in's numbers are on, they are read before the row
      goes;
    - the survivor's own queued changes were written while it was somewhere else, and after the
      move they will expand over the chain it has *now* — so the amount is measured first and the
      two chains are compensated, which is the same `+n`/`-n` pair a deliberate move writes;
    - the survivor's totals **add** the stand-in's rather than replacing them, because its own
      number is not zero but minus whatever it still has queued.

    All of it under the workspace lock, because the closure moves.
    """
    await aggregates.lock(connection, workspace.id)
    parent = await folders.get(connection, claim.destination.parent_id or claim.destination.id)
    if parent is None:  # pragma: no cover - a non-root folder always has its parent
        raise RuntimeError(f"folder {claim.destination.id} has no parent to inherit")

    # Measured before anything moves: these are the changes queued against the survivor's *old*
    # subtree, and they are about to start expanding over a different chain.
    files, size_bytes = await aggregates.queued_under(connection, claim.source.id)
    await aggregates.repoint(
        connection, from_folder=claim.destination.id, to_folder=claim.source.id
    )

    # Before the row goes: `folder_aggregate` cascades on folder deletion, so a stand-in whose
    # queued changes a rollup already drained holds them here and nowhere else. Reading it after
    # `discard` found nothing and added nothing — the survivor then reported an empty directory
    # until the drift sweep happened past it, which is hours on a large tree.
    await aggregates.inherit(connection, heir=claim.source.id, from_folder=claim.destination.id)

    merged = await _absorb(connection, run=run, into=claim.source, discarded=claim.destination)
    await folders.discard(connection, claim.destination.id)
    moved = await folders.reposition(
        connection,
        found=claim.source,
        parent=parent,
        name=claim.destination.name,
        actor=Actor.system(),
        detected="external",
    )
    await folders.stamp_seen(connection, folder_ids=[moved.id], seen_at=run.started_at)
    for survivor, discarded in merged:
        # Only now: every path under this folder resolved through the old directory name until
        # the line above moved the chain.
        await _record_merge(connection, survivor=survivor, discarded=discarded)
    if claim.source.parent_id != parent.id and (files or size_bytes):
        # The old chain keeps what it was owed and the new one gives back what it was not: above
        # the two parents' common ancestor the pair cancels, exactly as for a deliberate move.
        await aggregates.record(
            connection,
            workspace_id=workspace.id,
            folder_id=claim.source.parent_id or claim.source.id,
            files=files,
            size_bytes=size_bytes,
        )
        await aggregates.record(
            connection,
            workspace_id=workspace.id,
            folder_id=parent.id,
            files=-files,
            size_bytes=-size_bytes,
        )
    _logger.info(
        "a folder kept its identity through an external rename",
        extra={
            "workspace": str(workspace.id),
            "folder": str(moved.id),
            "evidence": claim.evidence,
            "matched": claim.matched,
            "known": claim.known,
        },
    )


def _by_source(moves: list[scans.Relocation]) -> dict[UUID, list[scans.Relocation]]:
    grouped: dict[UUID, list[scans.Relocation]] = {}
    for move in moves:
        grouped.setdefault(move.from_folder_id, []).append(move)
    return grouped


async def identities(
    connection: AsyncConnection,
    *,
    run: scans.Run,
    workspace: workspaces.Workspace,
    root_folder_id: UUID,
) -> Verdicts:
    """Transfer the identity of every folder whose directory was renamed rather than replaced.

    [F-015/FR-7](../../../features/F-015-folders.md), and it runs only once the traversal *and*
    the sweep are done: "this directory is gone" needs the whole tree read, and a file still
    waiting to be trashed would count towards the wrong denominator.

    **Deepest first**, for two reasons that both come from a renamed directory containing others.
    A parent processed first would put two folders of the same name under one parent. And a
    directory holding nothing but subdirectories has no files to vouch for it — its evidence *is*
    its children's transfers, so those have to have happened, which is why the evidence is re-read
    from the table on every turn rather than held in memory.
    """
    verdicts = Verdicts()
    if not await scans.relocations(connection, run.id):
        return verdicts

    # A destination two vanished folders both claim is a merge, and FR-7 gives neither of them the
    # identity. Decided up front from the file evidence, which is where a merge shows up.
    claimants: dict[UUID, set[UUID]] = {}
    for source_id, moves in _by_source(await scans.relocations(connection, run.id)).items():
        claim = await _claim(
            connection, run=run, root_folder_id=root_folder_id, source_id=source_id, moves=moves
        )
        if claim is not None:
            claimants.setdefault(claim.destination.id, set()).add(source_id)
    contested = {destination for destination, sources in claimants.items() if len(sources) > 1}

    handled: set[UUID] = set()
    while True:
        grouped = _by_source(await scans.relocations(connection, run.id))
        candidates: list[Claim] = []
        for source_id, moves in grouped.items():
            if source_id in handled:
                continue
            claim = await _claim(
                connection,
                run=run,
                root_folder_id=root_folder_id,
                source_id=source_id,
                moves=moves,
            )
            if claim is not None:
                candidates.append(claim)
        if not candidates:
            break

        # Deepest first, then by id so a retry makes the same choices in the same order.
        chosen = max(candidates, key=lambda claim: (claim.source.depth, claim.source.id.bytes))
        handled.add(chosen.source.id)

        if chosen.destination.id in contested:
            await _ambiguous(
                connection,
                source=chosen.source,
                reason="merge",
                moves=grouped[chosen.source.id],
            )
            verdicts.ambiguous += 1
            continue

        await _transfer(connection, run=run, workspace=workspace, claim=chosen)
        contested.add(chosen.destination.id)
        verdicts.transferred += 1

        # What the transfer proves about the folder *above* the one that moved: one of its child
        # folders is now somewhere else. That is the only evidence a directory holding no files of
        # its own ever leaves (F-015/FR-7), and the next turn — shallower — reads it.
        if (
            chosen.source.parent_id is not None
            and chosen.destination.parent_id is not None
            and chosen.source.parent_id != chosen.destination.parent_id
        ):
            await scans.record_relocation(
                connection,
                run_id=run.id,
                from_folder_id=chosen.source.parent_id,
                to_folder_id=chosen.destination.parent_id,
                folders=1,
            )

    # Everything left with evidence and no majority is a split: its content went several ways.
    for source_id, moves in _by_source(await scans.relocations(connection, run.id)).items():
        if source_id in handled:
            continue
        source = await folders.get(connection, source_id)
        if source is None or source.is_root:
            continue
        if not await folders.vanished(
            connection, folder_id=source_id, run_id=run.id, started_at=run.started_at
        ):
            continue
        if not await folders.contains(
            connection, ancestor_id=root_folder_id, descendant_id=source_id
        ):
            continue
        await _ambiguous(connection, source=source, reason="split", moves=moves)
        verdicts.ambiguous += 1

    return verdicts
