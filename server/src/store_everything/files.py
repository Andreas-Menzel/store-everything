"""Files and their versions: every row an upload or a scan produces about one file.

A file is *(folder, name)* with a UUID that outlives both (02 § file). Its **path is derived**
from the folder chain and never stored, so a rename or a move cannot leave a stale string
behind; `path_of` is that derivation, and it is the first real consumer of the folder closure
([F-015/FR-2](../../../features/F-015-folders.md)) — one indexed join instead of a walk.

A version is an immutable snapshot identified by its content hash. Exactly one version per
file is current, enforced by a partial unique index rather than maintained by code. What a
version *also* records is whether its bytes can still be produced: `restorable` is
[F-007/FR-9](../../../features/F-007-versioning.md), and it is set by whoever knew — the write
path that took a snapshot, or the scan that found the bytes already overwritten.

Sibling uniqueness covers `live` rows only, so a trashed file stops reserving its path
([F-014/FR-1](../../../features/F-014-deletion-and-trash.md)). Every query here therefore says
which state it means, and `find_in_folder` defaults to `live` because that is the file anyone
can open.

**No event for the upload session, one for the file.** The session is mechanism — a durable
row in the same sense as an `operation` record (ADR-0017), and those are not individually
audited either. What the audit trail owes a reader is what happened to the file: it appeared,
gained a version, moved. Reading the tree is not one of those things, which is why a scan that
merely confirms a file is still there writes no event and only stamps `last_seen_at`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Select,
    Text,
    and_,
    any_,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as UUID_TYPE
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import aggregates, events, mediatypes, names
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import file, file_version, folder, folder_closure, scan_blocked


@dataclass(frozen=True, slots=True)
class File:
    id: UUID
    workspace_id: UUID
    folder_id: UUID
    name: str
    state: str
    created_at: datetime

    @property
    def is_live(self) -> bool:
        return self.state == "live"


@dataclass(frozen=True, slots=True)
class Version:
    id: UUID
    file_id: UUID
    content_hash: str
    size_bytes: int
    media_type: str
    media_class: str
    origin: str
    is_current: bool
    restorable: bool
    """Whether these bytes can still be produced — from the tree while this version is
    current, from `versions/` once it is not (F-007/FR-9)."""

    modified_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Known:
    """A file the app already holds, with the version a scan compares the disk against."""

    file: File
    version: Version


_FILE_COLUMNS = (
    file.c.id,
    file.c.workspace_id,
    file.c.folder_id,
    file.c.name,
    file.c.state,
    file.c.created_at,
)

_VERSION_COLUMNS = (
    file_version.c.id,
    file_version.c.file_id,
    file_version.c.content_hash,
    file_version.c.size_bytes,
    file_version.c.media_type,
    file_version.c.media_class,
    file_version.c.origin,
    file_version.c.is_current,
    file_version.c.restorable,
    file_version.c.modified_at,
    file_version.c.created_at,
)

type _FileRow = tuple[UUID, UUID, UUID, str, str, datetime]
type _VersionRow = tuple[UUID, UUID, str, int, str, str, str, bool, bool, datetime | None, datetime]


def _as_file(row: _FileRow) -> File:
    return File(*row)


def _as_version(row: _VersionRow) -> Version:
    return Version(*row)


def _file_query() -> Select[_FileRow]:
    return select(*_FILE_COLUMNS)


async def get(connection: AsyncConnection, file_id: UUID) -> File | None:
    row = (await connection.execute(_file_query().where(file.c.id == file_id))).first()
    return None if row is None else _as_file(tuple(row))


async def find_in_folder(
    connection: AsyncConnection, *, folder_id: UUID, name: str, state: str = "live"
) -> File | None:
    """The folder's file of that name, compared on the key — so `Report.pdf` finds
    `report.pdf` ([F-001/FR-7](../../../features/F-001-upload-and-import.md)).

    Live by default, because a trashed row does not reserve its path
    ([F-014/FR-1](../../../features/F-014-deletion-and-trash.md)) and must not be reported as
    a collision. Asking for `trashed` is how the reappearance of deleted content finds the row
    to reactivate (F-014/FR-10); several may exist there, and the newest is the answer.
    """
    row = (
        await connection.execute(
            _file_query()
            .where(
                file.c.folder_id == folder_id,
                file.c.name_key == names.comparison_key(name),
                file.c.state == state,
            )
            .order_by(file.c.created_at.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else _as_file(tuple(row))


async def in_folder(connection: AsyncConnection, folder_id: UUID) -> list[Known]:
    """Every file the app holds in this folder, with its current version. One query.

    What a scan needs to reconcile a directory: the per-entry alternative is two round trips
    per file, and a directory with 50 000 photos in it turns an hourly pass into a round-trip
    storm. Trashed rows are included — the reappearance of deleted content is recognised from
    them (F-014/FR-10).
    """
    rows = (
        await connection.execute(
            select(*_FILE_COLUMNS, *_VERSION_COLUMNS)
            .join(file_version, file_version.c.file_id == file.c.id)
            .where(file.c.folder_id == folder_id, file_version.c.is_current.is_(True))
            .order_by(file.c.created_at)
        )
    ).all()
    return [
        Known(
            file=_as_file(tuple(row)[: len(_FILE_COLUMNS)]),
            version=_as_version(tuple(row)[len(_FILE_COLUMNS) :]),
        )
        for row in rows
    ]


async def unseen_under(
    connection: AsyncConnection,
    *,
    root_folder_id: UUID,
    run_id: UUID,
    started_at: datetime,
    limit: int,
) -> list[Known]:
    """Live files in this subtree that the run did not see — the deletions to reconcile.

    Four conditions, and each one is a rule rather than an optimization
    ([F-001/FR-6](../../../features/F-001-upload-and-import.md)):

    - **In the subtree**, by the folder closure: a rescan of one directory must never conclude
      anything about the rest of the workspace.
    - **Not stamped by this run.** Every name a directory mentioned was stamped, so what is
      left was in no listing this run read.
    - **Registered before the run started.** A file uploaded while the scan was walking is not
      missing — the traversal simply passed its directory earlier. Scans are convergent, not
      snapshot-perfect (12 § job atomicity), so the next pass covers it.
    - **Not under a directory this run could not read** (F-001/FR-16). "I could not look" says
      nothing about what is inside, and the sweep's whole job is concluding things about the
      inside.

    Returned with the current version because deciding restorability needs its digest, and the
    trash entry needs its path.
    """
    # Its own alias of the closure: the outer query joins it too, for the subtree bound.
    under_blocked = folder_closure.alias("blocked_closure")
    blocked = (
        select(scan_blocked.c.folder_id)
        .join(under_blocked, under_blocked.c.ancestor_id == scan_blocked.c.folder_id)
        .where(scan_blocked.c.run_id == run_id, under_blocked.c.descendant_id == file.c.folder_id)
    )
    rows = (
        await connection.execute(
            select(*_FILE_COLUMNS, *_VERSION_COLUMNS)
            .join(file_version, file_version.c.file_id == file.c.id)
            .join(folder_closure, folder_closure.c.descendant_id == file.c.folder_id)
            .where(
                folder_closure.c.ancestor_id == root_folder_id,
                file.c.state == "live",
                file_version.c.is_current.is_(True),
                or_(file.c.last_seen_at.is_(None), file.c.last_seen_at < started_at),
                file.c.created_at < started_at,
                ~blocked.exists(),
            )
            .order_by(file.c.id)
            .limit(limit)
        )
    ).all()
    return [
        Known(
            file=_as_file(tuple(row)[: len(_FILE_COLUMNS)]),
            version=_as_version(tuple(row)[len(_FILE_COLUMNS) :]),
        )
        for row in rows
    ]


async def live_under(connection: AsyncConnection, folder_id: UUID) -> bool:
    """Whether the index still holds a live file anywhere beneath this folder.

    Asked by the scan before it concludes anything from a directory that came back *empty*: an
    empty listing over nothing is unremarkable, while an empty listing over a subtree the index
    is holding is the shape of storage that went away (F-001/FR-22).
    """
    return bool(
        (
            await connection.execute(
                select(
                    select(file.c.id)
                    .join(folder_closure, folder_closure.c.descendant_id == file.c.folder_id)
                    .where(folder_closure.c.ancestor_id == folder_id, file.c.state == "live")
                    .exists()
                )
            )
        ).scalar_one()
    )


async def holds_any_content(connection: AsyncConnection, file_id: UUID) -> bool:
    """Whether the app can still produce *some* version of this file's content.

    What restorability means for a trash entry (F-014/FR-10): derived rather than stored, so it
    cannot go stale behind a snapshot taken later or a blob collected earlier.
    """
    return bool(
        (
            await connection.execute(
                select(
                    select(file_version.c.id)
                    .where(file_version.c.file_id == file_id, file_version.c.restorable.is_(True))
                    .exists()
                )
            )
        ).scalar_one()
    )


async def current_version(connection: AsyncConnection, file_id: UUID) -> Version | None:
    row = (
        await connection.execute(
            select(*_VERSION_COLUMNS).where(
                file_version.c.file_id == file_id, file_version.c.is_current.is_(True)
            )
        )
    ).first()
    return None if row is None else _as_version(tuple(row))


async def path_of(connection: AsyncConnection, found: File) -> str:
    """The file's workspace-relative path, derived from its folder chain.

    The root folder has no name of its own, so it contributes nothing but the leading
    position — which is why a file directly in the root comes back as just its own name.
    """
    ancestors = (
        (
            await connection.execute(
                select(folder.c.name)
                .join(folder_closure, folder_closure.c.ancestor_id == folder.c.id)
                .where(folder_closure.c.descendant_id == found.folder_id)
                .order_by(folder_closure.c.depth.desc())
            )
        )
        .scalars()
        .all()
    )
    return "/".join([*(name for name in ancestors if name), found.name])


async def register(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    folder_id: UUID,
    name: str,
    content_hash: str,
    size_bytes: int,
    media_type: str,
    modified_at: datetime | None,
    origin: str,
    actor: Actor,
    last_seen_at: datetime | None = None,
) -> tuple[File, Version]:
    """Record a file and its first version. The bytes are already on disk.

    Deliberately the last step of an upload rather than the first: bytes outlive the rows
    that reference them (02 § invariant 8), so a crash before this leaves a real file the
    scan adopts — never a row promising content that is not there.
    """
    created = _as_file(
        tuple(
            (
                await connection.execute(
                    insert(file)
                    .values(
                        id=new_id(),
                        workspace_id=workspace_id,
                        folder_id=folder_id,
                        name=name,
                        name_key=names.comparison_key(name),
                        state="live",
                        # A file the app just put there is known to be there. Stamping it now
                        # means a scan that starts mid-upload cannot mistake it for missing.
                        last_seen_at=func.now() if last_seen_at is None else last_seen_at,
                    )
                    .returning(*_FILE_COLUMNS)
                )
            ).one()
        )
    )

    version = _as_version(
        tuple(
            (
                await connection.execute(
                    insert(file_version)
                    .values(
                        id=new_id(),
                        file_id=created.id,
                        content_hash=content_hash,
                        size_bytes=size_bytes,
                        media_type=media_type,
                        media_class=mediatypes.media_class(media_type),
                        origin=origin,
                        is_current=True,
                        modified_at=modified_at,
                    )
                    .returning(*_VERSION_COLUMNS)
                )
            ).one()
        )
    )

    await events.record(
        connection,
        action=events.FILE_CREATED,
        resource_type=events.RESOURCE_FILE,
        resource_id=created.id,
        actor=actor,
        details={
            "workspace": str(workspace_id),
            # The path as it was at registration time, so the trail stays readable after a
            # move (F-011/FR-9).
            "path": await path_of(connection, created),
            "size": version.size_bytes,
            "media_type": version.media_type,
            "content_hash": version.content_hash,
            "origin": origin,
        },
    )
    # One more file, and its bytes, for this folder and everything above it (F-015/FR-8). Queued
    # here rather than applied here: the rollup is what keeps an import from serialising itself
    # on the workspace root's row. Asking for a run is the *caller's* job, once per transaction.
    await aggregates.record(
        connection,
        workspace_id=workspace_id,
        folder_id=folder_id,
        files=1,
        size_bytes=version.size_bytes,
    )
    return created, version


async def stamp_seen(
    connection: AsyncConnection, *, folder_id: UUID, name_keys: Sequence[str], seen_at: datetime
) -> int:
    """Record that a scan found these names in this folder. Returns how many rows it stamped.

    Stamped with the *run's* start rather than `now()`, so "what did this run not see" is an
    exact comparison however long the run took — and a run interrupted halfway leaves a
    consistent answer rather than a moving one.

    Every name the directory *mentioned* is stamped, not only the ones that registered: an
    entry the scan saw and refused — a symlink where a file used to be, the loser of a name
    collision, something it could not `stat` — is not an absence, and only an absence may
    conclude a deletion ([F-001/FR-18](../../../features/F-001-upload-and-import.md)).
    """
    if not name_keys:
        return 0
    result = await connection.execute(
        update(file)
        .where(
            file.c.folder_id == folder_id,
            file.c.state == "live",
            # One array parameter rather than an `IN` list of them: a directory can hold more
            # entries than PostgreSQL allows bind parameters (65 535), and a photo folder that
            # large should scan slowly, not fail.
            file.c.name_key == any_(literal(list(name_keys), ARRAY(Text))),
        )
        .values(last_seen_at=seen_at)
    )
    return result.rowcount


async def candidates_with_content(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    content_hash: str,
    unseen_since: datetime,
    limit: int,
) -> list[Known]:
    """Live files in this workspace whose current content is this, oldest registration first.

    The move heuristic's first step (02 § file). Restricted to files this run has **not**
    stamped, which is what keeps it cheap: during an import every duplicate already seen is
    excluded, so the common case finds nothing and reads no disk. A file the run has not
    reached yet is still a candidate — one `lstat` says it is there, and that is the answer.

    The current version comes along because a move updates the timestamp it recorded, and
    fetching it again per candidate would be a round trip for something already joined.
    """
    rows = (
        await connection.execute(
            select(*_FILE_COLUMNS, *_VERSION_COLUMNS)
            .join(file_version, file_version.c.file_id == file.c.id)
            .where(
                file.c.workspace_id == workspace_id,
                file.c.state == "live",
                file_version.c.is_current.is_(True),
                file_version.c.content_hash == content_hash,
                or_(file.c.last_seen_at.is_(None), file.c.last_seen_at < unseen_since),
            )
            .order_by(file.c.created_at, file.c.id)
            .limit(limit)
        )
    ).all()
    return [
        Known(
            file=_as_file(tuple(row)[: len(_FILE_COLUMNS)]),
            version=_as_version(tuple(row)[len(_FILE_COLUMNS) :]),
        )
        for row in rows
    ]


async def add_version(
    connection: AsyncConnection,
    *,
    found: File,
    content_hash: str,
    size_bytes: int,
    media_type: str,
    modified_at: datetime | None,
    origin: str,
    actor: Actor,
    predecessor_restorable: bool,
    seen_at: datetime | None = None,
) -> Version:
    """Make new content this file's current version, keeping the old one as history.

    `predecessor_restorable` is [F-007/FR-9](../../../features/F-007-versioning.md) in one
    argument: `True` when the app snapshotted the previous bytes into `versions/` before
    writing, `False` when it is only *noticing* a change that already overwrote them. Passed in
    rather than inferred, because the only code that knows is the code that had the chance to
    take a copy.

    The partial unique index is what keeps "exactly one current version" true here: the
    predecessor is demoted in the same statement batch, and a second attempt after a crash
    finds the new version already current and the old one already demoted.
    """
    # The demoted version's size comes back from the statement that demotes it: the folder's
    # total moves by the *difference*, and this is the only moment both numbers are in hand.
    superseded = (
        await connection.execute(
            update(file_version)
            .where(file_version.c.file_id == found.id, file_version.c.is_current.is_(True))
            .values(is_current=False, restorable=predecessor_restorable)
            .returning(file_version.c.size_bytes)
        )
    ).scalar_one_or_none()
    version = _as_version(
        tuple(
            (
                await connection.execute(
                    insert(file_version)
                    .values(
                        id=new_id(),
                        file_id=found.id,
                        content_hash=content_hash,
                        size_bytes=size_bytes,
                        media_type=media_type,
                        media_class=mediatypes.media_class(media_type),
                        origin=origin,
                        is_current=True,
                        modified_at=modified_at,
                    )
                    .returning(*_VERSION_COLUMNS)
                )
            ).one()
        )
    )
    await connection.execute(
        update(file)
        .where(file.c.id == found.id)
        .values(
            updated_at=func.now(),
            last_seen_at=func.now() if seen_at is None else seen_at,
        )
    )

    await events.record(
        connection,
        action=events.FILE_VERSION_CREATED,
        resource_type=events.RESOURCE_FILE,
        resource_id=found.id,
        actor=actor,
        details={
            "workspace": str(found.workspace_id),
            "path": await path_of(connection, found),
            "version": str(version.id),
            "size": version.size_bytes,
            "content_hash": version.content_hash,
            "origin": origin,
            # The reader's question about the version this one replaced: is it still there to
            # go back to, or is this all that is left?
            "predecessor_restorable": predecessor_restorable,
        },
    )
    if found.is_live:
        # A trashed file contributes nothing to a folder's totals, so new content for one changes
        # nothing to adjust — reactivation is what puts its current size back.
        await aggregates.record(
            connection,
            workspace_id=found.workspace_id,
            folder_id=found.folder_id,
            size_bytes=version.size_bytes - (superseded or 0),
        )
    return version


async def refresh_observed_mtime(
    connection: AsyncConnection, *, version_id: UUID, modified_at: datetime | None
) -> None:
    """Record the timestamp the unchanged content now carries on disk.

    A touch, or a copy restored from a backup, changes the mtime without changing a byte. The
    version is not a new one — the hash proves it — but the recorded timestamp has to follow,
    or every later pass re-reads the whole file to reach the same conclusion.
    """
    await connection.execute(
        update(file_version).where(file_version.c.id == version_id).values(modified_at=modified_at)
    )


async def relocate(
    connection: AsyncConnection,
    *,
    found: File,
    folder_id: UUID,
    name: str,
    actor: Actor,
    from_path: str,
    detected: str,
    match: str | None = None,
    modified_at: datetime | None = None,
    version_id: UUID | None = None,
    seen_at: datetime | None = None,
    workspace_id: UUID | None = None,
) -> File:
    """Move this file's identity to a new folder and name, keeping its UUID.

    One function for both ways a file moves, because the row change is identical and only the
    story differs. A person asking for it (`detected="api"` — F-010/FR-1's first-class move) and a
    scan recognising one that already happened (`detected="external"`, with the rule that matched)
    both end here: the content is the same, so this is one file that changed place rather than a
    deletion and an arrival. Everything attached to the UUID — versions now, tags and grants later
    — travels with it for free, which is the entire reason the app addresses files by UUID.

    `workspace_id` is only passed for a cross-workspace move, where the file follows its folder
    (F-015/FR-4); leaving it out keeps the file where it is.
    """
    values: dict[str, Any] = {
        "folder_id": folder_id,
        "name": name,
        "name_key": names.comparison_key(name),
        "last_seen_at": func.now() if seen_at is None else seen_at,
        "updated_at": func.now(),
    }
    if workspace_id is not None:
        values["workspace_id"] = workspace_id
    moved = _as_file(
        tuple(
            (
                await connection.execute(
                    update(file)
                    .where(file.c.id == found.id)
                    .values(**values)
                    .returning(*_FILE_COLUMNS)
                )
            ).one()
        )
    )
    if version_id is not None:
        # A move usually preserves the mtime and sometimes does not; either way the stat-scan has
        # to compare against what is on the disk now. An app-mediated move is a `rename`, which
        # preserves it, so there is nothing to refresh.
        await refresh_observed_mtime(connection, version_id=version_id, modified_at=modified_at)

    details: dict[str, Any] = {
        "workspace": str(moved.workspace_id),
        "from": from_path,
        "to": await path_of(connection, moved),
        "detected": detected,
    }
    if match is not None:
        # Which rule matched, so "why does this file carry those tags?" has an answer when
        # several identical files could have been the one that moved.
        details["match"] = match
    await events.record(
        connection,
        action=events.FILE_MOVED,
        resource_type=events.RESOURCE_FILE,
        resource_id=moved.id,
        actor=actor,
        details=details,
    )
    if moved.folder_id != found.folder_id and moved.is_live:
        # Two deltas, no common-ancestor arithmetic: each expands over its own folder's chain, so
        # every ancestor the two share sees `+n` and `-n` and stays exactly where it was. A pure
        # rename changes no folder and therefore no total.
        size = await current_size(connection, moved.id)
        await aggregates.record(
            connection,
            workspace_id=found.workspace_id,
            folder_id=found.folder_id,
            files=-1,
            size_bytes=-size,
        )
        await aggregates.record(
            connection,
            workspace_id=moved.workspace_id,
            folder_id=moved.folder_id,
            files=1,
            size_bytes=size,
        )
    return moved


async def current_size(connection: AsyncConnection, file_id: UUID) -> int:
    """The size the aggregates count for this file: its current version's, or nothing."""
    size = (
        await connection.execute(
            select(file_version.c.size_bytes).where(
                file_version.c.file_id == file_id, file_version.c.is_current.is_(True)
            )
        )
    ).scalar_one_or_none()
    return size or 0


async def set_state(connection: AsyncConnection, *, file_id: UUID, state: str) -> bool:
    """Flip a file's lifecycle state, guarded on it not already being there."""
    result = await connection.execute(
        update(file)
        .where(file.c.id == file_id, file.c.state != state)
        .values(state=state, updated_at=func.now())
    )
    return result.rowcount == 1


async def set_current_restorable(
    connection: AsyncConnection, *, file_id: UUID, restorable: bool
) -> None:
    """Record whether the current version's bytes can still be produced.

    Called when the answer changes without the content changing: the file vanished from the
    storage, or came back to it.
    """
    await connection.execute(
        update(file_version)
        .where(file_version.c.file_id == file_id, file_version.c.is_current.is_(True))
        .values(restorable=restorable)
    )


async def restorable_digests(connection: AsyncConnection) -> set[str]:
    """Every content digest the app must not delete from the blob store.

    The janitor's reference source (12 § debris & the janitor). Current versions are included
    even though their bytes live in the tree: a snapshot taken *before* its version becomes the
    predecessor would otherwise be unreferenced for the length of an upload, and a long upload
    outlives the grace window.
    """
    rows = (
        await connection.execute(
            select(file_version.c.content_hash)
            .where(file_version.c.restorable.is_(True))
            .distinct()
        )
    ).scalars()
    return set(rows)


#: How a folder's files can be ordered ([F-015/FR-5](../../../features/F-015-folders.md)). The
#: key is what the cursor carries; the id breaks ties so a page seam is stable.
SORT_COLUMNS = {
    "name": file.c.name_key,
    "size": file_version.c.size_bytes,
    "modified": file_version.c.modified_at,
}


async def page_in_folder(
    connection: AsyncConnection,
    *,
    folder_id: UUID,
    sort: str,
    limit: int,
    after: tuple[str, UUID] | None = None,
) -> list[Known]:
    """One page of a folder's **live** files, ordered by the requested key.

    Trashed rows are absent by construction rather than by filtering afterwards
    ([F-014/FR-12](../../../features/F-014-deletion-and-trash.md) applied to a listing): a file
    someone deleted must not appear in a folder they are browsing.

    Keyset-anchored on `(key, id)` so a directory of 100 000 files paginates stably while others
    are being uploaded into it (F-015/FR-5). `modified` can be NULL in principle, so it sorts
    NULLs last and the cursor carries the empty string for them — a page seam has to be
    representable, not just an ordering.
    """
    column = SORT_COLUMNS[sort]
    query = (
        select(*_FILE_COLUMNS, *_VERSION_COLUMNS)
        .join(file_version, file_version.c.file_id == file.c.id)
        .where(
            file.c.folder_id == folder_id,
            file.c.state == "live",
            file_version.c.is_current.is_(True),
        )
        .order_by(column.nullslast(), file.c.id)
        .limit(limit)
    )
    if after is not None:
        key, identifier = after
        query = query.where(
            or_(
                column > _sort_value(sort, key),
                and_(column == _sort_value(sort, key), file.c.id > literal(identifier, UUID_TYPE)),
            )
        )
    rows = (await connection.execute(query)).all()
    return [
        Known(
            file=_as_file(tuple(row)[: len(_FILE_COLUMNS)]),
            version=_as_version(tuple(row)[len(_FILE_COLUMNS) :]),
        )
        for row in rows
    ]


def sort_value_of(known: Known, sort: str) -> str:
    """What a cursor carries for this row under this ordering."""
    if sort == "size":
        return str(known.version.size_bytes)
    if sort == "modified":
        return "" if known.version.modified_at is None else known.version.modified_at.isoformat()
    return names.comparison_key(known.file.name)


def _sort_value(sort: str, key: str) -> Any:
    """The cursor's key as the column's own type, so the comparison is not a string one."""
    if sort == "size":
        return literal(int(key), BigInteger)
    if sort == "modified":
        return literal(datetime.fromisoformat(key), DateTime(timezone=True))
    return literal(key, Text)
