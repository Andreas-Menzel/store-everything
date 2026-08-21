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

from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import files, filestore, mediatypes, names, scans, trash, workspaces
from store_everything.blobs import BlobStore
from store_everything.events import Actor

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
                match=match,
                from_path=candidate.path,
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
