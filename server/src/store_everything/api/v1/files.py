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
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import Field

from store_everything import (
    aggregates,
    derived,
    extraction,
    files,
    filestore,
    folders,
    mediatypes,
    names,
    previews,
    results,
    tagging,
    tags,
    trash,
    workspaces,
)
from store_everything.api import API_V1_PREFIX
from store_everything.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursor,
    Page,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from store_everything.api.v1.tags import AppliedTag, TagApplyRequest, not_vocabulary, target_of
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema
from store_everything.security import CurrentCredential, settings_of

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

    tags: list[AppliedTag] = Field(default_factory=list)
    """Every tag on the file, with its provenance (F-003/FR-3). Included here rather than only
    behind `/tags` because a file's tags are part of what a file *is*, and a detail view that
    needed a second request to show them would show it late."""

    placeholder_hash: str | None = None
    """The same few dozen bytes a listing row carries
    ([F-028/FR-5](../../../../features/F-028-thumbnails-and-previews.md)), so a detail view can
    paint the space the image will occupy while it loads."""

    has_thumbnail: bool = False
    """Whether a thumbnail request will produce one. A client shows a type icon otherwise,
    rather than learning it from a failed image request (FR-3)."""

    @classmethod
    def of(
        cls,
        found: files.File,
        version: files.Version,
        path: str,
        extraction_status: extraction.Status,
        trash: TrashInfo | None = None,
        applied: list[tagging.Applied] | None = None,
        placeholder: str | None = None,
        has_thumbnail: bool = False,
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
            tags=[AppliedTag.of(one) for one in applied or []],
            placeholder_hash=placeholder,
            has_thumbnail=has_thumbnail,
        )


class RenditionInfo(BaseSchema):
    """One alternative form of the whole file, ready to download (ADR-0008)."""

    kind: str
    media_type: str
    size_bytes: int
    url: str


class SegmentInfo(BaseSchema):
    """One span of a file's content, and where it is."""

    id: UUID
    ordinal: int
    text: str
    anchor_kind: Literal["page", "time", "line", "sheet", "region", "whole"]
    anchor: dict[str, Any]
    """The position, shaped by its kind: a page and character offsets, a millisecond range, a
    line range, a sheet and rows, a normalised rectangle. Empty for `whole`."""

    confidence: float | None
    """How sure the extractor was — OCR reports this; a text layer does not (F-004/FR-2)."""

    language: str | None
    extractor: str | None
    generation: int


class MetadataInfo(BaseSchema):
    """One typed fact about a file, with the provenance every derived row carries."""

    key: str
    type: Literal[
        "string",
        "text",
        "integer",
        "float",
        "boolean",
        "datetime",
        "date",
        "duration",
        "geo",
        "json",
    ]
    value: Any
    provenance: Literal["auto", "manual"]
    confidence: float | None
    extractor: str | None
    """Which extractor produced it. Absent for a value a person entered."""

    generation: int


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
        await tagging.tags_of_file(connection, found.id),
        placeholder=(await previews.placeholders(connection, [version.id])).get(version.id),
        has_thumbnail=version.id in await previews.with_thumbnails(connection, [version.id]),
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


def _encode_ordinal(ordinal: int) -> str:
    """A segment cursor is just its position: the order is the reading order, and it is stable."""
    return encode_keyset_cursor(["segment", str(ordinal)])


def _ordinal_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    segment, ordinal = decode_keyset_cursor(cursor, parts=2)
    if segment != "segment" or not ordinal.isdigit():
        raise InvalidCursor()
    return int(ordinal)


@router.get(
    "/{file_id}/segments",
    summary="The searchable content of one file, with positions",
    response_model=Page[SegmentInfo],
    responses={404: {"description": "No such file, or not yours"}},
)
async def read_file_segments(
    file_id: UUID,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    limit: Annotated[int, Query(gt=0, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[SegmentInfo]:
    """Every segment of the current version, in reading order.

    This is [F-004](../../../../features/F-004-document-text-extraction.md)'s answer to *where*:
    the same rows a search hit will point into, readable directly so that "did the OCR work" has
    an answer that does not depend on search existing yet.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await files.current_version(connection, found.id)
    if version is None:  # pragma: no cover - see `summarize`
        raise RuntimeError(f"file {found.id} has no current version")

    after = _ordinal_cursor(cursor)
    # One row beyond the page: the cheapest honest way to know whether a next page exists.
    stored = await results.segments_of(connection, version.id, limit=limit + 1, after=after)
    page = stored[:limit]
    return Page(
        data=[
            SegmentInfo(
                id=span.id,
                ordinal=span.ordinal,
                text=span.text,
                anchor_kind=span.anchor_kind,  # pyright: ignore[reportArgumentType]
                anchor=span.anchor,
                confidence=span.confidence,
                language=span.language,
                extractor=span.extractor_id,
                generation=span.generation,
            )
            for span in page
        ],
        next_cursor=(_encode_ordinal(page[-1].ordinal) if len(stored) > limit and page else None),
    )


@router.get(
    "/{file_id}/metadata",
    summary="What is known about one file",
    response_model=list[MetadataInfo],
    responses={404: {"description": "No such file, or not yours"}},
)
async def read_file_metadata(
    file_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> list[MetadataInfo]:
    """Every typed fact about the current version — EXIF, page count, detected language, position.

    Unpaginated: a file has tens of these, not thousands, and the ones a client renders are all
    of them.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await files.current_version(connection, found.id)
    if version is None:  # pragma: no cover - see `summarize`
        raise RuntimeError(f"file {found.id} has no current version")

    return [
        MetadataInfo(
            key=entry.key,
            type=entry.value_type,  # pyright: ignore[reportArgumentType]
            value=entry.value,
            provenance=entry.provenance,  # pyright: ignore[reportArgumentType]
            confidence=entry.confidence,
            extractor=entry.extractor_id,
            generation=entry.generation,
        )
        for entry in await results.metadata_of(connection, version.id)
    ]


@router.get(
    "/{file_id}/tags",
    summary="The tags on one file",
    response_model=list[AppliedTag],
    responses={404: {"description": "No such file, or not yours"}},
)
async def read_file_tags(
    file_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> list[AppliedTag]:
    """Every tag the file carries, in name order, each with its provenance (F-003/FR-3).

    The same list `GET /files/{id}` embeds. It exists separately because tagging is the one
    thing a client changes often, and re-reading a file's whole summary after every edit would
    be a lot of response for one word.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    return [AppliedTag.of(one) for one in await tagging.tags_of_file(connection, found.id)]


@router.post(
    "/{file_id}/tags",
    summary="Tag a file",
    status_code=201,
    response_model=AppliedTag,
    responses={
        404: {"description": "No such file, or not yours"},
        409: {"description": "That tag is not part of the vocabulary"},
        422: {"description": "No tag goes by that name"},
    },
)
async def tag_file(
    file_id: UUID,
    request: TagApplyRequest,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> AppliedTag:
    """Apply a tag by hand (F-003/FR-2), stamped with the caller's user id.

    Tags belong to the file, not to the viewer: anyone who can write to it tags it, and everyone
    who can read it sees the same tags with the same attribution
    ([02 § file](../../../../specs/02-domain-model.md#file)). Phase 1's only permission is
    ownership, so *write* still means *yours*; grants change that in one place (`readable`).

    Idempotent — applying a tag the file already carries returns the row that is already there.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    target = await target_of(connection, request)
    try:
        applied = await tagging.apply_to_file(
            connection,
            file_id=found.id,
            tag_id=target.id,
            user_id=credential.user.id,
            actor=Actor.user(credential.user.id),
        )
    except tagging.NotVocabularyError as refused:
        raise not_vocabulary(refused) from refused
    return AppliedTag.of(applied)


@router.post(
    "/{file_id}/tags/{tag_id}/confirm",
    summary="Confirm a machine's tag",
    response_model=AppliedTag,
    responses={
        404: {"description": "No such file, or nothing claims that tag on it"},
        409: {"description": "That tag is not part of the vocabulary yet"},
    },
)
async def confirm_file_tag(
    file_id: UUID,
    tag_id: UUID,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> AppliedTag:
    """Agree with an `auto` tag (F-003/FR-4): from here it is user truth.

    The difference that matters is not cosmetic — a confirmed tag survives every reprocessing,
    because what carries it is a person's record rather than a generation's output. The model's
    stamp stays visible alongside, so "which detector found this, and how sure was it" remains
    answerable after the fact.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    try:
        applied = await tagging.confirm_on_file(
            connection,
            file_id=found.id,
            tag_id=tag_id,
            user_id=credential.user.id,
            actor=Actor.user(credential.user.id),
        )
    except (tagging.NothingToConfirmError, tags.UnknownTagError) as absent:
        raise not_found() from absent
    except tagging.NotVocabularyError as refused:
        raise not_vocabulary(refused) from refused
    return AppliedTag.of(applied)


@router.delete(
    "/{file_id}/tags/{tag_id}",
    summary="Remove a tag from a file",
    status_code=204,
    responses={404: {"description": "No such file, or it does not carry that tag"}},
)
async def untag_file(
    file_id: UUID,
    tag_id: UUID,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> None:
    """Take a tag off a file. `404` when the file does not carry it — there is nothing to undo.

    Removing a machine's tag is a **rejection** (F-003/FR-5): the claim goes and a record stays,
    so no later generation puts the word back. Removing one a person applied is just a removal —
    there is nothing to suppress.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    removed = await tagging.remove_from_file(
        connection,
        file_id=found.id,
        tag_id=tag_id,
        user_id=credential.user.id,
        actor=Actor.user(credential.user.id),
    )
    if not removed:
        raise not_found()


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


@router.get(
    "/{file_id}/thumbnail",
    summary="A file's thumbnail at one of the fixed sizes",
    response_class=FileResponse,
    response_model=None,
    responses={
        404: {"description": "No such file, or nothing to render for it"},
        410: {"description": "The file is in the trash"},
    },
)
async def read_file_thumbnail(
    file_id: UUID,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    size: Annotated[int | None, Query(ge=1, le=8192)] = None,
    v: UUID | None = None,
) -> Response:
    """A WebP thumbnail, at the nearest size **at or above** what was asked for (F-028/FR-1).

    Three things make this endpoint worth its own code rather than a generic asset route:

    - **`size` snaps into a fixed set.** Ask for 300, get 512. No free-form resizing, so the set
      of derived files stays bounded and every URL is cacheable.
    - **`v` pins a version, and only then is the answer immutable** (FR-4). A pinned URL can be
      cached for a year because that version's thumbnail can never change; the unpinned URL
      follows the current version and must be revalidated.
    - **No thumbnail is a typed answer, not a broken image** (FR-3). A client gets
      `problem+json` with its own slug and renders a type icon — never a placeholder graphic
      pretending to be the file.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    if not found.is_live:
        raise ProblemException(
            status=410,
            slug="gone",
            title="Gone",
            detail="This file is in the trash; its thumbnail is not served while it is there.",
        )

    version = await _pinned_version(connection, found=found, pinned=v)
    tier = previews.snap(size)
    stored = await previews.thumbnail(connection, file_version_id=version.id, size=tier)
    if stored is None:
        raise ProblemException(
            status=404,
            slug="no-thumbnail",
            title="No thumbnail",
            detail=(
                "Nothing has been rendered for this file. Either its type has no visual "
                "representation, or analysis has not finished yet — `GET /files/{id}/extraction` "
                "says which."
            ),
        )

    store = derived.DerivedStore(settings_of(request).derived_root)
    absolute = store.path_for(stored.source_hash, stored.name)
    if not await asyncio.to_thread(absolute.is_file):
        # The row promises bytes the derived store does not have. Regenerable by definition
        # (02 § invariants #5), so the honest answer is "not now" rather than a `500`.
        raise ProblemException(
            status=404,
            slug="no-thumbnail",
            title="No thumbnail",
            detail="This file's thumbnail is missing from the derived store; it will be rebuilt.",
        )

    etag = f'"{stored.source_hash}-{tier}"'
    headers = {
        "ETag": etag,
        # The whole reason for a fixed size set: a pinned URL identifies bytes that can never
        # change, so a client may keep them for a year without asking again (FR-4).
        "Cache-Control": (
            "private, max-age=31536000, immutable" if v is not None else "private, no-cache"
        ),
        # It is an image *we* produced, but it is still an image: the same policy as content,
        # because a WebP decoder is a decoder.
        "Content-Security-Policy": _CONTENT_SECURITY,
    }
    if etag in {tag.strip() for tag in (request.headers.get("if-none-match") or "").split(",")}:
        return Response(status_code=304, headers=headers)

    return FileResponse(
        absolute,
        media_type=stored.media_type,
        content_disposition_type="inline",
        headers=headers,
    )


class PreviewAsset(BaseSchema):
    """One thing a client can render or download, named by the descriptor."""

    kind: str
    """`thumbnail`, `image-preview`, `page` — an open vocabulary on purpose: a plugin's new kind
    appears here without a core change (F-028/FR-6)."""

    media_type: str
    url: str
    """Where to get it. Absolute-path, version-pinned, and therefore cacheable forever."""

    params: dict[str, Any]
    """What distinguishes it from its siblings: `{"size": 512}`, `{"page": 3}`."""

    size_bytes: int


class PreviewDescriptor(BaseSchema):
    """What exists, and what can be made, for one file (F-028/FR-6).

    A client renders from this rather than from the MIME type. That is the difference between a
    viewer that has to know about every format the instance might grow and one that shows what
    the server says is there — including kinds that did not exist when the client shipped.
    """

    version: UUID
    media_type: str
    media_class: Literal["image", "video", "audio", "document", "archive", "other"]
    placeholder_hash: str | None
    thumbnail_sizes: list[int]
    """The fixed set, so a client need not hard-code it."""

    assets: list[PreviewAsset]
    pages: int | None = None
    """How many pages a document has, when something has counted them. Every page is
    retrievable at `pages_url` whether or not it has been rendered yet (FR-7)."""

    pages_url: str | None = None
    """The pattern for one page, with `{page}` to substitute — named rather than assumed, which
    is the point of a descriptor."""

    renditions: list[str] = Field(default_factory=list)
    """Downloadable alternative forms of the whole file that exist for it (ADR-0008)."""


@router.get(
    "/{file_id}/preview",
    summary="What can be rendered for this file",
    response_model=PreviewDescriptor,
    responses={404: {"description": "No such file, or not yours"}},
)
async def read_file_preview(
    file_id: UUID,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    v: UUID | None = None,
) -> PreviewDescriptor:
    """The descriptor: every preview asset this file has, and the URLs to fetch them.

    Deliberately *not* a guess from the media type. A client that renders what this says will
    show a 3D model's turntable the day an extractor starts producing one, and will not try to
    paint a PDF page for a file that has none.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await _pinned_version(connection, found=found, pinned=v)
    assets = await previews.assets_of(connection, file_version_id=version.id)

    described = [
        PreviewAsset(
            kind=asset.kind,
            media_type=asset.media_type,
            url=_asset_url(file_id, asset, version.id),
            params=asset.params,
            size_bytes=asset.size_bytes,
        )
        for asset in assets
        if asset.rendition_kind is None
    ]
    pages = await previews.fact(connection, file_version_id=version.id, key="page_count")
    return PreviewDescriptor(
        version=version.id,
        media_type=version.media_type,
        media_class=version.media_class,  # pyright: ignore[reportArgumentType]
        placeholder_hash=(await previews.placeholders(connection, [version.id])).get(version.id),
        thumbnail_sizes=list(previews.THUMBNAIL_SIZES),
        assets=described,
        pages=int(pages) if pages is not None else None,
        pages_url=(
            f"{API_V1_PREFIX}/files/{file_id}/preview/pages/{{page}}?v={version.id}"
            if pages is not None
            else None
        ),
        renditions=sorted(
            {asset.rendition_kind for asset in assets if asset.rendition_kind is not None}
        ),
    )


def _asset_url(file_id: UUID, asset: previews.Asset, version: UUID) -> str:
    """Where a client asks for one asset — the endpoint that serves that kind.

    Every URL carries the version, because that is what makes it cacheable forever (FR-4): the
    descriptor is the one place a client learns them, so it hands out the pinned form.
    """
    base = f"{API_V1_PREFIX}/files/{file_id}"
    if asset.kind == previews.THUMBNAIL_KIND:
        return f"{base}/thumbnail?size={asset.params.get('size')}&v={version}"
    if asset.kind == previews.PAGE_KIND:
        return f"{base}/preview/pages/{asset.params.get('page')}?v={version}"
    return f"{base}/preview/assets/{asset.id}?v={version}"


@router.get(
    "/{file_id}/preview/pages/{page}",
    summary="One page of a document, rendered",
    response_class=FileResponse,
    response_model=None,
    responses={
        202: {"description": "Being rendered; retry after the interval in `Retry-After`"},
        404: {"description": "No such file or page, or nothing can render pages here"},
    },
)
async def read_file_page(
    file_id: UUID,
    page: Annotated[int, PathParam(ge=1, le=10_000)],
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    v: UUID | None = None,
) -> Response:
    """A page image, rendered on first request and stored (F-028/FR-7).

    Page 1 arrives with the thumbnail; anything else is rendered when somebody asks, which is the
    difference between storing three images for a three-page document and storing three hundred
    for a book nobody opened past the cover. The first request for a page answers `202` with a
    `Retry-After` and queues the work at interactive priority; the second serves the stored bytes.

    A `202` rather than a blocked request: holding an HTTP connection open across a queue, a
    lease and a container's render is how a proxy timeout becomes a mystery.
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await _pinned_version(connection, found=found, pinned=v)

    stored = await previews.page(connection, file_version_id=version.id, number=page)
    if stored is None:
        pages = await previews.fact(connection, file_version_id=version.id, key="page_count")
        if pages is None or page > int(pages):
            # Either nothing has counted this file's pages — it is not a paginated document — or
            # it has fewer than that. Both are "no such page".
            raise not_found()
        try:
            await extraction.request(
                connection,
                file_version_id=version.id,
                generation=1,
                claim_type="derived_asset",
                kind=previews.PAGE_KIND,
                variant=f"page:{page}",
                params={"page": page},
            )
        except extraction.NoProviderError as absent:
            # Nothing installed renders pages. An honest 404 rather than a 202 that will never
            # come true.
            raise ProblemException(
                status=404,
                slug="no-page-renderer",
                title="No page renderer",
                detail=(
                    "This instance has no extractor that produces page images, so pages of this "
                    "document cannot be rendered."
                ),
            ) from absent
        return Response(
            status_code=202,
            headers={"Retry-After": "1", "Cache-Control": "no-store"},
        )

    return _serve_asset(request, version=version, asset=stored, pinned=v is not None)


@router.get(
    "/{file_id}/preview/assets/{asset_id}",
    summary="One preview asset by id",
    response_class=FileResponse,
    response_model=None,
    responses={404: {"description": "No such file or asset"}},
)
async def read_file_preview_asset(
    file_id: UUID,
    asset_id: UUID,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    v: UUID | None = None,
) -> Response:
    """Whatever the descriptor pointed at — an image preview today, a plugin's kind tomorrow."""
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await _pinned_version(connection, found=found, pinned=v)
    for asset in await previews.assets_of(connection, file_version_id=version.id):
        if asset.id == asset_id:
            return _serve_asset(request, version=version, asset=asset, pinned=v is not None)
    raise not_found()


@router.get(
    "/{file_id}/renditions",
    summary="Alternative downloadable forms of this file",
    response_model=list[RenditionInfo],
    responses={404: {"description": "No such file, or not yours"}},
)
async def read_file_renditions(
    file_id: UUID,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    v: UUID | None = None,
) -> list[RenditionInfo]:
    """What else this file can be downloaded as (F-028/FR-8, ADR-0008).

    A rendition is the whole file in another form — a searchable PDF, a subtitled video — and it
    never replaces the original: `/content` serves the bytes that were uploaded, always (FR-9).
    """
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await _pinned_version(connection, found=found, pinned=v)
    return [
        RenditionInfo(
            kind=asset.rendition_kind or "",
            media_type=asset.media_type,
            size_bytes=asset.size_bytes,
            url=f"{API_V1_PREFIX}/files/{file_id}/renditions/{asset.rendition_kind}?v={version.id}",
        )
        for asset in await previews.assets_of(connection, file_version_id=version.id)
        if asset.rendition_kind is not None
    ]


@router.get(
    "/{file_id}/renditions/{kind}",
    summary="Download one rendition",
    response_class=FileResponse,
    response_model=None,
    responses={404: {"description": "No such file, or no such rendition for it"}},
)
async def read_file_rendition(
    file_id: UUID,
    kind: str,
    request: Request,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    v: UUID | None = None,
) -> Response:
    """The rendition's bytes, as a download. The original is never served from here."""
    found, _ = await readable(connection, file_id=file_id, credential=credential)
    version = await _pinned_version(connection, found=found, pinned=v)
    for asset in await previews.assets_of(connection, file_version_id=version.id):
        if asset.rendition_kind == kind:
            return _serve_asset(
                request,
                version=version,
                asset=asset,
                pinned=v is not None,
                filename=f"{found.name}.{asset.name.rpartition('.')[2]}",
            )
    raise not_found()


def _serve_asset(
    request: Request,
    *,
    version: files.Version,
    asset: previews.Asset,
    pinned: bool,
    filename: str | None = None,
) -> Response:
    """Serve one derived asset from the derived store, with the caching a pin allows.

    The same rules as the thumbnail endpoint, in one place: an `ETag` from the source hash and the
    asset's own name, `immutable` only when the caller pinned a version, and the content-security
    policy every byte we serve carries whether we produced it or not.
    """
    store = derived.DerivedStore(settings_of(request).derived_root)
    absolute = store.path_for(asset.source_hash, asset.name)
    if not absolute.is_file():
        raise ProblemException(
            status=404,
            slug="not-found",
            title="Not found",
            detail="This asset is missing from the derived store; it will be rebuilt.",
        )

    etag = f'"{asset.source_hash}-{asset.name}"'
    headers = {
        "ETag": etag,
        "Cache-Control": (
            "private, max-age=31536000, immutable" if pinned else "private, no-cache"
        ),
        "Content-Security-Policy": _CONTENT_SECURITY,
    }
    if etag in {tag.strip() for tag in (request.headers.get("if-none-match") or "").split(",")}:
        return Response(status_code=304, headers=headers)
    return FileResponse(
        absolute,
        media_type=asset.media_type,
        filename=filename,
        content_disposition_type="attachment" if filename else "inline",
        headers=headers,
    )


async def _pinned_version(
    connection: DatabaseConnection, *, found: files.File, pinned: UUID | None
) -> files.Version:
    """The version a request is about: the one it pinned, or the current one.

    A pinned version has to belong to *this* file — otherwise `?v=` would be a way to read one
    file's assets through another file's permission check.
    """
    if pinned is None:
        version = await files.current_version(connection, found.id)
        if version is None:  # pragma: no cover - see `summarize`
            raise RuntimeError(f"file {found.id} has no current version")
        return version
    version = await files.version(connection, pinned)
    if version is None or version.file_id != found.id:
        raise not_found()
    return version


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
