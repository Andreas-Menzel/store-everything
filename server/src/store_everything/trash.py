"""The trash: a deletion recorded rather than performed.

[F-014](../../../features/F-014-deletion-and-trash.md) is a phase-4 feature, and this module is
the sliver of it phase 1 owes: **a file that vanished from the storage is never silently dropped
from the index** ([F-001/FR-6](../../../features/F-001-upload-and-import.md)). A re-scan that
finds a file gone flips it to `trashed` and writes an entry saying so — origin
`detected_on_disk`, which is what a client badges "removed outside the app"
([F-014/FR-10](../../../features/F-014-deletion-and-trash.md)) — with a purge deadline it will
outlive, because nothing purges anything in phase 1.

Three properties are deliberate:

- **The deadline is stored, not derived.** F-014's promise is that nothing leaves the trash
  before its deadline; a deadline computed at read time from a setting could be pulled forward
  by changing that setting, which is exactly the promise being broken. Phase 4 replaces where
  the number comes from, not where it is kept.
- **Restorability is not stored here.** It is `file_version.restorable`: whether the app still
  holds any of the file's bytes. Recording a snapshot of that answer would go stale the moment
  a version is snapshotted or collected.
- **The batch is the scan run.** A mass deletion has to come back in one call (F-014/FR-5), and
  for an external deletion the natural unit is the pass that noticed it — "put back everything
  that scan removed" is what an operator wants after a share half-mounted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, files, names
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import trash_entry

type Origin = Literal["in_app", "detected_on_disk"]

#: How long an entry is kept before a janitor may purge it: F-014/FR-6's instance default, as a
#: constant until the admin-editable instance setting arrives with the trash surface itself.
#: Deliberately **not** an environment variable — the retention window is a policy an admin sets
#: in the app, not a deployment detail (F-014/FR-6).
RETENTION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Entry:
    id: UUID
    file_id: UUID
    origin: Origin
    batch_id: UUID
    path: str
    trashed_at: datetime
    trashed_by: UUID | None
    purge_after: datetime


_COLUMNS = (
    trash_entry.c.id,
    trash_entry.c.file_id,
    trash_entry.c.origin,
    trash_entry.c.batch_id,
    trash_entry.c.path,
    trash_entry.c.trashed_at,
    trash_entry.c.trashed_by,
    trash_entry.c.purge_after,
)

type _Row = tuple[UUID, UUID, Origin, UUID, str, datetime, UUID | None, datetime]


def _as_entry(row: _Row) -> Entry:
    return Entry(*row)


async def entry_for(connection: AsyncConnection, file_id: UUID) -> Entry | None:
    row = (
        await connection.execute(select(*_COLUMNS).where(trash_entry.c.file_id == file_id))
    ).first()
    return None if row is None else _as_entry(tuple(row))


async def record(
    connection: AsyncConnection,
    *,
    found: files.File,
    path: str,
    origin: Origin,
    batch_id: UUID,
    actor: Actor,
    restorable: bool,
    trashed_by: UUID | None = None,
) -> Entry:
    """Trash a file: state, restorability, the entry, and the event — in one transaction.

    `restorable` is the answer to "do we hold these bytes anywhere?", asked by the caller
    because only it knows whether the blob store has them. For an external deletion the answer
    is almost always no, and saying so is the point: an entry promising a restore it cannot
    perform is worse than one that is honest about being a tombstone (F-014/FR-10).

    The deadline is computed by the database from the same clock that stamps `trashed_at`, so
    an entry can never claim a retention window it did not get.
    """
    await files.set_state(connection, file_id=found.id, state="trashed")
    await files.set_current_restorable(connection, file_id=found.id, restorable=restorable)

    values: dict[str, Any] = {
        "id": new_id(),
        "file_id": found.id,
        "origin": origin,
        "batch_id": batch_id,
        "path": path,
        "trashed_by": trashed_by,
        "purge_after": func.now() + text(f"interval '{RETENTION.days} days'"),
    }
    entry = _as_entry(
        tuple(
            (
                await connection.execute(insert(trash_entry).values(**values).returning(*_COLUMNS))
            ).one()
        )
    )

    await events.record(
        connection,
        action=events.FILE_TRASHED,
        resource_type=events.RESOURCE_FILE,
        resource_id=found.id,
        actor=actor,
        details={
            "workspace": str(found.workspace_id),
            "path": path,
            "origin": origin,
            "batch": str(batch_id),
            "purge_after": entry.purge_after.isoformat(),
            "restorable": restorable,
        },
    )
    return entry


async def reactivate(
    connection: AsyncConnection,
    *,
    found: files.File,
    path: str,
    actor: Actor,
    reason: str,
    seen_at: datetime,
) -> bool:
    """Take a file back out of the trash. Returns whether it was in there.

    Phase 1 reaches this one way: content with a trashed file's own hash reappeared at its own
    path, so the deletion the app recorded has been undone on the storage (F-014/FR-10). The
    row comes back whole — same UUID, same version history — which is the entire reason a
    deletion is recorded rather than performed.

    The entry is deleted rather than kept as history; the event log is what remembers.
    """
    if not await files.set_state(connection, file_id=found.id, state="live"):
        return False
    # The bytes are on the disk again, so the current version can be produced again.
    await files.set_current_restorable(connection, file_id=found.id, restorable=True)
    # Stamped here rather than left to the caller's bulk stamp, which covers live rows only:
    # this row was trashed when that ran.
    await files.stamp_seen(
        connection,
        folder_id=found.folder_id,
        name_keys=[names.comparison_key(found.name)],
        seen_at=seen_at,
    )
    await connection.execute(delete(trash_entry).where(trash_entry.c.file_id == found.id))

    await events.record(
        connection,
        action=events.FILE_RESTORED,
        resource_type=events.RESOURCE_FILE,
        resource_id=found.id,
        actor=actor,
        details={"workspace": str(found.workspace_id), "path": path, "reason": reason},
    )
    return True
