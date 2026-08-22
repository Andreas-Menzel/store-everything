"""Resumable upload sessions: the durable row behind an interrupted multi-gigabyte upload.

The wire format lives in `resumable`; this is the part that has to survive a power cut. One
rule generates almost all of it:

> **The committed offset is a promise.** Bytes are `fsync`'d before it advances, never after.

Everything follows from that. A crash between the `fsync` and the offset's commit leaves
*more* bytes on disk than the offset claims — so a resume truncates back to the offset and
re-receives them, which is why unacknowledged bytes are never a correctness problem. The
reverse order would leave an offset promising bytes that a power cut ate, and the client would
resume past a hole it has no way to detect.

Two more properties worth naming:

- **Appends to one session are serialised by a row lock.** Concurrent appends are a client
  bug, and the request already holds its connection for the body's duration, so the lock costs
  nothing extra and turns a corruption into a `409` with the true offset.
- **A completed session keeps its outcome.** A `PATCH` whose response was lost replays the
  recorded result instead of re-executing (08 § idempotency): the session row *is* the
  idempotency record, which is why it holds `file_id` rather than being deleted on success.

Staging lives in the workspace's own `.workspace/staging/`, named after the session, on the
destination's filesystem — so finalizing is an atomic rename (ADR-0018) and the janitor can
attribute a leftover to the session that wrote it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import filestore, names, workspacefs
from store_everything.ids import new_id
from store_everything.tables import upload_session

type State = Literal["open", "completed", "cancelled", "expired", "failed"]

#: F-001/FR-7's explicit parameter. `reject` is the default everywhere: overwriting a file is a
#: decision, and the API never makes it on a client's behalf.
type ConflictMode = Literal["reject", "new_version"]

#: States nothing can be appended to. `completed` is terminal *and* still useful: it answers
#: an offset probe and replays a lost finalize.
TERMINAL_STATES = frozenset({"completed", "cancelled", "expired", "failed"})


class UploadError(Exception):
    """Base for the refusals an upload can produce, so the API layer can map them once."""


class OffsetMismatchError(UploadError):
    """An append arrived at an offset the server does not hold.

    Carries the true offset, because the protocol's `409` must tell the client where to
    resume — a bare refusal would leave it guessing.
    """

    def __init__(self, expected: int) -> None:
        super().__init__(f"expected offset {expected}")
        self.expected = expected


class HashMismatchError(UploadError):
    """The assembled bytes do not match the hash the client declared.

    The upload fails and **nothing is published**: draft-12 removed integrity digests from
    the protocol, so this check is ours (ADR-0017) and it is the only thing standing between
    a corrupted transfer and a file the user will trust.
    """


@dataclass(frozen=True, slots=True)
class Session:
    id: UUID
    workspace_id: UUID
    created_by: UUID
    target_path: str
    declared_length: int | None
    declared_hash: str | None
    media_type: str | None
    interop_version: int | None
    if_exists: ConflictMode
    """What to do if a live file already holds the target path (F-001/FR-7). Decided at
    creation, because that is where a client can be refused before it spends an hour
    uploading — and because finalize may run in a much later request."""

    committed_offset: int
    state: State
    file_id: UUID | None
    expires_at: datetime
    created_at: datetime

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_complete(self) -> bool:
        return self.state == "completed"


@dataclass(frozen=True, slots=True)
class Assembled:
    """What the staged bytes turned out to be, once they were in place."""

    content_hash: str
    size_bytes: int
    modified_at: datetime


_COLUMNS = (
    upload_session.c.id,
    upload_session.c.workspace_id,
    upload_session.c.created_by,
    upload_session.c.target_path,
    upload_session.c.declared_length,
    upload_session.c.declared_hash,
    upload_session.c.media_type,
    upload_session.c.interop_version,
    upload_session.c.if_exists,
    upload_session.c.committed_offset,
    upload_session.c.state,
    upload_session.c.file_id,
    upload_session.c.expires_at,
    upload_session.c.created_at,
)

type _Row = tuple[
    UUID,
    UUID,
    UUID,
    str,
    int | None,
    str | None,
    str | None,
    int | None,
    ConflictMode,
    int,
    State,
    UUID | None,
    datetime,
    datetime,
]


def _as_session(row: _Row) -> Session:
    return Session(*row)


def _query() -> Select[_Row]:
    return select(*_COLUMNS)


# ------------------------------------------------------------------------ the filesystem


def staging_path(workspace_root: Path, session_id: UUID) -> Path:
    """Where a session accumulates its bytes: inside the workspace, named after the session."""
    return filestore.staging_path(workspacefs.staging_directory(workspace_root), session_id)


class StagingLostError(Exception):
    """The staged bytes an acknowledged offset promised are not on the storage any more."""

    def __init__(self, *, staged: int, committed: int) -> None:
        super().__init__(f"staging holds {staged} bytes where {committed} were acknowledged")
        self.staged = staged
        self.committed = committed


def align_staging(staging: Path, committed_offset: int) -> None:
    """Make staging match the offset the client was promised, or refuse. **Blocking**, idempotent.

    Two directions, and only one of them is recoverable.

    Bytes **past** the offset were written but never promised — a crash after the `fsync` and
    before the offset's commit — so discarding them is what makes the client's view and ours
    identical again.

    **Fewer** bytes than the offset is the opposite situation and cannot be repaired here: the
    staging file has been truncated or removed underneath the session. That is reachable —
    staging lives in the user-visible `.workspace/staging/` inside the source tree, where an SMB
    client can reach it — and appending anyway would store the next chunk at the position the
    *file* is at rather than the one the client was promised, assembling a file that is
    self-consistent and wrong. An acknowledged offset is never wrong ([F-001/FR-15]), so the
    append is refused and the session ends rather than converging on content nobody sent.
    """
    try:
        size = staging.stat().st_size
    except FileNotFoundError:
        size = 0
    if size < committed_offset:
        raise StagingLostError(staged=size, committed=committed_offset)
    if size > committed_offset:
        filestore.truncate_staging(staging, committed_offset)


def assemble(staging: Path, destination: Path, *, declared_hash: str | None) -> Assembled:
    """Verify the staged bytes and move them into place. **Blocking.**

    Hashed *before* the rename, so a mismatch fails while the file is still nothing but
    debris. After the rename the destination is stat'ed rather than trusted: `modified_at` is
    what a later stat-scan compares against (ADR-0019), so it has to be the filesystem's own
    answer, not ours.
    """
    content_hash = filestore.digest_of_file(staging)
    if declared_hash is not None and content_hash != declared_hash.lower():
        raise HashMismatchError("the assembled content does not match the declared hash")

    filestore.commit_staged(staging, destination)
    stat = destination.stat()
    return Assembled(
        content_hash=content_hash,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


# ------------------------------------------------------------------------- the session


async def create(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    created_by: UUID,
    target_path: str,
    declared_length: int | None,
    declared_hash: str | None,
    media_type: str | None,
    interop_version: int | None,
    if_exists: ConflictMode,
    expires_in: timedelta,
) -> Session:
    """Record an upload before a single byte is written."""
    row = (
        await connection.execute(
            insert(upload_session)
            .values(
                id=new_id(),
                workspace_id=workspace_id,
                created_by=created_by,
                target_path=target_path,
                declared_length=declared_length,
                declared_hash=None if declared_hash is None else declared_hash.lower(),
                media_type=media_type,
                interop_version=interop_version,
                if_exists=if_exists,
                committed_offset=0,
                state="open",
                expires_at=func.now() + _interval(expires_in),
            )
            .returning(*_COLUMNS)
        )
    ).one()
    return _as_session(tuple(row))


def _interval(value: timedelta) -> object:
    """A `timedelta` PostgreSQL will add to `now()`, in database time (12 § transitions)."""
    return func.make_interval(0, 0, 0, 0, 0, 0, value.total_seconds())


async def get(connection: AsyncConnection, session_id: UUID) -> Session | None:
    row = (await connection.execute(_query().where(upload_session.c.id == session_id))).first()
    return None if row is None else _as_session(tuple(row))


async def locked(connection: AsyncConnection, session_id: UUID) -> Session | None:
    """The session, with concurrent appends to it queued behind this transaction.

    `FOR UPDATE` rather than an optimistic check: two appends at the same offset would both
    truncate and both write, and the second could destroy bytes the first already promised.
    Serialising them makes the loser's outcome a `409` carrying the true offset instead.
    """
    row = (
        await connection.execute(
            _query().where(upload_session.c.id == session_id).with_for_update()
        )
    ).first()
    return None if row is None else _as_session(tuple(row))


#: Distinguishes this lock space from every other advisory lock in the app. PostgreSQL keeps
#: the two-integer and single-bigint forms in separate namespaces, so a key here can never
#: collide with a workspace rollup lock (`aggregates`) whatever the numbers are.
_TARGET_LOCK_CLASS = 1


def _target_lock_key(workspace_id: UUID, path: str) -> int:
    """A signed 32-bit key for one publishable path in one workspace.

    Hashed rather than derived from an id, because the row that would carry the id — the file,
    or even its parent folder — is what two racing finalizes are competing to create. A
    collision between two *different* paths only over-serialises two uploads for the length of
    a finalize, which is why 32 bits is enough.
    """
    key = "/".join(names.comparison_key(segment) for segment in names.split_path(path))
    digest = hashlib.blake2b(f"{workspace_id}/{key}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


async def lock_target(connection: AsyncConnection, *, workspace_id: UUID, path: str) -> None:
    """Hold one destination path still until this transaction ends.

    Publishing is a check-then-act sequence over two systems: look for what is already at the
    path, snapshot it, rename the new bytes over it, write the rows. Two finalizes racing on one
    free path both find it free, and the loser's rename destroys the winner's bytes *before* any
    row guard can fire — content that was never snapshotted and is in no version, which is
    exactly what [F-001/FR-20](../../../features/F-001-upload-and-import.md) forbids. The unique
    index catches the rows a moment too late to matter.

    Transaction-scoped, so the commit releases it. Taken while the session's own row lock is
    already held, always in that order, so two finalizes cannot deadlock each other.
    """
    await connection.execute(
        select(func.pg_advisory_xact_lock(_TARGET_LOCK_CLASS, _target_lock_key(workspace_id, path)))
    )


async def advance(connection: AsyncConnection, *, session: Session, offset: int) -> Session:
    """Commit a new offset, guarded on the one this append was based on.

    The guard is what makes the promise safe to make: if anything moved the offset while the
    bytes were being written, this affects zero rows and the caller learns the real one.
    """
    row = (
        await connection.execute(
            update(upload_session)
            .where(
                upload_session.c.id == session.id,
                upload_session.c.state == "open",
                upload_session.c.committed_offset == session.committed_offset,
            )
            .values(committed_offset=offset, updated_at=func.now())
            .returning(*_COLUMNS)
        )
    ).first()
    if row is None:
        current = await get(connection, session.id)
        raise OffsetMismatchError(0 if current is None else current.committed_offset)
    return _as_session(tuple(row))


async def complete(connection: AsyncConnection, *, session: Session, file_id: UUID) -> bool:
    """Record the outcome, so a retry replays it rather than re-executing."""
    result = await connection.execute(
        update(upload_session)
        .where(upload_session.c.id == session.id, upload_session.c.state == "open")
        .values(state="completed", file_id=file_id, updated_at=func.now())
    )
    return result.rowcount == 1


async def close(
    connection: AsyncConnection, *, session_id: UUID, state: Literal["cancelled", "failed"]
) -> bool:
    """End a session without publishing anything. Its staged bytes become collectable."""
    result = await connection.execute(
        update(upload_session)
        .where(upload_session.c.id == session_id, upload_session.c.state == "open")
        .values(state=state, updated_at=func.now())
    )
    return result.rowcount == 1


async def expire_due(connection: AsyncConnection) -> int:
    """Mark every open session past its deadline `expired`. Returns how many.

    Database time, never the app's clock (12 § transitions). Expiry is what turns an abandoned
    upload's staging into ordinary debris: until then the janitor must leave it alone, because
    a client has up to a week to come back for it.
    """
    result = await connection.execute(
        update(upload_session)
        .where(upload_session.c.state == "open", upload_session.c.expires_at < func.now())
        .values(state="expired", updated_at=func.now())
    )
    return result.rowcount


async def open_ids(connection: AsyncConnection, ids: list[UUID]) -> set[UUID]:
    """Which of these ids belong to a session that is still open.

    The janitor's question, asked in one round trip: staging named after an open session is
    live data, however old it looks.
    """
    if not ids:
        return set()
    found = (
        await connection.execute(
            select(upload_session.c.id).where(
                upload_session.c.id.in_(ids), upload_session.c.state == "open"
            )
        )
    ).scalars()
    return set(found)
