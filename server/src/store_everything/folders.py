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

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import Select, insert, literal, select, text
from sqlalchemy.dialects.postgresql import UUID as UUID_TYPE
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, filestore, names
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import file, folder, folder_closure


class NameTakenError(Exception):
    """A file already occupies the name a folder needs.

    A directory entry is a file or a folder, never both — a rule two tables cannot express as
    one constraint, so it is checked here. Without it the row would insert happily and the
    `mkdir` would fail with `FileExistsError` halfway through an upload.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"a file named {name!r} is already there")
        self.name = name


class WorkspaceNotProvisionedError(Exception):
    """A workspace has no root folder yet, so nothing can be filed into it.

    Raised rather than papered over: the workspace's provisioning operation creates the root
    folder, so this means the upload arrived before the tree existed — a `409`, not a `500`.
    """


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


async def _name_taken_by_file(connection: AsyncConnection, *, parent_id: UUID, name: str) -> bool:
    """Whether a file already holds this name in that folder, compared on the key."""
    found = (
        await connection.execute(
            select(file.c.id)
            .where(file.c.folder_id == parent_id, file.c.name_key == names.comparison_key(name))
            .limit(1)
        )
    ).first()
    return found is not None


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


async def ensure_path(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    root_path: Path,
    segments: Sequence[str],
    actor: Actor,
) -> Folder:
    """The folder at `segments`, creating any that are missing. Returns the deepest one.

    An upload targets a path, so its parent directories have to exist — which makes this the
    first piece of F-015's machinery, deliberately shaped so that F-015's own create, rename
    and move endpoints extend it rather than reimplement it.

    Ordering follows [02 § invariant 8](../../../specs/02-domain-model.md#invariants): the
    directories are created on disk **before** the rows that describe them, so a crash
    between the two leaves an empty directory a scan will adopt, never a folder row pointing
    at a directory that does not exist. All of the filesystem work happens in one thread hop,
    off the event loop (`filestore` is synchronous by design).
    """
    root = await root_of(connection, workspace_id)
    if root is None:
        raise WorkspaceNotProvisionedError(workspace_id)
    if not segments:
        return root

    existing = await _resolve(connection, root, segments)
    if len(existing) == len(segments) + 1:
        return existing[-1]

    await asyncio.to_thread(_make_directories, root_path, segments)

    current = existing[-1]
    for depth, name in enumerate(segments[len(existing) - 1 :], start=len(existing)):
        current = await _create_child(
            connection,
            workspace_id=workspace_id,
            parent=current,
            name=name,
            depth=depth,
            actor=actor,
        )
    return current


async def _resolve(
    connection: AsyncConnection, root: Folder, segments: Sequence[str]
) -> list[Folder]:
    """The chain from the root as far as it already exists, root included."""
    chain = [root]
    for name in segments:
        found = await child_by_name(connection, parent_id=chain[-1].id, name=name)
        if found is None:
            return chain
        chain.append(found)
    return chain


def _make_directories(root_path: Path, segments: Sequence[str]) -> None:
    """Create the whole chain on disk. Blocking, idempotent, containment-checked."""
    target = filestore.resolve_within(root_path, Path(*segments))
    filestore.ensure_directory(target)


async def child_by_name(
    connection: AsyncConnection, *, parent_id: UUID, name: str
) -> Folder | None:
    """A folder's child of that name, compared on the key rather than the raw name."""
    row = (
        await connection.execute(
            select(*_COLUMNS).where(
                folder.c.parent_id == parent_id,
                folder.c.name_key == names.comparison_key(name),
            )
        )
    ).first()
    return None if row is None else _as_folder(tuple(row))


async def _create_child(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    parent: Folder,
    name: str,
    depth: int,
    actor: Actor,
) -> Folder:
    """Insert one folder and its closure rows, converging if someone else got there first."""
    if await _name_taken_by_file(connection, parent_id=parent.id, name=name):
        raise NameTakenError(name)

    inserted = (
        await connection.execute(
            pg_insert(folder)
            .values(
                id=new_id(),
                workspace_id=workspace_id,
                parent_id=parent.id,
                name=name,
                name_key=names.comparison_key(name),
                depth=depth,
            )
            .on_conflict_do_nothing(
                index_elements=[folder.c.workspace_id, folder.c.parent_id, folder.c.name_key]
            )
            .returning(*_COLUMNS)
        )
    ).first()
    if inserted is None:
        # Two uploads into the same new directory raced; the loser adopts the winner's row.
        settled = await child_by_name(connection, parent_id=parent.id, name=name)
        if settled is None:  # pragma: no cover - the unique constraint makes this impossible
            raise RuntimeError(f"folder {name!r} vanished between insert and select")
        return settled

    created = _as_folder(tuple(inserted))
    await _link_to_itself(connection, created.id)
    # One statement for the whole ancestry: every ancestor of the parent is an ancestor of the
    # child, one level deeper. This is the closure-table insert, and the reason ancestry stays
    # a single indexed join instead of a recursive query.
    await connection.execute(
        insert(folder_closure).from_select(
            ["ancestor_id", "descendant_id", "depth"],
            select(
                folder_closure.c.ancestor_id,
                literal(created.id, type_=UUID_TYPE),
                folder_closure.c.depth + 1,
            ).where(folder_closure.c.descendant_id == parent.id),
        )
    )
    await events.record(
        connection,
        action=events.FOLDER_CREATED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=created.id,
        actor=actor,
        details={"workspace": str(workspace_id), "name": created.name, "parent": str(parent.id)},
    )
    return created
