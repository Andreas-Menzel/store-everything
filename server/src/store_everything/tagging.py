"""Applying tags: what a file or a folder carries, and who said so.

The vocabulary lives in [tags.py](tags.py); this is the other half — the rows that say *this
file is an invoice*. Two rules shape all of it:

- **A tag belongs to the file, not to the viewer** (02 § file). Alice grants Bob write, Bob
  tags, Alice sees the tag stamped with Bob's user id. There is one shared truth per file and
  no private layer, which is a decision ([F-003](../../../features/F-003-tagging.md) § out of
  scope), not an omission.
- **User curation is its own table.** `file_tag` holds what a person said — `manual` now,
  `confirmed` and `rejected` once there are machine claims to curate. Nothing that reprocessing
  runs ever writes here, which is how
  [02 § invariants](../../../specs/02-domain-model.md#invariants) #4 stays true by construction
  rather than by vigilance.

Folder tags are the simple case and deliberately stay that way
([F-015/FR-9](../../../features/F-015-folders.md)): manual, self-only, no provenance state
machine, because extractors never run on folders. A folder tag describes the folder and does not
match the files inside it — inheritance is deferred with its precedence rules unanswered (Q23).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, tags
from store_everything.events import Actor
from store_everything.tables import file_tag, folder_tag, tag, tag_name
from store_everything.tags import Tag

#: What a user's row can say when the tag is on the file. `rejected` is a record that it is
#: *not*, so it is not an application and never appears in a tag list.
APPLIED_STATES = ("manual", "confirmed")


class NotVocabularyError(Exception):
    """The tag exists but may not be applied — a quarantined suggestion, or a rejected word.

    Carries the status so the answer can say which, because "approve it first" and "that word
    was turned down" are different things for the person reading it (F-003/FR-12).
    """

    def __init__(self, tag_id: UUID, status: str) -> None:
        super().__init__(status)
        self.tag_id = tag_id
        self.status = status


@dataclass(frozen=True, slots=True)
class Applied:
    """One tag as it sits on a file or folder, with everything a client needs to show it."""

    tag: Tag
    provenance: str
    user_id: UUID | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------------- files


async def tags_of_file(connection: AsyncConnection, file_id: UUID) -> list[Applied]:
    """Every tag on one file, in name order.

    Rejections are absent on purpose: a `rejected` row means the tag does not belong to the
    file, and listing it would put a tag in the response that the file does not carry.
    """
    rows = await connection.execute(
        select(
            tag.c.id,
            tag_name.c.name,
            tag_name.c.name_key,
            tag.c.status,
            tag.c.created_at.label("tag_created_at"),
            tag.c.created_by,
            file_tag.c.provenance,
            file_tag.c.user_id,
            file_tag.c.created_at,
            file_tag.c.updated_at,
        )
        .select_from(file_tag)
        .join(tag, tag.c.id == file_tag.c.tag_id)
        .join(tag_name, (tag_name.c.tag_id == tag.c.id) & ~tag_name.c.is_alias)
        .where(file_tag.c.file_id == file_id, file_tag.c.provenance.in_(APPLIED_STATES))
        .order_by(tag_name.c.name_key)
    )
    return [
        Applied(
            tag=Tag(
                id=row.id,
                name=row.name,
                name_key=row.name_key,
                status=row.status,
                created_at=row.tag_created_at,
                created_by=row.created_by,
            ),
            provenance=row.provenance,
            user_id=row.user_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows.all()
    ]


async def apply_to_file(
    connection: AsyncConnection, *, file_id: UUID, tag_id: UUID, user_id: UUID, actor: Actor
) -> Applied:
    """Put a tag on a file by hand (F-003/FR-2). Idempotent, and it overrides a rejection.

    Re-applying a tag the file already carries changes nothing and records nothing — a POST
    that says what is already true is not an edit, and rewriting the attribution would take
    Bob's tag away from Bob. Applying one the caller previously *rejected* is the opposite: an
    explicit change of mind, so the row flips to `manual` with the new author.
    """
    found = await tags.get(connection, tag_id)
    if found is None:
        raise tags.UnknownTagError(tag_id)
    if not found.is_vocabulary:
        raise NotVocabularyError(tag_id, found.status)

    existing = await _file_row(connection, file_id=file_id, tag_id=tag_id)
    if existing is not None and existing.provenance in APPLIED_STATES:
        return _as_applied(found, existing)

    if existing is not None:
        await connection.execute(
            update(file_tag)
            .where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
            .values(provenance="manual", user_id=user_id, updated_at=func.now())
        )
    else:
        await connection.execute(
            insert(file_tag).values(
                file_id=file_id, tag_id=tag_id, provenance="manual", user_id=user_id
            )
        )
    await events.record(
        connection,
        action=events.FILE_TAGGED,
        resource_type=events.RESOURCE_FILE,
        resource_id=file_id,
        actor=actor,
        details={
            "tag": found.name,
            "tag_id": str(tag_id),
            "provenance": "manual",
            # What it said before, so the audit trail reads as a transition rather than a
            # sequence of unrelated facts (F-003/FR-9).
            "was": existing.provenance if existing is not None else None,
        },
    )
    written = await _file_row(connection, file_id=file_id, tag_id=tag_id)
    if written is None:  # pragma: no cover - written in this transaction
        raise RuntimeError(f"tag {tag_id} vanished from file {file_id}")
    return _as_applied(found, written)


async def remove_from_file(
    connection: AsyncConnection, *, file_id: UUID, tag_id: UUID, actor: Actor
) -> bool:
    """Take a hand-applied tag off a file. `False` when it was not there.

    Removing a *machine's* claim is a different act — it leaves a `rejected` record so no later
    generation re-adds it (F-003/FR-5) — and it arrives with the machine claims themselves.
    Until then there is nothing on a file that reprocessing could bring back.
    """
    existing = await _file_row(connection, file_id=file_id, tag_id=tag_id)
    if existing is None or existing.provenance not in APPLIED_STATES:
        return False
    found = await tags.get(connection, tag_id)
    await connection.execute(
        delete(file_tag).where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
    )
    await events.record(
        connection,
        action=events.FILE_UNTAGGED,
        resource_type=events.RESOURCE_FILE,
        resource_id=file_id,
        actor=actor,
        details={
            "tag": found.name if found is not None else None,
            "tag_id": str(tag_id),
            "was": existing.provenance,
        },
    )
    return True


# ------------------------------------------------------------------------------- folders


async def tags_of_folder(connection: AsyncConnection, folder_id: UUID) -> list[Applied]:
    rows = await connection.execute(
        select(
            tag.c.id,
            tag_name.c.name,
            tag_name.c.name_key,
            tag.c.status,
            tag.c.created_at.label("tag_created_at"),
            tag.c.created_by,
            folder_tag.c.user_id,
            folder_tag.c.created_at,
        )
        .select_from(folder_tag)
        .join(tag, tag.c.id == folder_tag.c.tag_id)
        .join(tag_name, (tag_name.c.tag_id == tag.c.id) & ~tag_name.c.is_alias)
        .where(folder_tag.c.folder_id == folder_id)
        .order_by(tag_name.c.name_key)
    )
    return [
        Applied(
            tag=Tag(
                id=row.id,
                name=row.name,
                name_key=row.name_key,
                status=row.status,
                created_at=row.tag_created_at,
                created_by=row.created_by,
            ),
            provenance="manual",
            user_id=row.user_id,
            created_at=row.created_at,
            updated_at=row.created_at,
        )
        for row in rows.all()
    ]


async def apply_to_folder(
    connection: AsyncConnection, *, folder_id: UUID, tag_id: UUID, user_id: UUID, actor: Actor
) -> Applied:
    """Tag a directory (F-015/FR-9). Same vocabulary as files, none of the machinery."""
    found = await tags.get(connection, tag_id)
    if found is None:
        raise tags.UnknownTagError(tag_id)
    if not found.is_vocabulary:
        raise NotVocabularyError(tag_id, found.status)

    existing = await _folder_row(connection, folder_id=folder_id, tag_id=tag_id)
    if existing is not None:
        return _as_folder_applied(found, existing)

    await connection.execute(
        insert(folder_tag).values(folder_id=folder_id, tag_id=tag_id, user_id=user_id)
    )
    await events.record(
        connection,
        action=events.FOLDER_TAGGED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=folder_id,
        actor=actor,
        details={"tag": found.name, "tag_id": str(tag_id)},
    )
    written = await _folder_row(connection, folder_id=folder_id, tag_id=tag_id)
    if written is None:  # pragma: no cover - written in this transaction
        raise RuntimeError(f"tag {tag_id} vanished from folder {folder_id}")
    return _as_folder_applied(found, written)


async def remove_from_folder(
    connection: AsyncConnection, *, folder_id: UUID, tag_id: UUID, actor: Actor
) -> bool:
    existing = await _folder_row(connection, folder_id=folder_id, tag_id=tag_id)
    if existing is None:
        return False
    found = await tags.get(connection, tag_id)
    await connection.execute(
        delete(folder_tag).where(folder_tag.c.folder_id == folder_id, folder_tag.c.tag_id == tag_id)
    )
    await events.record(
        connection,
        action=events.FOLDER_UNTAGGED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=folder_id,
        actor=actor,
        details={"tag": found.name if found is not None else None, "tag_id": str(tag_id)},
    )
    return True


# --------------------------------------------------------------------------------- rows


@dataclass(frozen=True, slots=True)
class _Row:
    provenance: str
    user_id: UUID
    created_at: datetime
    updated_at: datetime


async def _file_row(connection: AsyncConnection, *, file_id: UUID, tag_id: UUID) -> _Row | None:
    rows = await connection.execute(
        select(
            file_tag.c.provenance,
            file_tag.c.user_id,
            file_tag.c.created_at,
            file_tag.c.updated_at,
        ).where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
    )
    row = rows.first()
    if row is None:
        return None
    return _Row(
        provenance=row.provenance,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _folder_row(connection: AsyncConnection, *, folder_id: UUID, tag_id: UUID) -> _Row | None:
    rows = await connection.execute(
        select(folder_tag.c.user_id, folder_tag.c.created_at).where(
            folder_tag.c.folder_id == folder_id, folder_tag.c.tag_id == tag_id
        )
    )
    row = rows.first()
    if row is None:
        return None
    return _Row(
        provenance="manual",
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.created_at,
    )


def _as_applied(found: Tag, row: _Row) -> Applied:
    return Applied(
        tag=found,
        provenance=row.provenance,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _as_folder_applied(found: Tag, row: _Row) -> Applied:
    return Applied(
        tag=found,
        provenance="manual",
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.created_at,
    )


__all__ = [
    "APPLIED_STATES",
    "Applied",
    "NotVocabularyError",
    "apply_to_file",
    "apply_to_folder",
    "remove_from_file",
    "remove_from_folder",
    "tags_of_file",
    "tags_of_folder",
]
