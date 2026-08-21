"""Files and their versions: the rows an upload — and later a scan — produces.

A file is *(folder, name)* with a UUID that outlives both (02 § file). Its **path is derived**
from the folder chain and never stored, so a rename or a move cannot leave a stale string
behind; `path_of` is that derivation, and it is the first real consumer of the folder closure
([F-015/FR-2](../../../features/F-015-folders.md)) — one indexed join instead of a walk.

A version is an immutable snapshot identified by its content hash. Exactly one version per
file is current, enforced by a partial unique index rather than maintained by code.

**No event for the upload session, one for the file.** The session is mechanism — a durable
row in the same sense as an `operation` record (ADR-0017), and those are not individually
audited either. What the audit trail owes a reader is that a file appeared, with the path,
size and hash it appeared with, which is what `file.created` carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, mediatypes, names
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import file, file_version, folder, folder_closure


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
    modified_at: datetime | None
    created_at: datetime


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
    file_version.c.modified_at,
    file_version.c.created_at,
)

type _FileRow = tuple[UUID, UUID, UUID, str, str, datetime]
type _VersionRow = tuple[UUID, UUID, str, int, str, str, str, bool, datetime | None, datetime]


def _as_file(row: _FileRow) -> File:
    return File(*row)


def _as_version(row: _VersionRow) -> Version:
    return Version(*row)


def _file_query() -> Select[_FileRow]:
    return select(*_FILE_COLUMNS)


async def get(connection: AsyncConnection, file_id: UUID) -> File | None:
    row = (await connection.execute(_file_query().where(file.c.id == file_id))).first()
    return None if row is None else _as_file(tuple(row))


async def find_in_folder(connection: AsyncConnection, *, folder_id: UUID, name: str) -> File | None:
    """The folder's file of that name, compared on the key — so `Report.pdf` finds
    `report.pdf` ([F-001/FR-7](../../../features/F-001-upload-and-import.md))."""
    row = (
        await connection.execute(
            _file_query().where(
                file.c.folder_id == folder_id, file.c.name_key == names.comparison_key(name)
            )
        )
    ).first()
    return None if row is None else _as_file(tuple(row))


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
    return created, version


async def mark_seen(connection: AsyncConnection, *, file_id: UUID, seen_at: datetime) -> None:
    """Record that a scan found this file on disk.

    Stamped with the *run's* start rather than `now()`, so "what did this run not see" is an
    exact comparison however long the run took — and a run interrupted halfway leaves a
    consistent answer rather than a moving one.
    """
    await connection.execute(update(file).where(file.c.id == file_id).values(last_seen_at=seen_at))
