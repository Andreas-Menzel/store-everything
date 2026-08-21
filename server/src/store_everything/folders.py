"""Folders: the tree that mirrors the disk, and the closure that makes ancestry a join.

A folder is a first-class object with a UUID, not a path string (F-015): the id survives
rename and move, which is what permission grants, tags and future share links attach to. A
path is *derived* from the chain of names, so nothing can drift out of step with the tree.

Ancestry lives in a **closure table** (the ADR-0006 pattern reused): one row per
ancestor-descendant pair, including a depth-0 row from every folder to itself. That is what
turns "everything under folder F" — the permission filter's hottest question — into a single
indexed join instead of a recursive query per check.

This module carries what a workspace needs to exist: its auto-created root folder
([F-015/FR-1](../../../features/F-015-folders.md)). Creating, renaming and moving folders
follows with the rest of F-015.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, names
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import folder, folder_closure

#: The root folder *is* the workspace root directory, so it has no name of its own. Every
#: path the API derives starts here, which is why an empty name is the honest one: a made-up
#: name would appear in derived paths that do not exist on disk.
ROOT_NAME = ""


@dataclass(frozen=True, slots=True)
class Folder:
    id: UUID
    workspace_id: UUID
    parent_id: UUID | None
    name: str
    depth: int
    created_at: datetime

    @property
    def is_root(self) -> bool:
        return self.parent_id is None


_COLUMNS = (
    folder.c.id,
    folder.c.workspace_id,
    folder.c.parent_id,
    folder.c.name,
    folder.c.depth,
    folder.c.created_at,
)


def _as_folder(row: tuple[UUID, UUID, UUID | None, str, int, datetime]) -> Folder:
    return Folder(*row)


def _root_query(workspace_id: UUID) -> Select[tuple[UUID, UUID, UUID | None, str, int, datetime]]:
    return select(*_COLUMNS).where(
        folder.c.workspace_id == workspace_id, folder.c.parent_id.is_(None)
    )


async def root_of(connection: AsyncConnection, workspace_id: UUID) -> Folder | None:
    """The workspace's root folder, or `None` if it has not been provisioned yet."""
    row = (await connection.execute(_root_query(workspace_id))).first()
    return None if row is None else _as_folder(tuple(row))


async def create_root(
    connection: AsyncConnection, *, workspace_id: UUID, actor: Actor
) -> tuple[Folder, bool]:
    """Create the workspace's root folder. Returns it and whether this call created it.

    Idempotent, because provisioning is a leased operation that may be retried after a crash:
    a partial unique index allows exactly one parent-less folder per workspace, so a second
    attempt converges on the first one's row instead of adding a second root.
    """
    identifier = new_id()
    inserted = (
        await connection.execute(
            pg_insert(folder)
            .values(
                id=identifier,
                workspace_id=workspace_id,
                parent_id=None,
                name=ROOT_NAME,
                name_key=names.comparison_key(ROOT_NAME),
                depth=0,
            )
            .on_conflict_do_nothing(
                # The same predicate as `uq_folder_workspace_root`: a conflict target has to
                # name the partial index it means.
                index_elements=[folder.c.workspace_id],
                index_where=text("parent_id IS NULL"),
            )
            .returning(*_COLUMNS)
        )
    ).first()

    if inserted is None:
        existing = (await connection.execute(_root_query(workspace_id))).one()
        return _as_folder(tuple(existing)), False

    created = _as_folder(tuple(inserted))
    await _link_to_itself(connection, created.id)
    await events.record(
        connection,
        action=events.FOLDER_CREATED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=created.id,
        actor=actor,
        details={"workspace": str(workspace_id), "role": "root"},
    )
    return created, True


async def _link_to_itself(connection: AsyncConnection, folder_id: UUID) -> None:
    """The depth-0 closure row every folder has.

    Without it "the subtree of F" would have to union F with its descendants, and every
    permission check would carry that special case.
    """
    await connection.execute(
        pg_insert(folder_closure)
        .values(ancestor_id=folder_id, descendant_id=folder_id, depth=0)
        .on_conflict_do_nothing()
    )
