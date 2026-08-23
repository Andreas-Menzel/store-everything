"""Applying tags: what a file or a folder carries, and who said so.

The vocabulary lives in [tags.py](tags.py); this is the other half — the rows that say *this
file is an invoice*. Two rules shape all of it:

- **A tag belongs to the file, not to the viewer** (02 § file). Alice grants Bob write, Bob
  tags, Alice sees the tag stamped with Bob's user id. There is one shared truth per file and
  no private layer, which is a decision ([F-003](../../../features/F-003-tagging.md) § out of
  scope), not an omission.
- **User curation is its own table.** `file_tag` holds what a person said — `manual`,
  `confirmed`, `rejected` — while a machine's claim is a derived row keyed by the run that made
  it (`file_auto_tag`). Reprocessing replaces a generation's claims and has no reason to name
  the curation table, which is how
  [02 § invariants](../../../specs/02-domain-model.md#invariants) #4 stays true by construction
  rather than by vigilance.
- **Where the two meet, the person decides.** A file's tag list is the curation rows plus the
  current version's claims for tags nobody has ruled on; a rejection hides the tag and stops
  later generations re-adding it
  ([ADR-0004](../../../decisions/ADR-0004-tag-provenance-and-reprocessing.md)).

Folder tags are the simple case and deliberately stay that way
([F-015/FR-9](../../../features/F-015-folders.md)): manual, self-only, no provenance state
machine, because extractors never run on folders. A folder tag describes the folder and does not
match the files inside it — inheritance is deferred with its precedence rules unanswered (Q23).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, tags
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import (
    extraction_run,
    file_auto_tag,
    file_tag,
    file_version,
    folder_tag,
    tag,
    tag_name,
)
from store_everything.tags import Tag

#: What a user's row can say when the tag is on the file. `rejected` is a record that it is
#: *not*, so it is not an application and never appears in a tag list.
APPLIED_STATES = ("manual", "confirmed")


class NotVocabularyError(Exception):
    """The tag exists but may not be applied — a quarantined suggestion, or a rejected word.

    Carries the status so the answer can say which, because "approve it first" and "that word
    was turned down" are different things for the person reading it (F-003/FR-12).
    """

    def __init__(self, tag_id: UUID, status: str) -> None:
        super().__init__(status)
        self.tag_id = tag_id
        self.status = status


@dataclass(frozen=True, slots=True)
class Source:
    """Which run produced a machine's claim, and how sure it was (F-003/FR-3).

    Every field here is provenance the API has to expose: an `auto` tag a user cannot trace is
    an assertion with no author, and a `0.62` detection should not look like a person's word.
    """

    extractor: str
    extractor_version: str | None
    model_version: str | None
    generation: int
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Applied:
    """One tag as it sits on a file or folder, with everything a client needs to show it."""

    tag: Tag
    provenance: str
    user_id: UUID | None
    created_at: datetime
    updated_at: datetime
    source: Source | None = None
    """The machine claim behind it, when there is one — present for an `auto` tag and kept for a
    `confirmed` one, because "which model said this, and how sure" stays interesting after a
    person agrees with it."""


@dataclass(frozen=True, slots=True)
class Claim:
    """One label a machine produced, before it is anything: a word and a confidence."""

    name: str
    confidence: float | None = None


# --------------------------------------------------------------------------------- files


def _newest_generation() -> Any:
    """The latest generation *this claim's extractor* reached on *this claim's version*.

    Reprocessing replaces a generation's output (F-003/FR-6), but the rows are kept rather than
    deleted — ADR-0004 retains the previous generation for rollback until something prunes it.
    So "replaced" is a property of the read: a claim counts only if it belongs to the newest
    generation its own extractor produced here.

    Per extractor, deliberately. Two extractors are reprocessed independently, and a
    version-wide maximum would hide the claims of one that has not been re-run yet.
    """
    claim = file_auto_tag.alias("sibling_claim")
    run = extraction_run.alias("sibling_run")
    return (
        select(func.max(claim.c.generation))
        .select_from(claim)
        .join(run, run.c.id == claim.c.run_id)
        .where(
            claim.c.file_version_id == file_auto_tag.c.file_version_id,
            run.c.extractor_id == extraction_run.c.extractor_id,
        )
        .scalar_subquery()
    )


async def tags_of_file(connection: AsyncConnection, file_id: UUID) -> list[Applied]:
    """Every tag on one file, in name order — curation and machine claims resolved into one list.

    Three rules, and they are the whole of ADR-0004's state machine as a reader sees it:

    1. a person's word decides. `manual` and `confirmed` are the tag being there; `rejected` is
       the tag *not* being there, and a rejected tag is absent rather than listed as absent;
    2. a claim shows up as `auto` only where nobody has ruled on it;
    3. a `confirmed` tag keeps the claim's stamp, because that is what makes it different from a
       tag somebody typed.

    Only the **current** version's claims count: an older version's describe bytes this file no
    longer has.
    """
    curated = {
        row.tag_id: row
        for row in (
            await connection.execute(
                select(
                    file_tag.c.tag_id,
                    file_tag.c.provenance,
                    file_tag.c.user_id,
                    file_tag.c.created_at,
                    file_tag.c.updated_at,
                ).where(file_tag.c.file_id == file_id)
            )
        ).all()
    }
    claims = {
        row.tag_id: row
        for row in (
            await connection.execute(
                select(
                    file_auto_tag.c.tag_id,
                    file_auto_tag.c.confidence,
                    file_auto_tag.c.generation,
                    file_auto_tag.c.created_at,
                    extraction_run.c.extractor_id,
                    extraction_run.c.extractor_version,
                    extraction_run.c.model_version,
                )
                .select_from(file_auto_tag)
                .join(file_version, file_version.c.id == file_auto_tag.c.file_version_id)
                .join(extraction_run, extraction_run.c.id == file_auto_tag.c.run_id)
                .where(
                    file_version.c.file_id == file_id,
                    file_version.c.is_current,
                    file_auto_tag.c.generation == _newest_generation(),
                )
                # Two extractors can claim one tag; the surest claim is the one to show, and
                # the newest generation breaks a tie. Ascending, because the dictionary below
                # keeps the last row it sees for a key.
                .order_by(
                    file_auto_tag.c.tag_id,
                    file_auto_tag.c.confidence.asc().nullsfirst(),
                    file_auto_tag.c.generation.asc(),
                )
            )
        ).all()
    }

    applied: list[Applied] = []
    for tag_id, found in (await tags.by_ids(connection, [*{*curated, *claims}])).items():
        curation = curated.get(tag_id)
        claim = claims.get(tag_id)
        if curation is not None and curation.provenance == "rejected":
            continue
        source = (
            None
            if claim is None
            else Source(
                extractor=claim.extractor_id,
                extractor_version=claim.extractor_version,
                model_version=claim.model_version,
                generation=claim.generation,
                confidence=claim.confidence,
            )
        )
        if curation is not None:
            applied.append(
                Applied(
                    tag=found,
                    provenance=curation.provenance,
                    user_id=curation.user_id,
                    created_at=curation.created_at,
                    updated_at=curation.updated_at,
                    source=source,
                )
            )
        elif claim is not None:
            applied.append(
                Applied(
                    tag=found,
                    provenance="auto",
                    user_id=None,
                    created_at=claim.created_at,
                    updated_at=claim.created_at,
                    source=source,
                )
            )
    applied.sort(key=lambda one: one.tag.name_key)
    return applied


async def apply_to_file(
    connection: AsyncConnection, *, file_id: UUID, tag_id: UUID, user_id: UUID, actor: Actor
) -> Applied:
    """Put a tag on a file by hand (F-003/FR-2). Idempotent, and it overrides a rejection.

    Re-applying a tag the file already carries changes nothing and records nothing — a POST
    that says what is already true is not an edit, and rewriting the attribution would take
    Bob's tag away from Bob. Applying one the caller previously *rejected* is the opposite: an
    explicit change of mind, so the row flips to `manual` with the new author.
    """
    found = await tags.get(connection, tag_id)
    if found is None:
        raise tags.UnknownTagError(tag_id)
    if not found.is_vocabulary:
        raise NotVocabularyError(tag_id, found.status)

    existing = await _file_row(connection, file_id=file_id, tag_id=tag_id)
    if existing is not None and existing.provenance in APPLIED_STATES:
        return _as_applied(found, existing)

    if existing is not None:
        await connection.execute(
            update(file_tag)
            .where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
            .values(provenance="manual", user_id=user_id, updated_at=func.now())
        )
    else:
        await connection.execute(
            insert(file_tag).values(
                file_id=file_id, tag_id=tag_id, provenance="manual", user_id=user_id
            )
        )
    await events.record(
        connection,
        action=events.FILE_TAGGED,
        resource_type=events.RESOURCE_FILE,
        resource_id=file_id,
        actor=actor,
        details={
            "tag": found.name,
            "tag_id": str(tag_id),
            "provenance": "manual",
            # What it said before, so the audit trail reads as a transition rather than a
            # sequence of unrelated facts (F-003/FR-9).
            "was": existing.provenance if existing is not None else None,
        },
    )
    written = await _file_row(connection, file_id=file_id, tag_id=tag_id)
    if written is None:  # pragma: no cover - written in this transaction
        raise RuntimeError(f"tag {tag_id} vanished from file {file_id}")
    return _as_applied(found, written)


async def confirm_on_file(
    connection: AsyncConnection, *, file_id: UUID, tag_id: UUID, user_id: UUID, actor: Actor
) -> Applied:
    """Agree with a machine (F-003/FR-4): the claim becomes user truth and stops being a guess.

    From here the tag survives every reprocessing, because what carries it is a curation row and
    reprocessing only ever replaces claims. The claim itself stays alongside — a confirmed tag
    that could no longer say which model found it would lose the thing that makes it different
    from one somebody typed.
    """
    found = await tags.get(connection, tag_id)
    if found is None:
        raise tags.UnknownTagError(tag_id)
    if not found.is_vocabulary:
        # Confirming a quarantined suggestion would make it user truth while the word is not
        # yet vocabulary. The admin's decision comes first (F-003/FR-12).
        raise NotVocabularyError(tag_id, found.status)
    if not await _claimed(connection, file_id=file_id, tag_id=tag_id):
        raise NothingToConfirmError(tag_id)

    existing = await _file_row(connection, file_id=file_id, tag_id=tag_id)
    if existing is None:
        await connection.execute(
            insert(file_tag).values(
                file_id=file_id, tag_id=tag_id, provenance="confirmed", user_id=user_id
            )
        )
    elif existing.provenance != "confirmed":
        await connection.execute(
            update(file_tag)
            .where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
            .values(provenance="confirmed", user_id=user_id, updated_at=func.now())
        )
    if existing is None or existing.provenance != "confirmed":
        await events.record(
            connection,
            action=events.FILE_TAG_CONFIRMED,
            resource_type=events.RESOURCE_FILE,
            resource_id=file_id,
            actor=actor,
            details={
                "tag": found.name,
                "tag_id": str(tag_id),
                "was": existing.provenance if existing is not None else "auto",
            },
        )
    return await _one_applied(connection, file_id=file_id, tag_id=tag_id)


async def remove_from_file(
    connection: AsyncConnection, *, file_id: UUID, tag_id: UUID, user_id: UUID, actor: Actor
) -> bool:
    """Take a tag off a file. `False` when the file was not carrying it.

    Two different acts wear one verb, and which one it is depends on whether a machine is
    claiming the tag:

    - **removing a machine's claim records a rejection** (F-003/FR-5). The claims go, and the
      `rejected` row stays as the negative record that keeps *any* future generation from
      putting the tag back — the "fox → cat comes back" failure ADR-0004 exists to prevent.
      Un-confirming counts as this too: the user is withdrawing agreement with a machine.
    - **removing a tag only a person applied deletes the row.** There is nothing to suppress:
      no model claimed it, so no model will re-add it, and leaving a rejection behind would
      block a future detection the user never objected to.
    """
    existing = await _file_row(connection, file_id=file_id, tag_id=tag_id)
    claimed = await _claimed(connection, file_id=file_id, tag_id=tag_id)
    if (existing is None or existing.provenance not in APPLIED_STATES) and not claimed:
        return False

    found = await tags.get(connection, tag_id)
    was = existing.provenance if existing is not None else "auto"
    machine_backed = claimed or was == "confirmed"

    if machine_backed:
        if existing is None:
            await connection.execute(
                insert(file_tag).values(
                    file_id=file_id, tag_id=tag_id, provenance="rejected", user_id=user_id
                )
            )
        else:
            await connection.execute(
                update(file_tag)
                .where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
                .values(provenance="rejected", user_id=user_id, updated_at=func.now())
            )
        await _discard_claims(connection, file_id=file_id, tag_id=tag_id)
    else:
        await connection.execute(
            delete(file_tag).where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
        )

    await events.record(
        connection,
        action=events.FILE_TAG_REJECTED if machine_backed else events.FILE_UNTAGGED,
        resource_type=events.RESOURCE_FILE,
        resource_id=file_id,
        actor=actor,
        details={
            "tag": found.name if found is not None else None,
            "tag_id": str(tag_id),
            "was": was,
        },
    )
    return True


# ------------------------------------------------------------------------- machine claims


class NothingToConfirmError(Exception):
    """Confirm was asked for a tag no machine is claiming — there is no guess to agree with."""

    def __init__(self, tag_id: UUID) -> None:
        super().__init__(str(tag_id))
        self.tag_id = tag_id


async def apply_claims(
    connection: AsyncConnection,
    *,
    file_id: UUID,
    file_version_id: UUID,
    run_id: UUID,
    generation: int,
    claims: tuple[Claim, ...],
) -> int:
    """Record what one run claims about a version's tags — the auto-tag write path (FR-3, FR-11).

    An extractor sends **labels**, not tag ids, because a model's vocabulary is not ours. So each
    label is mapped before it is stored, in the order ADR-0006 requires:

    1. **into an existing tag** if any spelling of one matches — canonical name or synonym, which
       is how `automobile` lands on `car` and how model drift is absorbed by the alias table
       instead of the taxonomy (embedding similarity joins this step in phase 3);
    2. **into a `suggested` tag** otherwise, quarantined until an admin approves it, and stamped
       with the run that proposed it;
    3. **nowhere at all** if the word was already rejected — either as a word (a turned-down
       suggestion, which is exactly what that record is for) or on this file (a user's rejection,
       which no future generation may override, FR-5).
    """
    written = 0
    for claim in claims:
        target = await _tag_for(connection, claim.name, run_id=run_id)
        if target is None:
            continue
        if await _rejected(connection, file_id=file_id, tag_id=target.id):
            continue
        result = await connection.execute(
            pg_insert(file_auto_tag)
            .values(
                id=new_id(),
                file_version_id=file_version_id,
                run_id=run_id,
                tag_id=target.id,
                generation=generation,
                confidence=claim.confidence,
            )
            # One claim per tag per run: an extractor that lists `cat` twice means it once.
            .on_conflict_do_nothing(index_elements=["file_version_id", "tag_id", "run_id"])
        )
        written += result.rowcount
    return written


async def drop_rejected_claims(connection: AsyncConnection, *, run_id: UUID) -> int:
    """Remove this run's claims for tags the file they landed on has rejected. Returns how many.

    Needed by the one path that writes claims without mapping them: **reuse**, which copies
    another version's analysis wholesale (F-009/FR-8). The bytes are identical, so the claims
    are right about the content — but a rejection belongs to the *file*, and a user who said no
    here should not get the tag back because somebody else uploaded the same document.
    """
    rejection = file_tag.alias("rejection")
    rejected = (
        select(literal(1))
        .select_from(rejection)
        .join(file_version, file_version.c.file_id == rejection.c.file_id)
        .where(
            file_version.c.id == file_auto_tag.c.file_version_id,
            rejection.c.tag_id == file_auto_tag.c.tag_id,
            rejection.c.provenance == "rejected",
        )
        .exists()
    )
    dropped = await connection.execute(
        delete(file_auto_tag).where(file_auto_tag.c.run_id == run_id, rejected)
    )
    return dropped.rowcount


async def discard_claims(connection: AsyncConnection, *, run_id: UUID) -> None:
    """Forget one run's claims — the tag half of a generation swap (F-003/FR-6).

    Called by the result path before it applies a new envelope, so a re-run replaces its own
    claims rather than doubling them, and a new generation replaces the old one's. Nothing here
    touches curation, which is what makes `manual` and `confirmed` survive verbatim.
    """
    await connection.execute(delete(file_auto_tag).where(file_auto_tag.c.run_id == run_id))


async def _tag_for(connection: AsyncConnection, label: str, *, run_id: UUID) -> Tag | None:
    """The tag a label means, creating a quarantined suggestion if nothing fits."""
    resolved = await tags.resolve(connection, label)
    if resolved is not None:
        return None if resolved.tag.status == "rejected" else resolved.tag
    return await tags.create(
        connection,
        name=label,
        actor=Actor.extractor(),
        status="suggested",
        suggested_by_run_id=run_id,
    )


async def _claimed(connection: AsyncConnection, *, file_id: UUID, tag_id: UUID) -> bool:
    """Whether any machine claims this tag on this file's current version."""
    rows = await connection.execute(
        select(file_auto_tag.c.id)
        .join(file_version, file_version.c.id == file_auto_tag.c.file_version_id)
        .where(
            file_version.c.file_id == file_id,
            file_version.c.is_current,
            file_auto_tag.c.tag_id == tag_id,
        )
        .limit(1)
    )
    return rows.first() is not None


async def _discard_claims(connection: AsyncConnection, *, file_id: UUID, tag_id: UUID) -> None:
    """Drop every version's claims of one tag on one file — what a rejection takes with it."""
    versions = select(file_version.c.id).where(file_version.c.file_id == file_id)
    await connection.execute(
        delete(file_auto_tag).where(
            file_auto_tag.c.tag_id == tag_id, file_auto_tag.c.file_version_id.in_(versions)
        )
    )


async def _rejected(connection: AsyncConnection, *, file_id: UUID, tag_id: UUID) -> bool:
    rows = await connection.execute(
        select(file_tag.c.provenance).where(
            file_tag.c.file_id == file_id,
            file_tag.c.tag_id == tag_id,
            file_tag.c.provenance == "rejected",
        )
    )
    return rows.first() is not None


async def _one_applied(connection: AsyncConnection, *, file_id: UUID, tag_id: UUID) -> Applied:
    """One tag as the file carries it — the same resolution the list does, for one row."""
    for applied in await tags_of_file(connection, file_id):
        if applied.tag.id == tag_id:
            return applied
    raise RuntimeError(f"tag {tag_id} is not on file {file_id}")  # pragma: no cover


# ------------------------------------------------------------------------------- folders


async def tags_of_folder(connection: AsyncConnection, folder_id: UUID) -> list[Applied]:
    rows = await connection.execute(
        select(
            tag.c.id,
            tag_name.c.name,
            tag_name.c.name_key,
            tag.c.status,
            tag.c.created_at.label("tag_created_at"),
            tag.c.created_by,
            folder_tag.c.user_id,
            folder_tag.c.created_at,
        )
        .select_from(folder_tag)
        .join(tag, tag.c.id == folder_tag.c.tag_id)
        .join(tag_name, (tag_name.c.tag_id == tag.c.id) & ~tag_name.c.is_alias)
        .where(folder_tag.c.folder_id == folder_id)
        .order_by(tag_name.c.name_key)
    )
    return [
        Applied(
            tag=Tag(
                id=row.id,
                name=row.name,
                name_key=row.name_key,
                status=row.status,
                created_at=row.tag_created_at,
                created_by=row.created_by,
            ),
            provenance="manual",
            user_id=row.user_id,
            created_at=row.created_at,
            updated_at=row.created_at,
        )
        for row in rows.all()
    ]


async def apply_to_folder(
    connection: AsyncConnection, *, folder_id: UUID, tag_id: UUID, user_id: UUID, actor: Actor
) -> Applied:
    """Tag a directory (F-015/FR-9). Same vocabulary as files, none of the machinery."""
    found = await tags.get(connection, tag_id)
    if found is None:
        raise tags.UnknownTagError(tag_id)
    if not found.is_vocabulary:
        raise NotVocabularyError(tag_id, found.status)

    existing = await _folder_row(connection, folder_id=folder_id, tag_id=tag_id)
    if existing is not None:
        return _as_folder_applied(found, existing)

    await connection.execute(
        insert(folder_tag).values(folder_id=folder_id, tag_id=tag_id, user_id=user_id)
    )
    await events.record(
        connection,
        action=events.FOLDER_TAGGED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=folder_id,
        actor=actor,
        details={"tag": found.name, "tag_id": str(tag_id)},
    )
    written = await _folder_row(connection, folder_id=folder_id, tag_id=tag_id)
    if written is None:  # pragma: no cover - written in this transaction
        raise RuntimeError(f"tag {tag_id} vanished from folder {folder_id}")
    return _as_folder_applied(found, written)


async def remove_from_folder(
    connection: AsyncConnection, *, folder_id: UUID, tag_id: UUID, actor: Actor
) -> bool:
    existing = await _folder_row(connection, folder_id=folder_id, tag_id=tag_id)
    if existing is None:
        return False
    found = await tags.get(connection, tag_id)
    await connection.execute(
        delete(folder_tag).where(folder_tag.c.folder_id == folder_id, folder_tag.c.tag_id == tag_id)
    )
    await events.record(
        connection,
        action=events.FOLDER_UNTAGGED,
        resource_type=events.RESOURCE_FOLDER,
        resource_id=folder_id,
        actor=actor,
        details={"tag": found.name if found is not None else None, "tag_id": str(tag_id)},
    )
    return True


# --------------------------------------------------------------------------------- rows


@dataclass(frozen=True, slots=True)
class _Row:
    provenance: str
    user_id: UUID
    created_at: datetime
    updated_at: datetime


async def _file_row(connection: AsyncConnection, *, file_id: UUID, tag_id: UUID) -> _Row | None:
    rows = await connection.execute(
        select(
            file_tag.c.provenance,
            file_tag.c.user_id,
            file_tag.c.created_at,
            file_tag.c.updated_at,
        ).where(file_tag.c.file_id == file_id, file_tag.c.tag_id == tag_id)
    )
    row = rows.first()
    if row is None:
        return None
    return _Row(
        provenance=row.provenance,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _folder_row(connection: AsyncConnection, *, folder_id: UUID, tag_id: UUID) -> _Row | None:
    rows = await connection.execute(
        select(folder_tag.c.user_id, folder_tag.c.created_at).where(
            folder_tag.c.folder_id == folder_id, folder_tag.c.tag_id == tag_id
        )
    )
    row = rows.first()
    if row is None:
        return None
    return _Row(
        provenance="manual",
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.created_at,
    )


def _as_applied(found: Tag, row: _Row) -> Applied:
    return Applied(
        tag=found,
        provenance=row.provenance,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _as_folder_applied(found: Tag, row: _Row) -> Applied:
    return Applied(
        tag=found,
        provenance="manual",
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.created_at,
    )


__all__ = [
    "APPLIED_STATES",
    "Applied",
    "Claim",
    "NotVocabularyError",
    "NothingToConfirmError",
    "Source",
    "apply_claims",
    "apply_to_file",
    "apply_to_folder",
    "confirm_on_file",
    "discard_claims",
    "drop_rejected_claims",
    "remove_from_file",
    "remove_from_folder",
    "tags_of_file",
    "tags_of_folder",
]
