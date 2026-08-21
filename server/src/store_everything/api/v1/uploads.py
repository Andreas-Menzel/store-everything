"""The upload surface: the IETF resumable-upload protocol, and the only way bytes get in.

Five methods over two resources. `OPTIONS`/`POST` on a workspace's file collection create an
upload; `HEAD`/`PATCH`/`DELETE` on `/uploads/{id}` probe, extend and cancel it. A one-request
upload is the ordinary case — a small file pays no extra round trip — and resumption exists
for when it is needed (ADR-0017).

What the code is shaped by, in order of how much it costs to get wrong:

1. **Bytes are durable before an offset is promised.** The append path is
   truncate-to-committed → stream → `fsync` → *then* commit the offset, so the only thing a
   crash can lose is a chunk the client was never told we had.
2. **The target is resolved twice.** Once at creation, to fail fast on a path that is
   occupied or unwritable, and again at finalize, because minutes of upload may have passed
   and the answer can have changed. Only finalize creates anything.
3. **Nothing is published until the hash checks out.** The protocol carries no integrity
   digest (draft-12 removed them), so a client-declared hash is verified against the assembled
   bytes before the atomic rename, and a mismatch fails the session instead of the file.

The interim `104` this protocol would otherwise send is absent, by the draft's own carve-out
for servers that cannot send interim responses — ASGI has no message for one. Clients learn
the upload resource from the `201 Created` instead; see `resumable` and Q58.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from store_everything import (
    files,
    filestore,
    folders,
    mediatypes,
    names,
    resumable,
    uploads,
    workspaces,
)
from store_everything.api.v1.files import FileSummary
from store_everything.config import Settings
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema
from store_everything.security import CurrentCredential, settings_of

router = APIRouter(tags=["uploads"])

UPLOADS_PATH = "/uploads"
WORKSPACE_FILES_PATH = "/workspaces/{workspace_id}/files"


class UploadState(BaseSchema):
    """A session's state, so a client can see what the protocol headers already told it."""

    id: UUID
    workspace: UUID
    path: str
    offset: int
    length: int | None
    complete: bool
    expires_at: datetime

    @classmethod
    def of(cls, session: uploads.Session) -> UploadState:
        return cls(
            id=session.id,
            workspace=session.workspace_id,
            path=session.target_path,
            offset=session.committed_offset,
            length=session.declared_length,
            complete=session.is_complete,
            expires_at=session.expires_at,
        )


# --------------------------------------------------------------------------- helpers


def limits_of(settings: Settings) -> resumable.Limits:
    return resumable.Limits(
        max_size=settings.upload_max_size or None,
        min_append_size=settings.upload_min_append_size,
        max_append_size=settings.upload_max_append_size,
        max_age_seconds=settings.upload_expiry_days * 24 * 3600,
    )


def _not_found(detail: str | None = None) -> ProblemException:
    return ProblemException(status=404, slug="not-found", title="Not found", detail=detail)


def _too_large(limit: int) -> ProblemException:
    return ProblemException(
        status=413,
        slug="payload-too-large",
        title="Payload too large",
        detail=f"This request may carry at most {limit} bytes; see Upload-Limit.",
    )


def _invalid(reason: str, pointer: str) -> ProblemException:
    return ProblemException(
        status=422,
        slug="validation",
        title="Validation failed",
        detail="1 request field(s) are invalid.",
        errors=[FieldProblem(detail=reason, pointer=pointer)],
    )


def _occupied(path: str) -> ProblemException:
    """F-001/FR-7: a collision is refused rather than resolved. On the comparison key, so
    `Report.pdf` collides with `report.pdf` (ADR-0019)."""
    return ProblemException(
        status=409,
        slug="conflict",
        title="Conflict",
        detail=f"Something already exists at {path!r}.",
    )


async def _writable_workspace(
    connection: DatabaseConnection, workspace_id: UUID, credential: CurrentCredential
) -> workspaces.Workspace:
    """The caller's workspace, ready to take files. `404` unless it is theirs."""
    workspace = await workspaces.get(connection, workspace_id)
    if workspace is None or workspace.owner_id != credential.user.id:
        raise _not_found()
    if not workspace.is_active:
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="This workspace is still being provisioned; its storage does not exist yet.",
        )
    return workspace


async def _existing_target(
    connection: DatabaseConnection, workspace: workspaces.Workspace, segments: tuple[str, ...]
) -> None:
    """Refuse now what finalize would refuse later, without creating anything.

    Only folders that already exist are consulted: if the parent chain is incomplete then
    nothing can be in the way, so there is nothing to check.
    """
    *parents, name = segments
    parent = await folders.root_of(connection, workspace.id)
    for depth, segment in enumerate(parents):
        if parent is None:
            return
        # A path cannot lead *through* a file. Caught here so the answer is a `409` rather
        # than a `mkdir` failing halfway through an upload.
        if await files.find_in_folder(connection, folder_id=parent.id, name=segment) is not None:
            raise _occupied("/".join(segments[: depth + 1]))
        parent = await folders.child_by_name(connection, parent_id=parent.id, name=segment)
    if parent is None:
        return

    if await files.find_in_folder(connection, folder_id=parent.id, name=name) is not None:
        raise _occupied("/".join(segments))
    # The other direction: a folder already holds the name this file wants.
    if await folders.child_by_name(connection, parent_id=parent.id, name=name) is not None:
        raise _occupied("/".join(segments))


async def _receive(request: Request, staging: Path, *, limit: int) -> int:
    """Stream the request body into staging, durably. Returns the staged file's size.

    Bounded by `limit` while it reads rather than by trusting `Content-Length`: a client that
    understates its body must not be able to write past the limit. Bytes written before a
    refusal are unacknowledged, so a resume truncates them away.
    """
    handle = await asyncio.to_thread(filestore.open_staging_append, staging)
    received = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > limit:
                raise _too_large(limit)
            await asyncio.to_thread(handle.write, chunk)
        return await asyncio.to_thread(filestore.commit_appended, handle)
    finally:
        await asyncio.to_thread(handle.close)


async def _finalize(
    connection: DatabaseConnection,
    *,
    session: uploads.Session,
    workspace: workspaces.Workspace,
    actor: Actor,
) -> FileSummary:
    """Turn staged bytes into a registered file. The last step, and the only publishing one."""
    segments = names.split_path(session.target_path)
    *parents, name = segments

    await _existing_target(connection, workspace, segments)
    try:
        folder = await folders.ensure_path(
            connection,
            workspace_id=workspace.id,
            root_path=workspace.root_path,
            segments=parents,
            actor=actor,
        )
    except folders.NameTakenError as taken:
        # Something arrived at that name while the upload was in flight.
        raise _occupied(taken.name) from taken
    except folders.WorkspaceNotProvisionedError as unprovisioned:
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="This workspace has no folder tree yet; provisioning has not finished.",
        ) from unprovisioned

    staging = uploads.staging_path(workspace.root_path, session.id)
    destination = await asyncio.to_thread(
        filestore.resolve_within, workspace.root_path, Path(*segments)
    )
    try:
        assembled = await asyncio.to_thread(
            uploads.assemble, staging, destination, declared_hash=session.declared_hash
        )
    except uploads.HashMismatchError as mismatch:
        # The session is finished either way, and saying so has to outlive this failed
        # request — otherwise the client resumes an upload that can never be accepted.
        await uploads.close(connection, session_id=session.id, state="failed")
        await connection.commit()
        raise _invalid(
            "the uploaded content does not match the declared content_hash", "/query/content_hash"
        ) from mismatch

    found, version = await files.register(
        connection,
        workspace_id=workspace.id,
        folder_id=folder.id,
        name=name,
        content_hash=assembled.content_hash,
        size_bytes=assembled.size_bytes,
        media_type=mediatypes.detect(name, session.media_type),
        modified_at=assembled.modified_at,
        origin="upload",
        actor=actor,
    )
    await uploads.complete(connection, session=session, file_id=found.id)
    return FileSummary.of(found, version, await files.path_of(connection, found))


def _protocol_headers(
    session: uploads.Session, limits: resumable.Limits, *, complete: bool
) -> dict[str, str]:
    headers = {
        resumable.OFFSET_HEADER: str(session.committed_offset),
        resumable.COMPLETE_HEADER: resumable.boolean(complete),
        resumable.LIMIT_HEADER: limits.render(),
    }
    if session.declared_length is not None:
        headers[resumable.LENGTH_HEADER] = str(session.declared_length)
    return headers


async def _session_for(
    connection: DatabaseConnection, upload_id: UUID, credential: CurrentCredential, *, lock: bool
) -> uploads.Session:
    """A session the caller may drive, or the right refusal for the state it is in."""
    session = (
        await uploads.locked(connection, upload_id)
        if lock
        else await uploads.get(connection, upload_id)
    )
    if session is None or session.created_by != credential.user.id:
        raise _not_found()
    if session.state == "expired":
        # Distinguished from `404` on purpose: "it expired" tells a client to start over,
        # while "never existed" tells it to check its bookkeeping.
        raise ProblemException(
            status=410,
            slug="gone",
            title="Gone",
            detail="This upload expired and its staged content has been discarded.",
        )
    if session.state in {"cancelled", "failed"}:
        raise _not_found("This upload was cancelled.")
    return session


# ------------------------------------------------------------------------- endpoints


@router.options(
    WORKSPACE_FILES_PATH,
    summary="Advertise the upload limits",
    status_code=200,
    response_class=Response,
    response_model=None,
)
async def upload_limits(
    workspace_id: UUID,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> Response:
    """The protocol's preflight: how large a body may be, and how long a session lives.

    A server that does not implement the protocol answers `501` here, so answering `200` with
    `Upload-Limit` is the signal that resumable uploads are available (ADR-0017).
    """
    await _writable_workspace(connection, workspace_id, credential)
    limits = limits_of(settings_of(request))
    return Response(status_code=200, headers={resumable.LIMIT_HEADER: limits.render()})


@router.post(
    WORKSPACE_FILES_PATH,
    summary="Upload a file, resumably",
    status_code=201,
    response_model=None,
    openapi_extra={
        "requestBody": {
            "description": "The file's bytes, or a leading part of them.",
            "required": False,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
    responses={
        201: {"description": "The file was stored, or the upload was created"},
        404: {"description": "No such workspace, or not yours"},
        409: {"description": "Something already exists at that path"},
        413: {"description": "The body exceeds Upload-Limit: max-append-size"},
        422: {"description": "The path or the declared hash was refused"},
    },
)
async def create_upload(
    workspace_id: UUID,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    path: Annotated[
        str,
        Query(
            min_length=1,
            max_length=names.MAX_PATH_BYTES,
            description="Workspace-relative destination, e.g. `Photos/2026/beach.jpg`.",
        ),
    ],
    content_hash: Annotated[
        str | None,
        Query(
            pattern="^[0-9a-fA-F]{64}$",
            description="Optional SHA-256 of the content; verified before anything is published.",
        ),
    ] = None,
) -> Response:
    settings = settings_of(request)
    workspace = await _writable_workspace(connection, workspace_id, credential)

    try:
        segments = names.split_path(path)
    except names.InvalidNameError as invalid:
        raise _invalid(invalid.reason, "/query/path") from invalid
    await _existing_target(connection, workspace, segments)

    dialect = resumable.dialect_for(request.headers.get(resumable.INTEROP_VERSION_HEADER))
    # Absent means "an ordinary upload": the client is not speaking the protocol, so this
    # request carries the whole file and no upload resource is handed out.
    declared_complete = resumable.parse_boolean(request.headers.get(resumable.COMPLETE_HEADER))
    complete = declared_complete is not False

    declared_length = resumable.parse_integer(request.headers.get(resumable.LENGTH_HEADER))
    limits = limits_of(settings)
    if limits.max_size is not None and (declared_length or 0) > limits.max_size:
        raise _too_large(limits.max_size)

    session = await uploads.create(
        connection,
        workspace_id=workspace.id,
        created_by=credential.user.id,
        target_path="/".join(segments),
        declared_length=declared_length,
        declared_hash=content_hash,
        media_type=request.headers.get("content-type"),
        interop_version=None if dialect is None else dialect.interop_version,
        expires_in=timedelta(days=settings.upload_expiry_days),
    )

    staging = uploads.staging_path(workspace.root_path, session.id)
    size = await _receive(request, staging, limit=limits.max_append_size)
    if limits.max_size is not None and size > limits.max_size:
        raise _too_large(limits.max_size)
    if size:
        session = await uploads.advance(connection, session=session, offset=size)

    if not complete:
        return JSONResponse(
            UploadState.of(session).model_dump(mode="json"),
            status_code=201,
            headers={
                "Location": f"{UPLOADS_PATH}/{session.id}",
                **_protocol_headers(session, limits, complete=False),
            },
        )

    if declared_length is not None and size != declared_length:
        raise _invalid(
            f"the body carried {size} bytes but Upload-Length declared {declared_length}",
            "/header/upload-length",
        )
    summary = await _finalize(
        connection, session=session, workspace=workspace, actor=Actor.user(credential.user.id)
    )
    return JSONResponse(
        summary.model_dump(mode="json"),
        status_code=201,
        headers={
            "Location": f"/files/{summary.id}",
            resumable.COMPLETE_HEADER: resumable.boolean(True),
            resumable.LIMIT_HEADER: limits.render(),
        },
    )


@router.head(
    f"{UPLOADS_PATH}/{{upload_id}}",
    summary="Report an upload's offset",
    status_code=204,
    response_class=Response,
    response_model=None,
    responses={
        204: {"description": "The current offset, in Upload-Offset"},
        404: {"description": "No such upload, or not yours"},
        410: {"description": "The upload expired"},
    },
)
async def upload_offset(
    upload_id: UUID,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> Response:
    """Where to resume from. The answer a client trusts after any interruption."""
    session = await _session_for(connection, upload_id, credential, lock=False)
    limits = limits_of(settings_of(request))
    return Response(
        status_code=204,
        headers={
            **_protocol_headers(session, limits, complete=session.is_complete),
            # Never cached: the whole point of this response is that it is current.
            "Cache-Control": "no-store",
        },
    )


@router.patch(
    f"{UPLOADS_PATH}/{{upload_id}}",
    summary="Append to an upload",
    response_model=None,
    openapi_extra={
        "requestBody": {
            "description": "The next bytes of the file.",
            "required": True,
            "content": {resumable.MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
    responses={
        200: {"description": "The upload completed and the file is registered"},
        204: {"description": "The bytes were stored; Upload-Offset carries the new offset"},
        400: {"description": "Upload-Offset is missing or unintelligible"},
        404: {"description": "No such upload, or not yours"},
        409: {"description": "The offset does not match; Upload-Offset carries the real one"},
        410: {"description": "The upload expired"},
        413: {"description": "The body exceeds Upload-Limit: max-append-size"},
        415: {"description": "An append must be application/partial-upload"},
    },
)
async def append_to_upload(
    upload_id: UUID,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> Response:
    settings = settings_of(request)
    limits = limits_of(settings)
    session = await _session_for(connection, upload_id, credential, lock=True)
    offset = resumable.parse_integer(request.headers.get(resumable.OFFSET_HEADER))

    if session.is_complete:
        # A retry whose predecessor's response was lost: replay the recorded outcome rather
        # than re-executing it (08 § idempotency).
        return await _replay(connection, session, limits)

    media_type = mediatypes.normalize(request.headers.get("content-type"))
    if media_type != resumable.MEDIA_TYPE:
        raise ProblemException(
            status=415,
            slug="unsupported-media-type",
            title="Unsupported media type",
            detail=f"An append must carry Content-Type: {resumable.MEDIA_TYPE}.",
        )
    if offset is None:
        raise ProblemException(
            status=400,
            slug="malformed-request",
            title="Malformed request",
            detail="An append must carry Upload-Offset.",
        )
    if offset != session.committed_offset:
        raise _mismatch(session, limits)

    workspace = await _writable_workspace(connection, session.workspace_id, credential)
    staging = uploads.staging_path(workspace.root_path, session.id)
    # Bytes past the committed offset were never acknowledged — a crash between the fsync and
    # the offset's commit — so they go before anything new is written.
    await asyncio.to_thread(uploads.discard_unacknowledged, staging, session.committed_offset)

    size = await _receive(request, staging, limit=limits.max_append_size)
    if limits.max_size is not None and size > limits.max_size:
        raise _too_large(limits.max_size)
    if session.declared_length is not None and size > session.declared_length:
        raise _invalid(
            f"the upload carries {size} bytes, past the declared {session.declared_length}",
            "/header/upload-offset",
        )
    session = await uploads.advance(connection, session=session, offset=size)

    complete = resumable.parse_boolean(request.headers.get(resumable.COMPLETE_HEADER)) is True
    if not complete:
        return Response(status_code=204, headers=_protocol_headers(session, limits, complete=False))

    if session.declared_length is not None and size != session.declared_length:
        raise _invalid(
            f"the upload ends at {size} bytes but declared {session.declared_length}",
            "/header/upload-complete",
        )
    summary = await _finalize(
        connection, session=session, workspace=workspace, actor=Actor.user(credential.user.id)
    )
    return JSONResponse(
        summary.model_dump(mode="json"),
        status_code=200,
        headers={
            "Location": f"/files/{summary.id}",
            resumable.COMPLETE_HEADER: resumable.boolean(True),
            resumable.LIMIT_HEADER: limits.render(),
        },
    )


@router.delete(
    f"{UPLOADS_PATH}/{{upload_id}}",
    summary="Cancel an upload",
    status_code=204,
    response_class=Response,
    response_model=None,
    responses={
        204: {"description": "The upload is cancelled and its staged content discarded"},
        404: {"description": "No such upload, or it already completed"},
    },
)
async def cancel_upload(
    upload_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> Response:
    session = await uploads.get(connection, upload_id)
    if session is None or session.created_by != credential.user.id:
        raise _not_found()
    if session.is_complete:
        raise _not_found("This upload already completed; the file it produced is not affected.")
    if session.is_open:
        await uploads.close(connection, session_id=session.id, state="cancelled")
        workspace = await workspaces.get(connection, session.workspace_id)
        if workspace is not None:
            # Best effort, because the janitor is the guarantee: a terminal session's staging
            # is collectable whether or not this unlink succeeded.
            staging = uploads.staging_path(workspace.root_path, session.id)
            await asyncio.to_thread(filestore.remove, staging)
    # Already cancelled or expired: `DELETE` is idempotent (08 § idempotency).
    return Response(status_code=204)


async def _replay(
    connection: DatabaseConnection, session: uploads.Session, limits: resumable.Limits
) -> Response:
    """The outcome a completed session recorded, for a client whose response was lost."""
    if session.file_id is None:  # pragma: no cover - completion always records its file
        raise _not_found()
    found = await files.get(connection, session.file_id)
    if found is None:
        raise _not_found("The file this upload produced no longer exists.")
    version = await files.current_version(connection, found.id)
    if version is None:  # pragma: no cover - a file is registered with its version
        raise _not_found()
    summary = FileSummary.of(found, version, await files.path_of(connection, found))
    return JSONResponse(
        summary.model_dump(mode="json"),
        status_code=200,
        headers={
            "Location": f"/files/{summary.id}",
            resumable.COMPLETE_HEADER: resumable.boolean(True),
            resumable.LIMIT_HEADER: limits.render(),
        },
    )


def _mismatch(session: uploads.Session, limits: resumable.Limits) -> ProblemException:
    """The protocol's `409`: refuse the append and say where the client actually is."""
    return ProblemException(
        status=409,
        slug="conflict",
        title="Conflict",
        detail=f"This upload is at offset {session.committed_offset}.",
        headers=_protocol_headers(session, limits, complete=False),
        type_uri=resumable.OFFSET_MISMATCH_TYPE,
    )
