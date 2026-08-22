"""Browsing and organising the tree: folders as objects, not path strings.

Three ideas shape this router, and all three come from
[F-015](../../../../features/F-015-folders.md):

1. **Every path is rendered per caller**, from folder ids, at read time — never stored and never
   assembled from a string a client sent. Phase 1 has one permission, so every caller's view
   starts at the workspace root; the function that decides that is `folders.visibility_root`, and
   grants change it without changing a single response shape (FR-12).
2. **A rename and a move are the same operation.** `POST /folders/{id}/move` carries a new parent,
   a new name, or both, because they are one row update and one `rename` on disk — and the four
   things that make it illegal (the workspace root, a cycle, an occupied name, two filesystems)
   each answer with the reason rather than a bare `409`.
3. **Children are one ordered stream, folders first.** A mixed list sorted by size would have to
   order directories by an aggregate that is still converging (FR-8's rollups are eventually
   consistent), and a page's order would shift under a client mid-pagination. So directories come
   first sorted by name, then files sorted by the requested key, with one cursor across the seam.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field

from store_everything import aggregates, files, filestore, folders, names, workspaces
from store_everything.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursor,
    Page,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema
from store_everything.security import CurrentCredential

router = APIRouter(tags=["folders"])

WORKSPACE_FOLDERS_PATH = "/workspaces/{workspace_id}/folders"
FOLDERS_PATH = "/folders"

#: How a folder's files may be ordered. Subfolders always sort by name — see the module docstring.
type Ordering = Literal["name", "size", "modified"]

#: Which segment of the mixed listing a cursor is in, and how many parts it carries.
_FOLDER_SEGMENT = "folder"
_FILE_SEGMENT = "file"
_CURSOR_PARTS = 4


class FolderAggregates(BaseSchema):
    """How much a folder holds ([F-015/FR-8](../../../../features/F-015-folders.md)).

    Two different guarantees in one object, and the field names are the only place a client can
    learn which is which — so they say so."""

    direct_files: int
    """Files in this folder itself. **Exact**: one indexed count, computed for this response."""

    total_files: int
    """Files anywhere beneath it, this folder included. Eventually consistent."""

    total_bytes: int
    """What those files' current versions add up to. Version history and trashed content are
    storage this number deliberately does not report — they have their own categories in
    `/stats/storage`."""

    as_of: datetime
    """When this workspace's queue of pending changes was last empty. Not "when these numbers
    were touched": a folder nothing has changed for a week is not stale."""

    pending: bool
    """Whether a change beneath this folder is still queued. A client that needs the exact
    number polls until this is `false` rather than guessing at the window."""


class FolderSummary(BaseSchema):
    id: UUID
    workspace: UUID
    parent: UUID | None
    """Absent for the caller's own visibility root — above it, nothing exists for them
    ([F-015/FR-12](../../../../features/F-015-folders.md))."""

    name: str
    path: str
    """Rendered for this caller, from the folder chain (F-015/FR-12)."""

    depth: int
    created_at: datetime
    aggregates: FolderAggregates


class ChildFolder(BaseSchema):
    kind: Literal["folder"] = "folder"
    id: UUID
    name: str
    path: str
    created_at: datetime


class ChildFile(BaseSchema):
    kind: Literal["file"] = "file"
    id: UUID
    name: str
    path: str
    size: int
    content_hash: str
    media_type: str
    media_class: Literal["image", "video", "audio", "document", "archive", "other"]
    modified_at: datetime | None
    created_at: datetime


type Child = ChildFolder | ChildFile


class FolderCreateRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=names.MAX_NAME_BYTES)
    parent: UUID | None = None
    """Where to create it; absent means the workspace root."""


class FolderMoveRequest(BaseSchema):
    parent: UUID | None = None
    """The new parent — in any workspace the caller owns. Absent keeps the current one."""

    name: str | None = None
    """The new name. Absent keeps the current one, which makes this a pure move."""


def _not_found() -> ProblemException:
    """One answer for absent, someone else's, and above the caller's visibility root (08)."""
    return ProblemException(status=404, slug="not-found", title="Not found")


def _conflict(detail: str) -> ProblemException:
    return ProblemException(status=409, slug="conflict", title="Conflict", detail=detail)


def _invalid(reason: str, pointer: str) -> ProblemException:
    return ProblemException(
        status=422,
        slug="validation",
        title="Validation failed",
        detail="1 request field(s) are invalid.",
        errors=[FieldProblem(detail=reason, pointer=pointer)],
    )


async def _owned_workspace(
    connection: DatabaseConnection, workspace_id: UUID, credential: CurrentCredential
) -> workspaces.Workspace:
    """The caller's workspace, ready to hold folders. `404` unless it is theirs."""
    workspace = await workspaces.get(connection, workspace_id)
    if workspace is None or workspace.owner_id != credential.user.id:
        raise _not_found()
    if not workspace.is_active:
        raise _conflict(
            "This workspace is still being provisioned; its storage does not exist yet."
        )
    return workspace


async def _readable(
    connection: DatabaseConnection, folder_id: UUID, credential: CurrentCredential
) -> tuple[folders.Folder, workspaces.Workspace, UUID]:
    """The folder, its workspace, and the caller's visibility root — or `404`.

    The visibility root comes back with the folder because every path in the response is rendered
    from it, and because a folder *above* it must be indistinguishable from one that does not
    exist (F-015/FR-12).
    """
    found = await folders.get(connection, folder_id)
    if found is None:
        raise _not_found()
    workspace = await workspaces.get(connection, found.workspace_id)
    if workspace is None or workspace.owner_id != credential.user.id:
        raise _not_found()
    root = await folders.visibility_root(
        connection, workspace_id=found.workspace_id, viewer=credential.user.id
    )
    if root is None:
        raise _conflict("This workspace has no folder tree yet; provisioning has not finished.")
    if not await folders.contains(connection, ancestor_id=root, descendant_id=found.id):
        # Above the caller's root: for them it does not exist at all.
        raise _not_found()
    return found, workspace, root


async def _summarize(
    connection: DatabaseConnection, found: folders.Folder, *, root: UUID
) -> FolderSummary:
    counted = await aggregates.totals(connection, found.id)
    return FolderSummary(
        id=found.id,
        workspace=found.workspace_id,
        # The root of what this caller can see has no parent *for them*, whatever the tree says.
        parent=None if found.id == root else found.parent_id,
        name=found.name,
        path=await folders.path_of(connection, found, relative_to=root),
        depth=found.depth,
        created_at=found.created_at,
        aggregates=FolderAggregates(
            direct_files=counted.direct_files,
            total_files=counted.total_files,
            total_bytes=counted.total_bytes,
            as_of=counted.as_of,
            pending=counted.pending,
        ),
    )


# ------------------------------------------------------------------------- endpoints


@router.post(
    WORKSPACE_FOLDERS_PATH,
    summary="Create a folder",
    status_code=201,
    response_model=FolderSummary,
    responses={
        404: {"description": "No such workspace, or not yours"},
        409: {"description": "Something already holds that name"},
        422: {"description": "The name was refused"},
    },
)
async def create_folder(
    workspace_id: UUID,
    payload: FolderCreateRequest,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> FolderSummary:
    """Create one folder, on disk and in the index — the directory is the point of a folder."""
    workspace = await _owned_workspace(connection, workspace_id, credential)
    try:
        names.validate_name(payload.name)
    except names.InvalidNameError as invalid:
        raise _invalid(invalid.reason, "/body/name") from invalid

    parent = await _parent_for(connection, workspace, payload.parent, credential)
    try:
        created = await folders.create(
            connection,
            workspace_id=workspace.id,
            parent=parent,
            name=names.normalize_api_name(payload.name),
            root_path=workspace.root_path,
            actor=Actor.user(credential.user.id),
        )
    except folders.CollisionError as taken:
        raise _conflict(f"A folder named {taken.name!r} is already there.") from taken
    except folders.NameTakenError as taken:
        raise _conflict(f"A file named {taken.name!r} is already there.") from taken
    except FileExistsError as taken:
        # A `mkdir` refused by the filesystem: something the index does not know about holds the
        # name — an unregistered file, most likely. Reported, never overwritten (ADR-0019).
        raise _conflict("Something of that name is already on the storage.") from taken

    root = await folders.visibility_root(
        connection, workspace_id=workspace.id, viewer=credential.user.id
    )
    assert root is not None  # noqa: S101 - `_owned_workspace` refused a workspace without a tree
    return await _summarize(connection, created, root=root)


async def _parent_for(
    connection: DatabaseConnection,
    workspace: workspaces.Workspace,
    parent_id: UUID | None,
    credential: CurrentCredential,
) -> folders.Folder:
    """The folder to create in, or the workspace root when none was named."""
    if parent_id is None:
        root = await folders.root_of(connection, workspace.id)
        if root is None:
            raise _conflict("This workspace has no folder tree yet; provisioning has not finished.")
        return root
    parent, holding, _ = await _readable(connection, parent_id, credential)
    if holding.id != workspace.id:
        raise _invalid("that folder is in another workspace", "/body/parent")
    return parent


@router.get(
    f"{FOLDERS_PATH}/{{folder_id}}",
    summary="Read one folder",
    response_model=FolderSummary,
    responses={404: {"description": "No such folder, or not yours"}},
)
async def read_folder(
    folder_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> FolderSummary:
    found, _, root = await _readable(connection, folder_id, credential)
    return await _summarize(connection, found, root=root)


@router.get(
    f"{FOLDERS_PATH}/{{folder_id}}/children",
    summary="List what is in a folder",
    response_model=Page[Child],
    responses={
        404: {"description": "No such folder, or not yours"},
        422: {"description": "The cursor or the ordering was refused"},
    },
)
async def list_children(
    folder_id: UUID,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    sort: Annotated[
        Ordering,
        Query(description="How to order **files**; subfolders always sort by name."),
    ] = "name",
    cursor: Annotated[str | None, Query(description="Opaque; from a previous page.")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Page[Child]:
    """Subfolders first, then files, as one cursor-paginated stream.

    A page is filled across the seam rather than cut short at it: a folder with three subfolders
    and a thousand files returns the three and then ninety-seven files, not a page of three.
    """
    found, _, root = await _readable(connection, folder_id, credential)
    here = await folders.path_of(connection, found, relative_to=root)
    segment, sorted_by, key, identifier = _position(cursor, sort)

    items: list[Child] = []
    if segment == _FOLDER_SEGMENT:
        page = await folders.children(
            connection,
            parent_id=found.id,
            limit=limit + 1,
            after=None if identifier is None else (key, identifier),
        )
        if len(page) > limit:
            last = page[limit - 1]
            return Page(
                data=[_as_child_folder(child, here) for child in page[:limit]],
                next_cursor=encode_keyset_cursor(
                    [_FOLDER_SEGMENT, sorted_by, folders.sort_key_of(last), str(last.id)]
                ),
            )
        items = [_as_child_folder(child, here) for child in page]
        key, identifier = "", None

    remaining = limit - len(items)
    page_of_files = await files.page_in_folder(
        connection,
        folder_id=found.id,
        sort=sorted_by,
        limit=remaining + 1,
        after=None if identifier is None else (key, identifier),
    )
    items.extend(_as_child_file(known, here) for known in page_of_files[:remaining])
    if len(page_of_files) <= remaining:
        return Page(data=items, next_cursor=None)

    last_file = page_of_files[remaining - 1]
    return Page(
        data=items,
        next_cursor=encode_keyset_cursor(
            [
                _FILE_SEGMENT,
                sorted_by,
                files.sort_value_of(last_file, sorted_by),
                str(last_file.file.id),
            ]
        ),
    )


def _position(cursor: str | None, sort: str) -> tuple[str, str, str, UUID | None]:
    """Where to resume, from an opaque cursor — or the beginning of the folder segment.

    A cursor carries the ordering it was made under, and a request that changes `sort` mid-way
    through is refused rather than silently re-ordered: the client would otherwise skip or repeat
    rows at the seam and have no way to notice.
    """
    if cursor is None:
        return _FOLDER_SEGMENT, sort, "", None
    segment, sorted_by, key, identifier = decode_keyset_cursor(cursor, parts=_CURSOR_PARTS)
    if segment not in {_FOLDER_SEGMENT, _FILE_SEGMENT} or sorted_by != sort:
        raise InvalidCursor()
    try:
        return segment, sorted_by, key, UUID(identifier)
    except ValueError as invalid:
        raise InvalidCursor() from invalid


def _as_child_folder(found: folders.Folder, here: str) -> ChildFolder:
    return ChildFolder(
        id=found.id,
        name=found.name,
        path=f"{here}/{found.name}" if here else found.name,
        created_at=found.created_at,
    )


def _as_child_file(known: files.Known, here: str) -> ChildFile:
    return ChildFile(
        id=known.file.id,
        name=known.file.name,
        path=f"{here}/{known.file.name}" if here else known.file.name,
        size=known.version.size_bytes,
        content_hash=known.version.content_hash,
        media_type=known.version.media_type,
        media_class=known.version.media_class,  # pyright: ignore[reportArgumentType]
        modified_at=known.version.modified_at,
        created_at=known.file.created_at,
    )


@router.post(
    f"{FOLDERS_PATH}/{{folder_id}}/move",
    summary="Rename or move a folder",
    response_model=FolderSummary,
    responses={
        404: {"description": "No such folder, or not yours"},
        409: {"description": "The root, a cycle, an occupied name, or two filesystems"},
        422: {"description": "The name or the destination was refused"},
    },
)
async def move_folder(
    folder_id: UUID,
    payload: FolderMoveRequest,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> FolderSummary:
    """A rename and a move are one operation: one row update, one `rename` on disk.

    Cross-workspace moves are supported while both roots live on the same filesystem, which is
    what makes the move a single atomic rename. Across filesystems it would be a copy of every
    byte under the folder — a resumable operation rather than a request — so it is refused with
    that reason (F-015/FR-4).
    """
    found, workspace, root = await _readable(connection, folder_id, credential)
    name = found.name if payload.name is None else names.normalize_api_name(payload.name)
    if payload.name is not None:
        try:
            names.validate_name(name)
        except names.InvalidNameError as invalid:
            raise _invalid(invalid.reason, "/body/name") from invalid

    parent = found
    destination = workspace
    if payload.parent is not None:
        parent, destination, _ = await _readable(connection, payload.parent, credential)
    elif found.parent_id is not None:
        holding = await folders.get(connection, found.parent_id)
        if holding is None:  # pragma: no cover - a non-root folder always has its parent
            raise _not_found()
        parent = holding

    try:
        moved = await folders.relocate(
            connection,
            found=found,
            parent=parent,
            name=name,
            source_root=workspace.root_path,
            destination_root=destination.root_path,
            actor=Actor.user(credential.user.id),
        )
    except folders.RootFolderError as refused:
        raise _conflict(
            "The workspace root is the workspace itself; rename the workspace instead."
        ) from refused
    except folders.CycleError as refused:
        raise _conflict("A folder cannot be moved inside itself.") from refused
    except folders.CollisionError as taken:
        raise _conflict(f"A folder named {taken.name!r} is already there.") from taken
    except folders.NameTakenError as taken:
        raise _conflict(f"A file named {taken.name!r} is already there.") from taken
    except folders.CrossFilesystemError as refused:
        raise _conflict(
            "These two workspaces are on different filesystems, so their files cannot be moved "
            "by renaming a directory. Copy them instead."
        ) from refused
    except FileExistsError as taken:
        raise _conflict("A directory of that name is already on the storage.") from taken
    except filestore.ContainmentError as escaped:
        raise _not_found() from escaped

    if destination.id != workspace.id:
        # The caller's view of the moved folder is now rooted in the destination workspace.
        root_of_destination = await folders.visibility_root(
            connection, workspace_id=destination.id, viewer=credential.user.id
        )
        assert root_of_destination is not None  # noqa: S101 - `_readable` refused a treeless one
        root = root_of_destination
    return await _summarize(connection, moved, root=root)
