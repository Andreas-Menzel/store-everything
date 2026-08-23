"""The job endpoints: claim it, keep it, read it, finish it.

Everything an extractor does after registering is here, and all of it is extractor-initiated
(ADR-0020) — the core never calls into a container, which is what lets one run on a network with
no inbound route and no egress.

Three properties are worth stating, because each is a rule the implementation has to carry:

- **the fencing token is the caller's proof.** A claim returns `attempt`, and every write-back
  repeats it. A worker whose lease expired while it was busy is refused (`409`) instead of
  overwriting the run that replaced it (12 § leases & fencing);
- **a claim holds no connection while it waits.** `wait` polls with the connection released
  between attempts, so a hundred idle extractors do not sit on a hundred pooled connections;
- **inputs are per job.** The URL an extractor is handed serves the bytes of *that job's* file
  version and nothing else, which is the whole of its filesystem access
  ([ADR-0021](../../../../../decisions/ADR-0021-extractor-sandbox-enforcement.md)).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Path as PathParam
from fastapi import Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from store_everything import blobs, extraction, files, filestore, operations, workspaces
from store_everything.api.extractor_api import EXTRACTOR_API_PREFIX
from store_everything.api.extractor_api.security import CurrentExtractor
from store_everything.config import Settings
from store_everything.db import DatabaseConnection
from store_everything.extraction import ClaimedJob
from store_everything.problems import ProblemException
from store_everything.schemas import BaseSchema
from store_everything.security import settings_of

#: The longest a claim may wait for work before answering "nothing". Bounded so that a client
#: cannot hold a request open indefinitely, and so an idle extractor's polling is visible in the
#: access log at a predictable rate rather than never.
MAX_WAIT_SECONDS = 30

#: How often a waiting claim looks again. Not configurable: it trades latency against a cheap
#: indexed query, and the honest fix for wanting it lower is a doorbell (12 § lossy doorbells),
#: which extraction gets when live updates arrive.
_POLL_SECONDS = 1.0

_MAX_ERROR_LENGTH = 2000


class InputReference(BaseSchema):
    """Where to read one of a job's inputs. Range requests are supported."""

    index: int
    kind: Literal["original"]
    """`original` is the file's own bytes. Derived-asset inputs join this when chaining does."""

    url: str
    media_type: str
    size: int
    content_hash: str
    digest_algorithm: Literal["sha256"] = "sha256"


class JobVersion(BaseSchema):
    id: UUID
    content_hash: str
    digest_algorithm: Literal["sha256"] = "sha256"
    size: int
    media_type: str
    media_class: Literal["image", "video", "audio", "document", "archive", "other"]
    is_current: bool
    """False when a newer version of the file exists — the job may have been superseded."""


class ClaimedJobResponse(BaseSchema):
    id: UUID
    """The job id, which is also the id of the run this job's results are stamped with."""

    attempt: int
    """The fencing token. Repeat it on every heartbeat, result and error for this claim."""

    idempotency_key: str
    extractor_id: str
    generation: int
    params: dict[str, Any]
    lease_expires_at: datetime
    heartbeat_interval_seconds: int
    """Heartbeat at least this often, or the lease lapses and the job is claimed by another."""

    cancel_requested: bool
    """Already true when the job was superseded between being queued and being claimed."""

    file_version: JobVersion
    inputs: list[InputReference]

    @classmethod
    def of(cls, claimed: ClaimedJob, *, prefix: str, heartbeat_seconds: int) -> ClaimedJobResponse:
        version = claimed.version
        return cls(
            id=claimed.operation.id,
            attempt=claimed.operation.attempt,
            idempotency_key=str(claimed.operation.payload.get("idempotency_key") or ""),
            extractor_id=claimed.run.extractor_id,
            generation=claimed.run.generation,
            params=dict(claimed.operation.payload.get("params") or {}),
            lease_expires_at=claimed.lease_expires_at,
            heartbeat_interval_seconds=heartbeat_seconds,
            cancel_requested=claimed.operation.cancel_requested,
            file_version=JobVersion(
                id=version.id,
                content_hash=version.content_hash,
                size=version.size_bytes,
                media_type=version.media_type,
                media_class=version.media_class,  # pyright: ignore[reportArgumentType]
                is_current=version.is_current,
            ),
            inputs=[
                InputReference(
                    index=0,
                    kind="original",
                    url=f"{prefix}/jobs/{claimed.operation.id}/inputs/0",
                    media_type=version.media_type,
                    size=version.size_bytes,
                    content_hash=version.content_hash,
                )
            ],
        )


class ClaimRequest(BaseSchema):
    worker: str | None = Field(default=None, min_length=1, max_length=100)
    """Which replica is claiming, for diagnostics. The lease belongs to the extractor either
    way, so two replicas of one image are two workers and not two extractors."""


class Fenced(BaseSchema):
    attempt: int = Field(ge=0)
    """The `attempt` from the claim. A stale one is refused rather than applied."""


class HeartbeatResponse(BaseSchema):
    lease_expires_at: datetime
    cancel: bool
    """Stop and report an error, or simply stop: the job will be re-claimed or is superseded."""


class JobErrorRequest(Fenced):
    message: str = Field(min_length=1, max_length=_MAX_ERROR_LENGTH)
    retryable: bool = True
    """False for a file this extractor can never process — it dead-letters immediately."""


class ResultEnvelope(Fenced):
    """The result of one job.

    This core version accepts an envelope that carries **no outputs**: the lifecycle is complete,
    and the tables that hold segments, metadata, tags and derived assets arrive with the code
    that writes them. Unknown fields are tolerated rather than refused (05 § compatibility
    rules), so an extractor written against a later core still completes its jobs here — its
    outputs are simply not stored yet, which the run's status reports honestly.
    """


class JobOutcome(BaseSchema):
    id: UUID
    state: str
    finished_at: datetime | None


def _lease(settings: Settings) -> timedelta:
    return timedelta(seconds=settings.lease_seconds)


def _lease_lost() -> ProblemException:
    """One answer for "this claim is no longer yours", whichever way it stopped being.

    Which of them it was is not something the caller can act on differently: stop working. The
    job is either running under another attempt or already terminal.
    """
    return ProblemException(
        status=409,
        slug="lease-lost",
        title="Lease lost",
        detail=(
            "This claim is no longer current — the lease expired and the job was re-claimed, or "
            "it has already finished. Stop working on it and claim again."
        ),
    )


def _no_such_job() -> ProblemException:
    """Absent, someone else's extractor, or already purged with its file — one answer."""
    return ProblemException(status=404, slug="not-found", title="Not found")


async def claim_job(
    request: Request,
    credential: CurrentExtractor,
    payload: ClaimRequest | None = None,
    wait: Annotated[int, Query(ge=0, le=MAX_WAIT_SECONDS)] = 0,
) -> Response:
    """Claim the next job for this extractor, or answer `204` when there is none.

    Deliberately not a consumer of the request transaction: waiting has to release its
    connection between attempts, and a claim has to commit the instant it succeeds — the lease
    it takes is a promise to everybody else, not something to hold open until a response is
    serialised.
    """
    settings = settings_of(request)
    engine: AsyncEngine = request.app.state.engine
    extractor = credential.extractor
    worker = f"extractor:{extractor.id}"
    if payload is not None and payload.worker:
        worker = f"{worker}/{payload.worker}"

    # A disabled extractor is refused work rather than told there is none: the difference is
    # what an operator sees when they wonder why nothing is being processed.
    if not extractor.enabled:
        raise ProblemException(
            status=409,
            slug="extractor-disabled",
            title="Extractor disabled",
            detail="An administrator has disabled this extractor, so no work is routed to it.",
        )

    deadline = asyncio.get_running_loop().time() + wait
    while True:
        async with engine.connect() as connection:
            claimed = await extraction.claim(
                connection,
                extractor_id=extractor.id,
                extractor_version=extractor.version,
                model_version=extractor.model_version,
                worker=worker,
                lease=_lease(settings),
            )
            # Committed either way: a claim takes a lease, and the failed-claim branch may have
            # dead-lettered a job whose run went missing.
            await connection.commit()

        if claimed is not None:
            return Response(
                content=ClaimedJobResponse.of(
                    claimed,
                    prefix=request.scope.get("root_path", "") + EXTRACTOR_API_PREFIX,
                    heartbeat_seconds=settings.heartbeat_seconds,
                ).model_dump_json(),
                media_type="application/json",
            )

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return Response(status_code=204)
        await asyncio.sleep(min(_POLL_SECONDS, remaining))


async def _fenced_claim(
    connection: AsyncConnection, *, job_id: UUID, extractor_id: str, attempt: int
) -> operations.Operation:
    """The claim a fenced call may act on, or the problem explaining why it may not.

    Two refusals with two meanings: `404` for a job that is not this extractor's to touch at
    all, and `409` for one that is but whose claim has moved on.
    """
    owned = await extraction.owned_job(connection, job_id=job_id, extractor_id=extractor_id)
    if owned is None:
        raise _no_such_job()
    found, _ = owned
    if found.attempt != attempt or found.state != "running":
        raise _lease_lost()
    return found


async def heartbeat_job(
    job_id: UUID,
    payload: Fenced,
    request: Request,
    credential: CurrentExtractor,
    connection: DatabaseConnection,
) -> HeartbeatResponse:
    """Extend the lease, and learn whether to stop.

    Progress *is* the heartbeat and the heartbeat *is* the cancellation channel (05 § job
    lifecycle): an extractor that keeps reporting keeps its job, and one that is told to stop
    learns it here rather than from a signal it might miss.
    """
    settings = settings_of(request)
    extractor_id = credential.extractor.id
    owned = await extraction.owned_job(connection, job_id=job_id, extractor_id=extractor_id)
    if owned is None:
        raise _no_such_job()
    found, _ = owned
    if found.attempt != payload.attempt:
        raise _lease_lost()

    held = await extraction.lease_of(connection, job_id)
    beat = await operations.heartbeat(
        connection,
        claimed=found,
        worker=held.owner or f"extractor:{extractor_id}",
        lease=_lease(settings),
    )
    if not beat.lease_extended:
        raise _lease_lost()

    extended = await extraction.lease_of(connection, job_id)
    if extended.expires_at is None:  # pragma: no cover - just extended, in this transaction
        raise _lease_lost()
    return HeartbeatResponse(lease_expires_at=extended.expires_at, cancel=beat.cancel_requested)


async def submit_result(
    job_id: UUID,
    payload: ResultEnvelope,
    credential: CurrentExtractor,
    connection: DatabaseConnection,
) -> JobOutcome:
    """Finish a job. One envelope, one guarded transaction, applied once."""
    found = await _fenced_claim(
        connection,
        job_id=job_id,
        extractor_id=credential.extractor.id,
        attempt=payload.attempt,
    )
    if not await extraction.finish(connection, claimed=found):
        raise _lease_lost()
    run = await extraction.get_run(connection, job_id)
    if run is None:  # pragma: no cover - written in this transaction
        raise _no_such_job()
    return JobOutcome(id=run.id, state=run.state, finished_at=run.finished_at)


async def report_error(
    job_id: UUID,
    payload: JobErrorRequest,
    request: Request,
    credential: CurrentExtractor,
    connection: DatabaseConnection,
) -> JobOutcome:
    """Report a failed attempt. The queue decides whether it is retried or dead-lettered."""
    settings = settings_of(request)
    found = await _fenced_claim(
        connection,
        job_id=job_id,
        extractor_id=credential.extractor.id,
        attempt=payload.attempt,
    )
    await extraction.abandon(
        connection,
        claimed=found,
        error=payload.message,
        retryable=payload.retryable,
        base_seconds=settings.retry_base_seconds,
        max_seconds=settings.retry_max_seconds,
    )
    run = await extraction.get_run(connection, job_id)
    if run is None:  # pragma: no cover - written in this transaction
        raise _no_such_job()
    return JobOutcome(id=run.id, state=run.state, finished_at=run.finished_at)


async def read_input(
    job_id: UUID,
    index: Annotated[int, PathParam(ge=0)],
    request: Request,
    credential: CurrentExtractor,
    connection: DatabaseConnection,
) -> Response:
    """Stream one input of one job — the only bytes this extractor may read.

    Which bytes depends on the version, not on the path: while a version is current its content
    is the file in the workspace, and once it is superseded the app's own copy in `versions/` is
    what remains (F-007/FR-9). A version whose bytes are gone from both — overwritten on the
    storage before the app saw it — is `410`, because there is nothing to analyse and retrying
    will not change that.
    """
    settings = settings_of(request)
    run = await _job_input_run(connection, job_id=job_id, extractor_id=credential.extractor.id)
    if index != 0:
        raise _no_such_job()

    version = await files.version(connection, run.file_version_id)
    if version is None:
        raise _no_such_job()

    absolute = await _bytes_of(connection, version=version, settings=settings)
    if absolute is None:
        raise ProblemException(
            status=410,
            slug="gone",
            title="Gone",
            detail="The content of this file version is no longer stored.",
        )
    return FileResponse(
        absolute,
        media_type=version.media_type,
        headers={
            "ETag": f'"{version.content_hash}"',
            # An extractor reads bytes to analyse them, never to render them.
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
        content_disposition_type="attachment",
    )


async def _job_input_run(
    connection: DatabaseConnection, *, job_id: UUID, extractor_id: str
) -> extraction.Run:
    """The run whose inputs may be read.

    Unfenced on purpose: reading bytes changes nothing, and a worker whose lease lapsed mid-read
    is refused when it tries to *write* the result. Refusing the read as well would only turn a
    clean `409` into a truncated download.
    """
    owned = await extraction.owned_job(connection, job_id=job_id, extractor_id=extractor_id)
    if owned is None:
        raise _no_such_job()
    return owned[1]


async def _bytes_of(
    connection: DatabaseConnection, *, version: files.Version, settings: Settings
) -> Path | None:
    """Where this version's content is, or `None` if it is nowhere."""
    if version.is_current:
        found = await files.get(connection, version.file_id)
        workspace = None if found is None else await workspaces.get(connection, found.workspace_id)
        if found is not None and workspace is not None:
            relative = await files.path_of(connection, found)
            try:
                absolute = await asyncio.to_thread(
                    filestore.resolve_within, workspace.root_path, Path(relative)
                )
            except filestore.ContainmentError:
                return None
            if await asyncio.to_thread(absolute.is_file):
                return absolute
        return None

    store = blobs.BlobStore(settings.versions_root)
    candidate = store.path_for(version.content_hash)
    return candidate if await asyncio.to_thread(candidate.is_file) else None
