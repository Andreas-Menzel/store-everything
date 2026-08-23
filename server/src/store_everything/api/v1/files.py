"""Reading a file: its metadata, and its untouched bytes.

`/content` serves the original file exactly as it is on disk — that is ADR-0003's promise, and
it is also why this endpoint needs care. Serving user-supplied content from the app's own
origin is how a personal cloud grows an XSS hole: an uploaded `.html` is honest HTML, and
`nosniff` does nothing about that. So content is served **as a download unless its type is
inert to render** (images other than SVG, audio, video, plain text), always with
`Content-Security-Policy: default-src 'none'; sandbox` and the app's global `nosniff`. Inline
viewing of documents belongs to the rendition path (ADR-0008), where the bytes we serve are
ones we generated.

Two cheap wins come for free with the content hash: it is the `ETag`, so a client that already
has the bytes revalidates into a `304`
([F-026/FR-25](../../../../features/F-026-offline-cache-and-prefetch.md)), and Starlette's
`FileResponse` handles `Range`, `If-Range` and `416` properly, so streaming a video or letting
an extractor read one byte range is framework work rather than ours.
"""

from __future__ import annotations

import asyncio
import errno
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from store_everything import (
    aggregates,
    extraction,
    files,
    filestore,
    folders,
    mediatypes,
    names,
    trash,
    workspaces,
)
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema
from store_everything.security import CurrentCredential

router = APIRouter(prefix="/files", tags=["files"])

#: Types a browser may render in place without becoming a script host. SVG is deliberately
#: absent: it is a document format that can carry script, whatever its `image/` prefix says.
_INLINE_FAMILIES = frozenset({"image", "audio", "video"})
_INLINE_EXCEPTIONS = frozenset({"image/svg+xml"})

#: Belt and braces for the one response that carries content we did not write.
_CONTENT_SECURITY = "default-src 'none'; sandbox"


class TrashInfo(BaseSchema):
    """Why a file is in the trash, and until when
    ([F-014/FR-3](../../../../features/F-014-deletion-and-trash.md))."""

    origin: Literal["in_app", "detected_on_disk"]
    """`detected_on_disk` is the badge a client renders as "removed outside the app"."""

    trashed_at: datetime
    purge_after: datetime
    """Nothing removes the entry before this, ever (F-014/FR-9)."""

    batch: UUID
    """Everything one deletion removed, restorable together — for a re-scan, the run's id."""

    restorable: bool
    """Whether the app still holds any of this file's content. `false` for the ordinary
    external deletion: the bytes were on the storage and the storage no longer has them."""


class FileSummary(BaseSchema):
    id: UUID
    workspace: UUID
    path: str
    """Workspace-relative, derived from the folder chain — never a stored string (02 § file)."""

    name: str
    size: int
    content_hash: str
    digest_algorithm: Literal["sha256"] = "sha256"
    media_type: str
    media_class: Literal["image", "video", "audio", "document", "archive", "other"]
    version: UUID
    """The current version's id — what a pinned thumbnail URL and a segment query are about."""

    extraction_status: extraction.Status
    """Where content analysis stands for the current version
    ([F-001/FR-8](../../../../features/F-001-upload-and-import.md)): `pending` from the moment
    the file lands until every matching extractor has finished; `none` when nothing analyses
    this type. Details per extractor: `GET /files/{id}/extraction`."""

    state: Literal["live", "trashed"]
    created_at: datetime
    modified_at: datetime | None
    """The file's own timestamp on disk, which is what a later scan compares against."""

    trash: TrashInfo | None = None
    """Present exactly when `state` is `trashed`."""

    @classmethod
    def of(
        cls,
        found: files.File,
        version: files.Version,
        path: str,
        extraction_status: extraction.Status,
        trash: TrashInfo | None = None,
    ) -> FileSummary:
        return cls(
            id=found.id,
            workspace=found.workspace_id,
            path=path,
            name=found.name,
            size=version.size_bytes,
            content_hash=version.content_hash,
            media_type=version.media_type,
            media_class=version.media_class,  # pyright: ignore[reportArgumentType]
            version=version.id,
            extraction_status=extraction_status,
            state=found.state,  # pyright: ignore[reportArgumentType]
            created_at=found.created_at,
            modified_at=version.modified_at,
            trash=trash,
        )


class ExtractionRunInfo(BaseSchema):
    """One extractor's run over this version, with the provenance it was stamped with."""

    extractor: str
    state: str
    generation: int
    extractor_version: str | None
    """What actually ran. Absent until the job is claimed — nothing has run yet."""

    model_version: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    """The last failure's message, kept while a retry is pending and after a dead-letter."""


class ExtractionStatus(BaseSchema):
    """Per-file extraction status (04 § status & observability)."""

    version: UUID
    status: extraction.Status
    runs: list[ExtractionRunInfo]


def not_found() -> ProblemException:
    """One answer for absent, someone else's, and purged (08 § errors)."""
    return ProblemException(status=404, slug="not-found", title="Not found")


async def readable(
    connection: DatabaseConnection, *, file_id: UUID, credential: CurrentCredential
) -> tuple[files.File, workspaces.Workspace]:
    """The file and its workspace, or `404`.

    Ownership is the only permission in phase 1 (07): grants and sharing arrive in phase 4,
    and this is the single place that will need to learn about them.
    """
    found = await files.get(connection, file_id)
    if found is None:
        raise not_found()
    workspace = await workspaces.get(connection, found.workspace_id)
    if workspace is None or workspace.owner_id != credential.user.id:
        raise not_found()
    return found, workspace


async def summarize(connection: DatabaseConnection, found: files.File) -> FileSummary:
    version = await files.current_version(connection, found.id)
    if version is None:  # pragma: no cover - a file is registered with its version or not at all
        raise RuntimeError(f"file {found.id} has no current version")
    return FileSummary.of(
        found,
        version,
        await files.path_of(connection, found),
        extraction.status_of(await extraction.runs_for(connection, version.id)),
        await _trash_info(connection, found),
    )


async def _trash_info(connection: DatabaseConnection, found: files.File) -> TrashInfo | None:
    """The trash entry behind a `trashed` state, so a client can say what happened and when.

    Phase 1 has no trash page and no way to delete through the app, but a re-scan can put a file
    in here — so a client asking about one file has to be able to learn that its content was
    removed on the storage rather than see a bare `trashed` with no explanation.
    """
    if found.is_live:
        return None
    entry = await trash.entry_for(connection, found.id)
    if entry is None:  # pragma: no cover - trashing writes the entry in the same transaction
        return None
    return TrashInfo(
        origin=entry.origin,
        trashed_at=entry.trashed_at,
        purge_after=entry.purge_after,
        batch=entry.batch_id,
        restorable=await files.holds_any_content(connection, found.id),
    )


@router.get(
    "/{file_id}",
    summary="Read one file",
    response_model=FileSummary,
    responses={404: {"description": "No such file, or not yours"}},
)
async def read_file(
    file_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> FileSummary:
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    return await summarize(connection, found)


@router.get(
    "/{file_id}/extraction",
    summary="Extraction status of one file",
    response_model=ExtractionStatus,
    responses={404: {"description": "No such file, or not yours"}},
)
async def read_file_extraction(
    file_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> ExtractionStatus:
    """Which extractors ran over this file's current version, and how each one ended.

    The reference an upload response points at (F-001/FR-3): extraction is asynchronous, so what
    a client gets back is not a result but somewhere to ask.
    Per version rather than per file, because that is what a run is about — an older version
    keeps its own runs and its own results (F-007).
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await files.current_version(connection, found.id)
    if version is None:  # pragma: no cover - see `summarize`
        raise RuntimeError(f"file {found.id} has no current version")

    runs = await extraction.runs_for(connection, version.id)
    return ExtractionStatus(
        version=version.id,
        status=extraction.status_of(runs),
        runs=[
            ExtractionRunInfo(
                extractor=run.extractor_id,
                state=run.state,
                generation=run.generation,
                extractor_version=run.extractor_version,
                model_version=run.model_version,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error=run.error,
            )
            for run in runs
        ],
    )


@router.get(
    "/{file_id}/content",
    summary="Download a file's content",
    response_class=FileResponse,
    response_model=None,
    responses={
        200: {"description": "The original bytes, unmodified", "content": {"*/*": {}}},
        206: {"description": "The requested byte range"},
        304: {"description": "The caller's copy matches the current content hash"},
        404: {"description": "No such file, or not yours"},
        410: {"description": "The file is in the trash"},
        416: {"description": "The requested range lies outside the file"},
    },
)
async def read_file_content(
    file_id: UUID,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> Response:
    found, workspace = await readable(connection, file_id=file_id, credential=credential)
    version = await files.current_version(connection, found.id)
    if version is None:  # pragma: no cover - see `summarize`
        raise RuntimeError(f"file {found.id} has no current version")
    if not found.is_live:
        # Distinguished from a `404` on purpose: the file is a real thing with a real history,
        # and "it is in the trash, here is why" is a different answer from "no such file".
        raise ProblemException(
            status=410,
            slug="gone",
            title="Gone",
            detail="This file is in the trash; its content is not served while it is there.",
        )

    etag = f'"{version.content_hash}"'
    headers = {
        "ETag": etag,
        # Storable but always revalidated: the URL serves *the current* version, so a client
        # may keep the bytes and ask whether they are still current (08 § caching).
        "Cache-Control": "private, no-cache",
        "Content-Security-Policy": _CONTENT_SECURITY,
    }
    if etag in {tag.strip() for tag in (request.headers.get("if-none-match") or "").split(",")}:
        return Response(status_code=304, headers=headers)

    relative = await files.path_of(connection, found)
    # Re-resolved on every open, deliberately redundant with the scanner's refusal to follow
    # symlinks: lexical containment is not containment (ADR-0019).
    try:
        absolute = await asyncio.to_thread(
            filestore.resolve_within, workspace.root_path, Path(relative)
        )
        exists = await asyncio.to_thread(absolute.is_file)
    except filestore.ContainmentError as escaped:
        raise not_found() from escaped
    if not exists:
        # The row says the file is there and the filesystem disagrees. Reconciling that is
        # re-scan's job (F-001/FR-6); answering honestly is this endpoint's.
        raise ProblemException(
            status=404,
            slug="not-found",
            title="Not found",
            detail="The content of this file is no longer on the storage.",
        )

    return FileResponse(
        absolute,
        media_type=version.media_type,
        filename=found.name,
        content_disposition_type=_disposition(version.media_type),
        headers=headers,
    )


def _disposition(media_type: str) -> str:
    """`inline` only for types that cannot execute; everything else is a download."""
    normalized = mediatypes.normalize(media_type) or mediatypes.DEFAULT_MEDIA_TYPE
    if normalized in _INLINE_EXCEPTIONS:
        return "attachment"
    if normalized.partition("/")[0] in _INLINE_FAMILIES or normalized == "text/plain":
        return "inline"
    return "attachment"


class FileMoveRequest(BaseSchema):
    folder: UUID | None = None
    """The folder to move it into — in any workspace the caller owns. Absent keeps the current
    one, which makes this a rename."""

    name: str | None = None
    """The new name. Absent keeps the current one, which makes this a pure move."""


@router.post(
    "/{file_id}/move",
    summary="Rename or move a file",
    response_model=FileSummary,
    responses={
        404: {"description": "No such file, or not yours"},
        409: {"description": "An occupied name, or two filesystems"},
        422: {"description": "The name or the destination was refused"},
    },
)
async def move_file(
    file_id: UUID,
    payload: FileMoveRequest,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> FileSummary:
    """Move or rename one file: the operation everything else about identity rests on.

    [F-010/FR-1](../../../../features/F-010-auto-sort-inbox.md) asks for exactly this as a
    first-class operation, because the deferred auto-sorter is going to be its heaviest user and a
    move that lost tags or versions would make sorting destructive. It cannot: the row keeps its
    UUID, and everything hangs off that.

    Disk before rows, like every other move (F-015/FR-4): a crash between them leaves the file at
    its new path with the index still pointing at the old one, which the next scan reconciles by
    content — the reverse order would leave a row pointing at nothing.
    """
    found, workspace = await readable(connection, file_id=file_id, credential=credential)
    if not found.is_live:
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="This file is in the trash; restore it before moving it.",
        )

    name = found.name if payload.name is None else names.normalize_api_name(payload.name)
    destination_folder, destination = await _destination(
        connection,
        found=found,
        workspace=workspace,
        folder_id=payload.folder,
        credential=credential,
    )
    # After the destination is known, and whether or not a new name was asked for: the control
    # directory's name is reserved at a workspace root and ordinary below it, so a move alone
    # can carry a name somewhere it may not be.
    try:
        names.validate_name(name, at_root=destination_folder.is_root)
    except names.InvalidNameError as invalid:
        raise _invalid(
            invalid.reason, "/body/name" if payload.name is not None else "/body/folder"
        ) from invalid

    if await files.find_in_folder(connection, folder_id=destination_folder.id, name=name) not in (
        None,
        found,
    ):
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail=f"A file named {name!r} is already there.",
        )
    if await folders.child_by_name(connection, parent_id=destination_folder.id, name=name):
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail=f"A folder named {name!r} is already there.",
        )

    from_path = await files.path_of(connection, found)
    destination_path = await folders.path_of(connection, destination_folder)
    to_path = f"{destination_path}/{name}" if destination_path else name
    try:
        await asyncio.to_thread(
            _move_bytes, workspace.root_path, from_path, destination.root_path, to_path
        )
    except filestore.ContainmentError as escaped:
        raise not_found() from escaped
    except FileExistsError as taken:
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="Something of that name is already on the storage.",
        ) from taken
    except OSError as refused:
        if refused.errno != errno.EXDEV:
            raise
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail=(
                "These two workspaces are on different filesystems, so the file cannot be moved "
                "by renaming it. Upload it to the new location instead."
            ),
        ) from refused

    moved = await files.relocate(
        connection,
        found=found,
        folder_id=destination_folder.id,
        name=name,
        actor=Actor.user(credential.user.id),
        from_path=from_path,
        detected="api",
        workspace_id=None if destination.id == workspace.id else destination.id,
    )
    if moved.folder_id != found.folder_id:
        # The two deltas `relocate` queued are for these workspaces' rollups, not for this
        # request: a move takes no aggregate lock and does no arithmetic (F-015/FR-8).
        await aggregates.schedule(connection, workspace.id)
        if destination.id != workspace.id:
            await aggregates.schedule(connection, destination.id)
    return await summarize(connection, moved)


async def _destination(
    connection: DatabaseConnection,
    *,
    found: files.File,
    workspace: workspaces.Workspace,
    folder_id: UUID | None,
    credential: CurrentCredential,
) -> tuple[folders.Folder, workspaces.Workspace]:
    """The folder to move into and the workspace holding it, or the file's current folder."""
    if folder_id is None:
        current = await folders.get(connection, found.folder_id)
        if current is None:  # pragma: no cover - a file always has its folder
            raise not_found()
        return current, workspace

    target = await folders.get(connection, folder_id)
    if target is None:
        raise not_found()
    holding = await workspaces.get(connection, target.workspace_id)
    if holding is None or holding.owner_id != credential.user.id:
        raise not_found()
    return target, holding


def _move_bytes(source_root: Path, from_path: str, destination_root: Path, to_path: str) -> None:
    """The one rename, containment-checked at both ends. Blocking."""
    filestore.move_entry(
        filestore.resolve_within(source_root, Path(from_path)),
        filestore.resolve_within(destination_root, Path(to_path)),
    )


def _invalid(reason: str, pointer: str) -> ProblemException:
    return ProblemException(
        status=422,
        slug="validation",
        title="Validation failed",
        detail="1 request field(s) are invalid.",
        errors=[FieldProblem(detail=reason, pointer=pointer)],
    )
