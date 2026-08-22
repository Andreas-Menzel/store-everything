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
import errno
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Select,
    any_,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    true,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as UUID_TYPE
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import aggregates, events, filestore, names
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import file, folder, folder_closure, scan_blocked


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


class CollisionError(Exception):
    """A folder of that name is already there. Never merged into (F-015/FR-4)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"a folder named {name!r} is already there")
        self.name = name


class RootFolderError(Exception):
    """The workspace root is not a folder anyone renames, moves or deletes.

    It *is* the workspace's directory, so those are workspace operations (F-015/FR-1) — renaming
    it would mean renaming the workspace, which ADR-0018 handles on its own terms.
    """

    def __init__(self) -> None:
        super().__init__("the workspace root cannot be renamed or moved")


class CycleError(Exception):
    """A folder cannot be moved inside itself — the tree would stop being one."""

    def __init__(self, folder_id: UUID, parent_id: UUID) -> None:
        super().__init__(f"folder {folder_id} contains {parent_id}, so it cannot move into it")
        self.folder_id = folder_id
        self.parent_id = parent_id


class CrossFilesystemError(Exception):
    """Two workspace roots on different filesystems cannot exchange a subtree with one rename.

    Refused rather than degraded into a copy: moving terabytes is a long-running, resumable
    operation, not something a request finishes, and pretending otherwise would block a request
    for hours and leave a half-copied tree if it died (F-015/FR-4).
    """

    def __init__(self, source: Path, destination: Path) -> None:
        super().__init__(f"{source} and {destination} are on different filesystems")
        self.source = source
        self.destination = destination


class NotAnAncestorError(Exception):
    """A path was asked for relative to a folder that does not contain it.

    A programming error, and one that would leak an ancestor's name if it fell back to the full
    path — so it fails instead (F-015/FR-13).
    """

    def __init__(self, ancestor_id: UUID, folder_id: UUID) -> None:
        super().__init__(f"{ancestor_id} is not an ancestor of {folder_id}")


#: The root folder *is* the workspace root directory, so it has no name of its own. Every
#: path the API derives starts here, which is why an empty name is the honest one: a made-up
#: name would appear in derived paths that do not exist on disk.
ROOT_NAME = ""

#: The composite foreign key that makes "a file's workspace is its folder's workspace"
#: structural. Named here because a cross-workspace move is the one transaction that has to hold
#: it until commit (migration 0009).
_CONTAINMENT_CONSTRAINT = "fk_file_folder_id_workspace_id_folder"


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
    # A folder's totals start at zero and that is not an approximation — it holds nothing yet.
    await aggregates.initialise(connection, created.id)
    await events.record(
        connection,
        action=events.FOLDER_CREATED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=created.id,
        actor=actor,
        # The same keys as every other `folder.created`, so a consumer of the log never has to
        # ask which shape it is holding. The root's name is genuinely empty — it *is* the
        # workspace's directory — and `role` says why.
        details={"workspace": str(workspace_id), "name": created.name, "role": "root"},
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
    await aggregates.initialise(connection, created.id)
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


async def ensure_child(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    parent: Folder,
    name: str,
    actor: Actor,
) -> Folder:
    """The folder row for a directory that **already exists on disk**, created if missing.

    The scanner's counterpart to `ensure_path`: a scan has just listed the directory, so
    creating anything on the filesystem here would be wrong — at best a no-op, at worst a
    directory conjured for an entry that had vanished between the listing and now.
    """
    found = await child_by_name(connection, parent_id=parent.id, name=name)
    if found is not None:
        return found
    return await _create_child(
        connection,
        workspace_id=workspace_id,
        parent=parent,
        name=name,
        depth=parent.depth + 1,
        actor=actor,
    )


async def get(connection: AsyncConnection, folder_id: UUID) -> Folder | None:
    row = (await connection.execute(select(*_COLUMNS).where(folder.c.id == folder_id))).first()
    return None if row is None else _as_folder(tuple(row))


async def resolve(
    connection: AsyncConnection, *, workspace_id: UUID, segments: Sequence[str]
) -> Folder | None:
    """The folder at `segments`, or `None` if any part of the path is not a folder.

    Read-only, unlike `ensure_path`: a subtree rescan names a directory that must already be
    known, and inventing folder rows for a path the user mistyped would be worse than a `404`.
    """
    current = await root_of(connection, workspace_id)
    for segment in segments:
        if current is None:
            return None
        current = await child_by_name(connection, parent_id=current.id, name=segment)
    return current


# ------------------------------------------------------------------- paths and visibility


async def path_of(
    connection: AsyncConnection, found: Folder, *, relative_to: UUID | None = None
) -> str:
    """The folder's path, derived from its ancestry — never a stored string (02 § folder).

    `relative_to` is [F-015/FR-12](../../../features/F-015-folders.md)'s mechanism: a path is
    rendered **per caller**, starting at the topmost folder that caller may read, because an
    ancestor's name is content — a folder called `Divorce 2026` reveals as much as a document
    does (07 § visibility roots). For an owner that root is the workspace root, which is why an
    owner sees the whole workspace-relative path.

    A `relative_to` that is not an ancestor raises rather than falling back to the full path: in
    phase 4 that mistake is a leak, and a loud failure is the only acceptable direction to fail.
    """
    chain = (
        await connection.execute(
            select(folder.c.id, folder.c.name)
            .join(folder_closure, folder_closure.c.ancestor_id == folder.c.id)
            .where(folder_closure.c.descendant_id == found.id)
            .order_by(folder_closure.c.depth.desc())
        )
    ).all()
    names_in_order = [str(row[1]) for row in chain]
    if relative_to is not None:
        identifiers = [row[0] for row in chain]
        if relative_to not in identifiers:
            raise NotAnAncestorError(relative_to, found.id)
        names_in_order = names_in_order[identifiers.index(relative_to) + 1 :]
    return "/".join(name for name in names_in_order if name)


async def visibility_root(
    connection: AsyncConnection, *, workspace_id: UUID, viewer: UUID
) -> UUID | None:
    """The topmost folder this viewer may read, which every path they see is rendered from.

    Phase 1 has one permission — ownership — so the answer is always the workspace root, and the
    interesting version arrives with grants (F-008). It exists now so that every path the API
    renders already goes through it: when grants land, the change is this function, not every
    response that happens to contain a path.
    """
    del viewer  # phase 1: ownership is the only permission, so every caller sees the root
    root = await root_of(connection, workspace_id)
    return None if root is None else root.id


# ----------------------------------------------------------------------- the operations


async def create(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    parent: Folder,
    name: str,
    root_path: Path,
    actor: Actor,
) -> Folder:
    """Create one folder under `parent`, on disk and in the index.

    Disk first, then the row (02 § invariant 8): a crash between them leaves an empty directory
    the next scan adopts, where the reverse would leave a folder row pointing at nothing.

    Both refusals are decided **before** the filesystem is touched, because a `mkdir` that fails
    half-way through would answer with the kernel's reason rather than ours — and a file already
    holding the name is a different fact from a directory already being there.

    A directory that already exists is *adopted*, not refused: creating one is idempotent, which
    is the same rule uploads rely on for their parent directories, and nothing the user has is
    modified by registering it. Whatever is inside it is registered by the next scan.
    """
    if await child_by_name(connection, parent_id=parent.id, name=name) is not None:
        raise CollisionError(name)
    if await _name_taken_by_file(connection, parent_id=parent.id, name=name):
        raise NameTakenError(name)

    parent_path = await path_of(connection, parent)
    segments = [*names.split_path(parent_path), name] if parent_path else [name]
    await asyncio.to_thread(_make_directories, root_path, segments)
    return await _create_child(
        connection,
        workspace_id=workspace_id,
        parent=parent,
        name=name,
        depth=parent.depth + 1,
        actor=actor,
    )


async def relocate(
    connection: AsyncConnection,
    *,
    found: Folder,
    parent: Folder,
    name: str,
    source_root: Path,
    destination_root: Path,
    actor: Actor,
) -> Folder:
    """Rename or move a folder, keeping its identity — and its subtree's.

    One rename on disk moves the whole subtree, however large: the kernel rewrites a directory
    entry, not the bytes under it. The rows then follow — the closure is detached from the old
    ancestors and re-attached under the new parent, and every folder in the subtree has its depth
    shifted by the same delta.

    **Disk before rows, deliberately.** A crash between them leaves the directory moved and the
    index stale, which is a state the app already knows how to fix: the next scan finds the old
    path gone and the new one full, re-matches every file by content
    ([F-001/FR-19](../../../features/F-001-upload-and-import.md)) and transfers the folder's
    identity with them ([F-015/FR-7](../../../features/F-015-folders.md)). The reverse order would
    leave every derived path pointing at a directory that is not there, and the next scan would
    trash the whole subtree.

    Four refusals, and each one names itself: the workspace root is not movable, a folder cannot
    move into its own descendant, a name that is taken at the destination is not merged into, and
    two workspaces on different filesystems cannot exchange a subtree with one rename.
    """
    # `is_root` in its own words: spelled out so that everything below has a parent folder to
    # name rather than a maybe — the aggregates shift between two real chains.
    if found.parent_id is None:
        raise RootFolderError()
    if await _is_ancestor_of(connection, ancestor_id=found.id, descendant_id=parent.id):
        raise CycleError(found.id, parent.id)

    unchanged = parent.id == found.parent_id and names.comparison_key(name) == names.comparison_key(
        found.name
    )
    if not unchanged:
        if await child_by_name(connection, parent_id=parent.id, name=name) is not None:
            raise CollisionError(name)
        if await _name_taken_by_file(connection, parent_id=parent.id, name=name):
            raise NameTakenError(name)

    crossing = parent.workspace_id != found.workspace_id

    # From here on the closure and the folder totals have to agree, and this is the one place
    # that changes both. A rollup expands each queued delta over the ancestors it finds *at that
    # moment*, so a drain running inside this rewrite would file a change against a tree that no
    # longer exists. Holding both workspaces still is the whole correctness argument for
    # F-015/FR-8's arithmetic — and nothing on the upload path takes this lock.
    await aggregates.lock(connection, found.workspace_id, parent.workspace_id)

    old_relative = await path_of(connection, found)
    new_parent_path = await path_of(connection, parent)
    new_relative = f"{new_parent_path}/{name}" if new_parent_path else name

    await asyncio.to_thread(
        _move_on_disk, source_root, old_relative, destination_root, new_relative
    )

    if crossing:
        # Both halves of the containment pair change, and either alone reads as a violation, so
        # this one transaction holds the check until commit (migration 0009).
        await connection.execute(text(f"SET CONSTRAINTS {_CONTAINMENT_CONSTRAINT} DEFERRED"))

    subtree = select(folder_closure.c.descendant_id).where(folder_closure.c.ancestor_id == found.id)
    await _detach(connection, subtree)
    await _attach(connection, folder_id=found.id, parent_id=parent.id)

    depth_shift = (parent.depth + 1) - found.depth
    values: dict[str, Any] = {"depth": folder.c.depth + depth_shift, "updated_at": func.now()}
    if crossing:
        values["workspace_id"] = parent.workspace_id
    await connection.execute(update(folder).where(folder.c.id.in_(subtree)).values(**values))
    if crossing:
        await connection.execute(
            update(file)
            .where(file.c.folder_id.in_(subtree))
            .values(workspace_id=parent.workspace_id)
        )

    moved = _as_folder(
        tuple(
            (
                await connection.execute(
                    update(folder)
                    .where(folder.c.id == found.id)
                    .values(
                        parent_id=parent.id,
                        name=name,
                        name_key=names.comparison_key(name),
                        updated_at=func.now(),
                    )
                    .returning(*_COLUMNS)
                )
            ).one()
        )
    )

    # O(depth), as FR-8 requires: the subtree's own totals leave one parent chain and join the
    # other, and the folders above the two parents' common ancestor see the pair cancel. The
    # subtree's own numbers never move, because they did not change.
    await aggregates.shift(
        connection,
        folder_id=found.id,
        from_parent=found.parent_id,
        from_workspace=found.workspace_id,
        to_parent=parent.id,
        to_workspace=parent.workspace_id,
    )
    await aggregates.schedule(connection, found.workspace_id)
    if crossing:
        await aggregates.schedule(connection, parent.workspace_id)

    renamed = parent.id == found.parent_id
    await events.record(
        connection,
        action=events.FOLDER_RENAMED if renamed else events.FOLDER_MOVED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=moved.id,
        actor=actor,
        details={
            "workspace": str(moved.workspace_id),
            "from": old_relative,
            "to": new_relative,
            **({"from_workspace": str(found.workspace_id)} if crossing else {}),
        },
    )
    return moved


def _move_on_disk(
    source_root: Path, old_relative: str, destination_root: Path, new_relative: str
) -> None:
    """The one rename, containment-checked at both ends. Blocking.

    A cross-filesystem rename fails with `EXDEV` and changes nothing, so that is the check —
    asking `st_dev` first would be a second way to learn the same fact, and two ways to learn one
    fact is one too many.
    """
    source = filestore.resolve_within(source_root, Path(old_relative))
    destination = filestore.resolve_within(destination_root, Path(new_relative))
    try:
        filestore.move_entry(source, destination)
    except OSError as refused:
        if refused.errno == errno.EXDEV:
            raise CrossFilesystemError(source_root, destination_root) from refused
        raise


async def stamp_seen(
    connection: AsyncConnection, *, folder_ids: Sequence[UUID], seen_at: datetime
) -> int:
    """Record that a scan accounted for these directories. Returns how many rows it stamped.

    Stamped by **id**, from the parent listing that mentioned them, and with the *run's* start
    rather than `now()` — so "which directories did this run not account for" is an exact
    comparison however long the run took (the `files.stamp_seen` rule, one level up).
    """
    if not folder_ids:
        return 0
    result = await connection.execute(
        update(folder)
        .where(folder.c.id == any_(literal(list(folder_ids), ARRAY(UUID_TYPE))))
        .values(last_seen_at=seen_at)
    )
    return result.rowcount


async def vanished(
    connection: AsyncConnection, *, folder_id: UUID, run_id: UUID, started_at: datetime
) -> bool:
    """Whether this run found no directory for this folder — and was in a position to say so.

    Three conditions, and each is the folder-level spelling of a rule the file sweep already
    obeys ([F-001/FR-6](../../../features/F-001-upload-and-import.md), FR-16):

    - **Not accounted for by this run.** Its parent's listing did not mention it.
    - **Registered before the run started.** A directory created while the traversal was walking
      was never going to be listed by a parent the pass had already read.
    - **Not under a directory this run could not read.** "I could not look" is not "it is not
      there" — and here the consequence would be handing a live folder's identity away.
    """
    under_blocked = folder_closure.alias("blocked_closure")
    blocked = (
        select(scan_blocked.c.folder_id)
        .join(under_blocked, under_blocked.c.ancestor_id == scan_blocked.c.folder_id)
        .where(scan_blocked.c.run_id == run_id, under_blocked.c.descendant_id == folder.c.id)
    )
    found = (
        await connection.execute(
            select(folder.c.id).where(
                folder.c.id == folder_id,
                or_(folder.c.last_seen_at.is_(None), folder.c.last_seen_at < started_at),
                folder.c.created_at < started_at,
                ~blocked.exists(),
            )
        )
    ).first()
    return found is not None


async def reposition(
    connection: AsyncConnection,
    *,
    found: Folder,
    parent: Folder,
    name: str,
    actor: Actor,
    detected: str,
) -> Folder:
    """Move a folder's rows to where its directory already is. **Touches no filesystem.**

    `relocate`'s second half, and only its second half. There the app moves the directory and the
    rows follow; here the directory moved without the app — the disk is already the destination —
    so doing anything to it would be the app editing the user's tree to match its own index
    ([ADR-0003](../../../decisions/ADR-0003-files-on-disk-source-of-truth.md)).

    The caller owns the refusals and the ordering. This is deliberately not `relocate` with a flag:
    the two differ in what they may touch, which is exactly the kind of thing a flag hides.
    """
    old_relative = await path_of(connection, found)
    subtree = select(folder_closure.c.descendant_id).where(folder_closure.c.ancestor_id == found.id)
    if parent.id != found.parent_id:
        await _detach(connection, subtree)
        await _attach(connection, folder_id=found.id, parent_id=parent.id)
        depth_shift = (parent.depth + 1) - found.depth
        await connection.execute(
            update(folder)
            .where(folder.c.id.in_(subtree))
            .values(depth=folder.c.depth + depth_shift, updated_at=func.now())
        )

    moved = _as_folder(
        tuple(
            (
                await connection.execute(
                    update(folder)
                    .where(folder.c.id == found.id)
                    .values(
                        parent_id=parent.id,
                        name=name,
                        name_key=names.comparison_key(name),
                        updated_at=func.now(),
                    )
                    .returning(*_COLUMNS)
                )
            ).one()
        )
    )
    await events.record(
        connection,
        action=events.FOLDER_RENAMED if parent.id == found.parent_id else events.FOLDER_MOVED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=moved.id,
        actor=actor,
        details={
            "workspace": str(moved.workspace_id),
            "from": old_relative,
            "to": await path_of(connection, moved),
            # The same key `file.moved` carries, for the same distinction: whether a person asked
            # for this or the app recognised it after the fact.
            "detected": detected,
        },
    )
    return moved


async def absorb(connection: AsyncConnection, *, into: Folder, discarded: Folder) -> None:
    """Move everything one folder holds into another, so the emptied one can be deleted.

    Files change folder, subfolders change parent, and the closure is rewritten for each of them.
    Nothing on disk moves: this is two rows describing one directory, and only one may survive.
    """
    await connection.execute(
        update(file)
        .where(file.c.folder_id == discarded.id)
        .values(folder_id=into.id, updated_at=func.now())
    )
    children = (
        await connection.execute(select(*_COLUMNS).where(folder.c.parent_id == discarded.id))
    ).all()
    for row in children:
        child = _as_folder(tuple(row))
        subtree = select(folder_closure.c.descendant_id).where(
            folder_closure.c.ancestor_id == child.id
        )
        await _detach(connection, subtree)
        await _attach(connection, folder_id=child.id, parent_id=into.id)
        depth_shift = (into.depth + 1) - child.depth
        await connection.execute(
            update(folder)
            .where(folder.c.id.in_(subtree))
            .values(depth=folder.c.depth + depth_shift, updated_at=func.now())
        )
        await connection.execute(
            update(folder)
            .where(folder.c.id == child.id)
            .values(parent_id=into.id, updated_at=func.now())
        )


async def discard(connection: AsyncConnection, folder_id: UUID) -> None:
    """Delete a folder row that describes the same directory as another. Nothing on disk.

    Only ever the *new* row of an identity transfer, and only once `absorb` has emptied it: its
    closure rows go with it, and so would anything else that pointed at it.
    """
    await connection.execute(delete(folder).where(folder.c.id == folder_id))


async def contains(connection: AsyncConnection, *, ancestor_id: UUID, descendant_id: UUID) -> bool:
    """Whether the first contains the second, itself included — one indexed read.

    The closure's whole purpose, and the same question two very different callers ask: the cycle
    check before a move, and "is this inside what the caller may see?" before a response
    ([F-015/FR-12](../../../features/F-015-folders.md)).
    """
    return await _is_ancestor_of(connection, ancestor_id=ancestor_id, descendant_id=descendant_id)


async def _is_ancestor_of(
    connection: AsyncConnection, *, ancestor_id: UUID, descendant_id: UUID
) -> bool:
    """Whether the first contains the second — the cycle check, and one indexed read."""
    return bool(
        (
            await connection.execute(
                select(
                    select(folder_closure.c.depth)
                    .where(
                        folder_closure.c.ancestor_id == ancestor_id,
                        folder_closure.c.descendant_id == descendant_id,
                    )
                    .exists()
                )
            )
        ).scalar_one()
    )


async def _detach(connection: AsyncConnection, subtree: Select[tuple[UUID]]) -> None:
    """Cut every link that reaches into the subtree from outside it, keeping the inside intact."""
    await connection.execute(
        delete(folder_closure).where(
            folder_closure.c.descendant_id.in_(subtree),
            folder_closure.c.ancestor_id.not_in(subtree),
        )
    )


async def _attach(connection: AsyncConnection, *, folder_id: UUID, parent_id: UUID) -> None:
    """Link every ancestor of the new parent to every folder in the subtree, at the right depth."""
    above = folder_closure.alias("above")
    below = folder_closure.alias("below")
    # A deliberate cross join, spelled as one: every ancestor of the new parent pairs with every
    # folder in the subtree, and the two `WHERE` clauses are what bound each side.
    await connection.execute(
        insert(folder_closure).from_select(
            ["ancestor_id", "descendant_id", "depth"],
            select(
                above.c.ancestor_id,
                below.c.descendant_id,
                above.c.depth + below.c.depth + 1,
            )
            .select_from(above.join(below, true()))
            .where(above.c.descendant_id == parent_id, below.c.ancestor_id == folder_id),
        )
    )


async def children(
    connection: AsyncConnection,
    *,
    parent_id: UUID,
    limit: int,
    after: tuple[str, UUID] | None = None,
) -> list[Folder]:
    """One page of subfolders, ordered by comparison key so the order is case-insensitive.

    Keyset-anchored on `(name_key, id)`: a page stays correct while folders are created and
    removed around it (08 § pagination).
    """
    query = (
        select(*_COLUMNS)
        .where(folder.c.parent_id == parent_id)
        .order_by(folder.c.name_key, folder.c.id)
        .limit(limit)
    )
    if after is not None:
        key, identifier = after
        query = query.where(
            tuple_(folder.c.name_key, folder.c.id)
            > tuple_(literal(key), literal(identifier, type_=UUID_TYPE))
        )
    rows = (await connection.execute(query)).all()
    return [_as_folder(tuple(row)) for row in rows]


def sort_key_of(found: Folder) -> str:
    """The value a cursor carries for this folder — its comparison key, not its raw name."""
    return names.comparison_key(found.name)
