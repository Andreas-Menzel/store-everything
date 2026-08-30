"""The tag vocabulary: one global DAG, the names that resolve to it, and the closure.

Three ideas, all from [ADR-0006](../../../decisions/ADR-0006-hierarchical-tags-dag.md):

1. **A tag is a node, not a word.** Its canonical name and every synonym are rows in
   `tag_name`, keyed by the folded spelling — so resolving a label and completing a prefix are
   the *same lookup*, and no two meanings can claim one word.
2. **Breadth is a query, not a write.** A file carries the most specific tags it earns;
   `tag:nature` finds `tree` by expanding downward over `tag_closure` at query time. Nothing
   here writes an ancestor onto a file, which is why restructuring is instant.
3. **Multi-parent, never cyclic.** `tree` may sit under `plant` and under `landscaping`. An
   edge that would close a loop is refused *before* it exists, because the closure rebuild
   below walks the graph and a loop would make that walk endless.

The closure is rebuilt whole whenever an edge changes. Incremental maintenance of a DAG's
closure is possible and subtle — removing one edge can leave a pair connected by another path,
so a decrement is wrong more often than it is right — while a rebuild is one recursive query
over a table that holds a few thousand rows on a large instance. The simple thing is also the
correct thing here; if a taxonomy ever grows past that, the incremental version is a known
refinement and this docstring is where to start.

Admin-only, all of it (F-003/FR-10). The vocabulary is shared truth: members apply it, and
the surfaces that let them are in [tagging.py](tagging.py).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, names
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import (
    MAX_TAG_NAME_LENGTH,
    file,
    file_auto_tag,
    file_tag,
    file_version,
    folder,
    folder_tag,
    tag,
    tag_closure,
    tag_edge,
    tag_name,
    workspace,
)

#: How many prefix matches are considered before ranking by usage (F-003/FR-8). A completion
#: shows ten or twenty; looking at two hundred candidates to rank them is generous, and the cap
#: is what keeps a one-letter prefix from counting usage over the whole vocabulary.
COMPLETION_CANDIDATES = 200


class InvalidTagNameError(ValueError):
    """A name breaks the policy. Carries the rule, not the value (08 § errors)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NameTakenError(Exception):
    """The name already resolves to a tag — this one or another.

    One error for both because the caller's answer is the same: pick another word, or edit the
    tag that owns it. The offending tag is carried so the message can name it.
    """

    def __init__(self, name: str, tag_id: UUID, *, is_alias: bool) -> None:
        super().__init__(name)
        self.name = name
        self.tag_id = tag_id
        self.is_alias = is_alias


class NameRaceError(Exception):
    """Another request claimed the name between the check and the insert.

    Separate from `NameTakenError` because the answer differs: after a failed insert the
    transaction is spent, so this one cannot name the tag that won — and "retry" is what the
    caller should do anyway.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class CycleError(Exception):
    """The edge would make a tag its own ancestor."""

    def __init__(self, parent_id: UUID, child_id: UUID) -> None:
        super().__init__(f"{parent_id} is already below {child_id}")
        self.parent_id = parent_id
        self.child_id = child_id


class UnknownTagError(Exception):
    """A referenced tag id does not exist."""

    def __init__(self, tag_id: UUID) -> None:
        super().__init__(str(tag_id))
        self.tag_id = tag_id


class TagInUseError(Exception):
    """A hard delete was asked for a tag something still carries.

    ADR-0006 reserves hard deletion for typo-grade mistakes with no history; anything with
    applications is rejected instead, and `rejected` is the soft removal that keeps the name.
    """

    def __init__(self, tag_id: UUID, *, files: int, folders: int) -> None:
        super().__init__(str(tag_id))
        self.tag_id = tag_id
        self.files = files
        self.folders = folders


@dataclass(frozen=True, slots=True)
class Tag:
    id: UUID
    name: str
    name_key: str
    status: str
    created_at: datetime
    created_by: UUID | None
    #: The run that proposed the word, for a `suggested` tag — the review queue's provenance.
    suggested_by_run_id: UUID | None = None
    reviewed_at: datetime | None = None
    reviewed_by: UUID | None = None

    @property
    def is_vocabulary(self) -> bool:
        """Whether it may be applied, completed and searched — `active` and nothing else."""
        return self.status == "active"

    @property
    def is_suggestion(self) -> bool:
        """Whether it is a machine's proposal awaiting a decision (F-003/FR-12)."""
        return self.status == "suggested"


@dataclass(frozen=True, slots=True)
class Resolved:
    """A name lookup's answer: the tag, and which of its spellings matched."""

    tag: Tag
    matched: str
    is_alias: bool


@dataclass(frozen=True, slots=True)
class Usage:
    """How much of *the caller's own* library carries a tag.

    Caller-scoped deliberately: an instance-wide count would tell every member how many files
    exist tagged `divorce`, and instance admin is not data access
    ([07](../../../specs/07-identity-permissions-sharing.md)). It also happens to rank
    completions the way a person wants — their own vocabulary first.
    """

    files: int = 0
    folders: int = 0


@dataclass(frozen=True, slots=True)
class Completion:
    tag: Tag
    matched: str
    is_alias: bool
    usage: Usage


# ------------------------------------------------------------------------------------- names


def normalize(name: str) -> str:
    """The display form: NFC, no leading or trailing space, no runs of internal whitespace.

    Collapsing whitespace is F-003/FR-1's normalization and not cosmetic — `tax  return` and
    `tax return` are the same label to a person, and a vocabulary that holds both is one where
    autocomplete offers the same word twice.
    """
    return " ".join(unicodedata.normalize("NFC", name).split())


def key_of(name: str) -> str:
    """The identity of a name: the same Unicode recipe filenames use (`names.comparison_key`).

    Reused rather than reinvented, because the question is identical — when are two spellings
    one name — and answering it twice differently is how `Rechnung` and `rechnung` end up as
    two tags.
    """
    return names.comparison_key(normalize(name))


def validate_name(name: str) -> str:
    """Normalize and refuse what may not be a tag name. Returns the display form."""
    normalized = normalize(name)
    if not normalized:
        raise InvalidTagNameError("a tag name must not be empty")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise InvalidTagNameError("a tag name must not contain control characters")
    if len(normalized) > MAX_TAG_NAME_LENGTH:
        raise InvalidTagNameError(f"a tag name must be at most {MAX_TAG_NAME_LENGTH} characters")
    return normalized


# ------------------------------------------------------------------------------------ reading


def _tag_query() -> Select[
    tuple[UUID, str, str, str, datetime, UUID | None, UUID | None, datetime | None, UUID | None]
]:
    """A tag with its canonical name — the only shape callers ever want."""
    return select(
        tag.c.id,
        tag_name.c.name,
        tag_name.c.name_key,
        tag.c.status,
        tag.c.created_at,
        tag.c.created_by,
        tag.c.suggested_by_run_id,
        tag.c.reviewed_at,
        tag.c.reviewed_by,
    ).join(tag_name, (tag_name.c.tag_id == tag.c.id) & ~tag_name.c.is_alias)


def _as_tag(row: tuple[Any, ...]) -> Tag:
    return Tag(
        id=row[0],
        name=row[1],
        name_key=row[2],
        status=row[3],
        created_at=row[4],
        created_by=row[5],
        suggested_by_run_id=row[6],
        reviewed_at=row[7],
        reviewed_by=row[8],
    )


async def get(connection: AsyncConnection, tag_id: UUID) -> Tag | None:
    rows = await connection.execute(_tag_query().where(tag.c.id == tag_id))
    row = rows.first()
    return None if row is None else _as_tag(tuple(row))


async def by_ids(connection: AsyncConnection, tag_ids: list[UUID]) -> dict[UUID, Tag]:
    if not tag_ids:
        return {}
    rows = await connection.execute(_tag_query().where(tag.c.id.in_(tag_ids)))
    found = [_as_tag(tuple(row)) for row in rows.all()]
    return {one.id: one for one in found}


async def resolve(connection: AsyncConnection, name: str) -> Resolved | None:
    """Which tag a word means, canonical spelling or synonym. `None` if it means nothing yet.

    Two queries rather than one: the name registry says *which* tag, and `get` says what the tag
    is under its own name. Assembling the tag from this query's columns instead would work today
    and break the day a column is added to `tag` — which is exactly what happened once.
    """
    key = key_of(name)
    if not key:
        return None
    rows = await connection.execute(
        select(tag_name.c.tag_id, tag_name.c.name, tag_name.c.is_alias).where(
            tag_name.c.name_key == key
        )
    )
    row = rows.first()
    if row is None:
        return None
    found = await get(connection, row.tag_id)
    if found is None:  # pragma: no cover - a tag always has a canonical name
        raise RuntimeError(f"tag {row.tag_id} has no canonical name")
    return Resolved(tag=found, matched=row.name, is_alias=row.is_alias)


async def aliases_of(connection: AsyncConnection, tag_id: UUID) -> list[str]:
    rows = await connection.execute(
        select(tag_name.c.name)
        .where(tag_name.c.tag_id == tag_id, tag_name.c.is_alias)
        .order_by(tag_name.c.name_key)
    )
    return [row.name for row in rows.all()]


async def parents_of(connection: AsyncConnection, tag_ids: list[UUID]) -> dict[UUID, list[UUID]]:
    """Direct parents per tag — enough for a client to rebuild the DAG from one listing."""
    if not tag_ids:
        return {}
    rows = await connection.execute(
        select(tag_edge.c.child_id, tag_edge.c.parent_id)
        .where(tag_edge.c.child_id.in_(tag_ids))
        .order_by(tag_edge.c.child_id, tag_edge.c.parent_id)
    )
    parents: dict[UUID, list[UUID]] = {tag_id: [] for tag_id in tag_ids}
    for row in rows.all():
        parents[row.child_id].append(row.parent_id)
    return parents


async def children_of(connection: AsyncConnection, tag_id: UUID) -> list[Tag]:
    rows = await connection.execute(
        _tag_query()
        .join(tag_edge, tag_edge.c.child_id == tag.c.id)
        .where(tag_edge.c.parent_id == tag_id)
        .order_by(tag_name.c.name_key)
    )
    return [_as_tag(tuple(row)) for row in rows.all()]


async def ancestors_of(connection: AsyncConnection, tag_id: UUID) -> list[Tag]:
    """Everything above a tag, nearest first — the breadcrumb, derived and never stored."""
    rows = await connection.execute(
        _tag_query()
        .join(tag_closure, tag_closure.c.ancestor_id == tag.c.id)
        .where(tag_closure.c.descendant_id == tag_id, tag_closure.c.depth > 0)
        .order_by(tag_closure.c.depth, tag_name.c.name_key)
    )
    return [_as_tag(tuple(row)) for row in rows.all()]


def _rejected_on(file_id: Any, tag_id: Any) -> Any:
    """Whether a person has rejected this tag on this file — the suppression record (FR-5).

    Written as a correlated `EXISTS` because it belongs *inside* the queries that must respect
    it: a rejection is not a filter callers can be trusted to remember.
    """
    rejection = file_tag.alias("rejection")
    return (
        select(literal(1))
        .where(
            rejection.c.file_id == file_id,
            rejection.c.tag_id == tag_id,
            rejection.c.provenance == "rejected",
        )
        .exists()
    )


async def usage_of(
    connection: AsyncConnection, tag_ids: list[UUID], *, owner_id: UUID
) -> dict[UUID, Usage]:
    """How many of this owner's live files and folders carry each tag.

    Trashed files are excluded here rather than filtered by the caller
    ([02 § invariants](../../../specs/02-domain-model.md#invariants) #7: trashed items appear in
    no default surface, counts included).
    """
    if not tag_ids:
        return {}
    # A file counts once whether a person tagged it, a machine claimed it, or both — so the two
    # sources are unioned into (tag, file) pairs and counted, rather than added up.
    curated = (
        select(file_tag.c.tag_id, file_tag.c.file_id)
        .select_from(file_tag)
        .join(file, file.c.id == file_tag.c.file_id)
        .join(workspace, workspace.c.id == file.c.workspace_id)
        .where(
            file_tag.c.tag_id.in_(tag_ids),
            # A rejection is a record that the tag does *not* belong, so it is not usage.
            file_tag.c.provenance != "rejected",
            file.c.state == "live",
            workspace.c.owner_id == owner_id,
        )
    )
    claimed = (
        select(file_auto_tag.c.tag_id, file_version.c.file_id)
        .select_from(file_auto_tag)
        .join(file_version, file_version.c.id == file_auto_tag.c.file_version_id)
        .join(file, file.c.id == file_version.c.file_id)
        .join(workspace, workspace.c.id == file.c.workspace_id)
        .where(
            file_auto_tag.c.tag_id.in_(tag_ids),
            # The current version's claims only: an older version's are history, not the file.
            file_version.c.is_current,
            file.c.state == "live",
            workspace.c.owner_id == owner_id,
            ~_rejected_on(file_version.c.file_id, file_auto_tag.c.tag_id),
        )
    )
    pairs = curated.union(claimed).subquery("carried")
    files = await connection.execute(
        select(pairs.c.tag_id, func.count().label("total")).group_by(pairs.c.tag_id)
    )
    folders = await connection.execute(
        select(folder_tag.c.tag_id, func.count().label("total"))
        .select_from(folder_tag)
        .join(folder, folder.c.id == folder_tag.c.folder_id)
        .join(workspace, workspace.c.id == folder.c.workspace_id)
        .where(folder_tag.c.tag_id.in_(tag_ids), workspace.c.owner_id == owner_id)
        .group_by(folder_tag.c.tag_id)
    )
    counted = {tag_id: Usage() for tag_id in tag_ids}
    for row in files.all():
        counted[row.tag_id] = Usage(files=row.total)
    for row in folders.all():
        counted[row.tag_id] = Usage(files=counted[row.tag_id].files, folders=row.total)
    return counted


async def listing(
    connection: AsyncConnection,
    *,
    statuses: tuple[str, ...],
    limit: int,
    after: str | None = None,
) -> list[Tag]:
    """The taxonomy in name order — a browse, cursor-paginated on the name key.

    Canonical names only, and no prefix filter: browsing the vocabulary is a question about
    tags, while narrowing by what somebody typed is `complete`'s job — synonyms included, ranked
    by usage. Two functions rather than one with a mode, because they answer different questions.
    """
    query = (
        _tag_query().where(tag.c.status.in_(statuses)).order_by(tag_name.c.name_key).limit(limit)
    )
    if after is not None:
        query = query.where(tag_name.c.name_key > after)
    rows = await connection.execute(query)
    return [_as_tag(tuple(row)) for row in rows.all()]


async def complete(
    connection: AsyncConnection, *, prefix: str, limit: int, owner_id: UUID
) -> list[Completion]:
    """Prefix completion over the vocabulary, most-used first (F-003/FR-8).

    Aliases match too, which is the point of having them: typing `automobile` offers `car`,
    and the answer says which spelling matched so the UI can show why. Only `active` tags are
    offered — a suggestion is not vocabulary until an admin says so (F-003/FR-12).
    """
    key = key_of(prefix)
    if not key:
        return []
    rows = await connection.execute(
        # Which spellings match, and whose they are. The tags themselves are fetched below, by
        # id: this query is about names.
        select(tag.c.id, tag_name.c.name, tag_name.c.is_alias)
        .join(tag_name, tag_name.c.tag_id == tag.c.id)
        .where(
            tag.c.status == "active",
            tag_name.c.name_key.startswith(key, autoescape=True),
        )
        .order_by(tag_name.c.name_key)
        .limit(COMPLETION_CANDIDATES)
    )

    # One entry per tag: two of its spellings can match one prefix, and offering `car` twice
    # because `cars` is an alias would be a bug a user sees. The canonical name wins; between
    # two aliases the shorter one is the closer match.
    best: dict[UUID, tuple[str, bool]] = {}
    for row in rows.all():
        current = best.get(row.id)
        if current is None or _closer((row.name, row.is_alias), current):
            best[row.id] = (row.name, row.is_alias)

    found = await by_ids(connection, list(best))
    usage = await usage_of(connection, list(best), owner_id=owner_id)
    completions = [
        Completion(
            tag=found[tag_id],
            matched=matched,
            is_alias=is_alias,
            usage=usage.get(tag_id, Usage()),
        )
        for tag_id, (matched, is_alias) in best.items()
        if tag_id in found
    ]
    completions.sort(
        key=lambda one: (
            -(one.usage.files + one.usage.folders),
            len(one.matched),
            one.tag.name_key,
        )
    )
    return completions[:limit]


def _closer(candidate: tuple[str, bool], current: tuple[str, bool]) -> bool:
    """Whether one matching spelling beats another: canonical first, then shorter."""
    candidate_name, candidate_is_alias = candidate
    current_name, current_is_alias = current
    if candidate_is_alias != current_is_alias:
        return not candidate_is_alias
    return len(candidate_name) < len(current_name)


# ------------------------------------------------------------------------------------ writing


async def create(
    connection: AsyncConnection,
    *,
    name: str,
    actor: Actor,
    status: str = "active",
    created_by: UUID | None = None,
    suggested_by_run_id: UUID | None = None,
) -> Tag:
    """Add a word to the vocabulary.

    `status` is a parameter rather than always `active` because the same function creates the
    auto-tagger's `suggested` tags (F-003/FR-11) — one place that decides what a new tag looks
    like, whoever asked for it. A suggestion also records the run behind it, which is what a
    review queue shows next to the word.
    """
    display = validate_name(name)
    key = key_of(display)
    await _refuse_taken(connection, key, display)

    tag_id = new_id()
    await connection.execute(
        insert(tag).values(
            id=tag_id,
            status=status,
            created_by=created_by,
            suggested_by_run_id=suggested_by_run_id,
        )
    )
    await _claim_name(connection, key=key, name=display, tag_id=tag_id, is_alias=False)
    # Its own depth-0 row, so a subtree query includes the tag itself without a special case.
    # A full rebuild would be honest too and pointlessly expensive: a new tag has no edges.
    await connection.execute(
        insert(tag_closure).values(ancestor_id=tag_id, descendant_id=tag_id, depth=0)
    )
    await events.record(
        connection,
        action=events.TAG_CREATED,
        resource_type=events.RESOURCE_TAG,
        resource_id=tag_id,
        actor=actor,
        details={"name": display, "status": status},
    )
    created = await get(connection, tag_id)
    if created is None:  # pragma: no cover - inserted in this transaction
        raise RuntimeError(f"tag {tag_id} vanished between insert and select")
    return created


async def rename(connection: AsyncConnection, *, tag_id: UUID, name: str, actor: Actor) -> Tag:
    """Give a tag a different canonical name.

    The old name is **not** kept as an alias. A rename says the word was wrong; keeping it
    would mean the wrong word still resolves, and an admin who wants that adds it back in one
    call. Promoting one of the tag's own aliases is a rename too, and that case is handled:
    the alias row becomes the canonical one.
    """
    current = await get(connection, tag_id)
    if current is None:
        raise UnknownTagError(tag_id)
    display = validate_name(name)
    key = key_of(display)

    if key != current.name_key:
        held = await _holder(connection, key)
        if held is not None and held[0] != tag_id:
            raise NameTakenError(display, held[0], is_alias=held[1])
        # Either free, or one of this tag's own aliases being promoted. Both end the same way.
        await connection.execute(delete(tag_name).where(tag_name.c.name_key == key))
        await connection.execute(delete(tag_name).where(tag_name.c.name_key == current.name_key))
        await _claim_name(connection, key=key, name=display, tag_id=tag_id, is_alias=False)
    else:
        # Same word, different spelling of it — `invoice` to `Invoice`.
        await connection.execute(
            update(tag_name).where(tag_name.c.name_key == key).values(name=display)
        )

    await events.record(
        connection,
        action=events.TAG_RENAMED,
        resource_type=events.RESOURCE_TAG,
        resource_id=tag_id,
        actor=actor,
        details={"from": current.name, "to": display},
    )
    renamed = await get(connection, tag_id)
    if renamed is None:  # pragma: no cover - renamed in this transaction
        raise RuntimeError(f"tag {tag_id} lost its name")
    return renamed


async def set_aliases(
    connection: AsyncConnection, *, tag_id: UUID, aliases: list[str], actor: Actor
) -> list[str]:
    """Replace a tag's synonyms with exactly this set, and say what changed.

    Declarative rather than add-one/remove-one: an admin editing a taxonomy thinks in terms of
    "these are the words for this tag", and a diff of one list is also the only shape a form
    can submit without inventing a protocol for absence.
    """
    current = await get(connection, tag_id)
    if current is None:
        raise UnknownTagError(tag_id)

    wanted: dict[str, str] = {}
    for alias in aliases:
        display = validate_name(alias)
        key = key_of(display)
        if key == current.name_key:
            raise InvalidTagNameError(f"'{display}' is this tag's own name, not a synonym for it")
        wanted[key] = display

    existing = {
        row.name_key: row.name
        for row in (
            await connection.execute(
                select(tag_name.c.name_key, tag_name.c.name).where(
                    tag_name.c.tag_id == tag_id, tag_name.c.is_alias
                )
            )
        ).all()
    }

    for key in set(existing) - set(wanted):
        await connection.execute(delete(tag_name).where(tag_name.c.name_key == key))
        await events.record(
            connection,
            action=events.TAG_ALIAS_REMOVED,
            resource_type=events.RESOURCE_TAG,
            resource_id=tag_id,
            actor=actor,
            details={"tag": current.name, "alias": existing[key]},
        )
    for key in set(wanted) - set(existing):
        await _refuse_taken(connection, key, wanted[key])
        await _claim_name(connection, key=key, name=wanted[key], tag_id=tag_id, is_alias=True)
        await events.record(
            connection,
            action=events.TAG_ALIAS_ADDED,
            resource_type=events.RESOURCE_TAG,
            resource_id=tag_id,
            actor=actor,
            details={"tag": current.name, "alias": wanted[key]},
        )
    return sorted(wanted.values(), key=key_of)


async def set_parents(
    connection: AsyncConnection, *, tag_id: UUID, parents: list[UUID], actor: Actor
) -> list[UUID]:
    """Replace a tag's direct parents with exactly this set. Refuses a cycle, rebuilds once."""
    current = await get(connection, tag_id)
    if current is None:
        raise UnknownTagError(tag_id)

    wanted = set(parents)
    if tag_id in wanted:
        raise CycleError(tag_id, tag_id)
    known = await by_ids(connection, list(wanted))
    for parent_id in wanted:
        if parent_id not in known:
            raise UnknownTagError(parent_id)

    existing = {
        row.parent_id
        for row in (
            await connection.execute(
                select(tag_edge.c.parent_id).where(tag_edge.c.child_id == tag_id)
            )
        ).all()
    }
    added = wanted - existing
    removed = existing - wanted
    if not added and not removed:
        return sorted(existing, key=str)

    # Cycle check before any write: `parent` may not already be below `tag`. The closure
    # carries depth-0 rows, so this also catches a tag being made its own parent.
    for parent_id in added:
        if await _reaches(connection, ancestor=tag_id, descendant=parent_id):
            raise CycleError(parent_id, tag_id)

    for parent_id in removed:
        await connection.execute(
            delete(tag_edge).where(tag_edge.c.parent_id == parent_id, tag_edge.c.child_id == tag_id)
        )
    for parent_id in added:
        await connection.execute(insert(tag_edge).values(parent_id=parent_id, child_id=tag_id))

    await rebuild_closure(connection)
    for parent_id in sorted(removed, key=str):
        await events.record(
            connection,
            action=events.TAG_PARENT_REMOVED,
            resource_type=events.RESOURCE_TAG,
            resource_id=tag_id,
            actor=actor,
            details={"tag": current.name, "parent": str(parent_id)},
        )
    for parent_id in sorted(added, key=str):
        await events.record(
            connection,
            action=events.TAG_PARENT_ADDED,
            resource_type=events.RESOURCE_TAG,
            resource_id=tag_id,
            actor=actor,
            details={"tag": current.name, "parent": str(parent_id)},
        )
    return sorted(wanted, key=str)


@dataclass(frozen=True, slots=True)
class Merged:
    """What a merge moved, so the response and the event can say it."""

    files: int
    folders: int
    claims: int
    aliases: int
    edges: int


async def merge(
    connection: AsyncConnection, *, source_id: UUID, target_id: UUID, actor: Actor
) -> Merged:
    """Fold one tag into another: same concept, two words.

    Everything the source carried moves, and its names become synonyms of the target — which is
    what makes a merge safe to do late: whatever anybody typed still resolves, it just resolves
    to one tag now. Three details are decisions rather than mechanics:

    - **A curation collision keeps the stronger statement**, `confirmed` over `manual` over
      `rejected`. A merge must not silently delete somebody's curation, and a positive one is
      the stronger word: the user said yes to this concept under one of its names.
    - **Edges are moved, then any that would close a loop are dropped.** Merging a child into
      its own parent is a legitimate thing to want, and it makes `parent → child` into
      `parent → parent`; refusing the whole merge over an edge the merge itself made redundant
      would be pedantry.
    - **The source row is deleted, not marked.** Its names live on as aliases and its
      applications moved, so there is nothing left for the row to mean. `rejected` is for a
      word the vocabulary refuses, which is the opposite of this.
    """
    source = await get(connection, source_id)
    target = await get(connection, target_id)
    if source is None:
        raise UnknownTagError(source_id)
    if target is None:
        raise UnknownTagError(target_id)
    if source_id == target_id:
        raise InvalidTagNameError("a tag cannot be merged into itself")

    files = await _merge_file_tags(connection, source_id=source_id, target_id=target_id)
    folders = await _merge_folder_tags(connection, source_id=source_id, target_id=target_id)
    claims = await _merge_claims(connection, source_id=source_id, target_id=target_id)
    edges = await _merge_edges(connection, source_id=source_id, target_id=target_id)

    aliases = await connection.execute(
        update(tag_name)
        .where(tag_name.c.tag_id == source_id)
        .values(tag_id=target_id, is_alias=True)
    )
    await connection.execute(delete(tag).where(tag.c.id == source_id))
    await rebuild_closure(connection)

    await events.record(
        connection,
        action=events.TAG_MERGED,
        resource_type=events.RESOURCE_TAG,
        resource_id=target_id,
        actor=actor,
        details={
            "merged": source.name,
            "into": target.name,
            "files": files,
            "folders": folders,
            "claims": claims,
        },
    )
    return Merged(
        files=files, folders=folders, claims=claims, aliases=aliases.rowcount, edges=edges
    )


#: Which curation statement survives a merge collision. Positive beats negative; between two
#: positives, the one that also carries a machine's claim behind it.
_CURATION_RANK = {"rejected": 0, "manual": 1, "confirmed": 2}


async def _merge_claims(connection: AsyncConnection, *, source_id: UUID, target_id: UUID) -> int:
    """Re-point the machine claims. A collision is one run saying the same thing twice.

    The unique key is (version, tag, run), so a run that claimed both words ends up with two
    rows that are now the same claim — the second is dropped rather than merged, because a
    claim carries no user's word to lose. Anything with no counterpart simply moves.
    """
    duplicate = file_auto_tag.alias("existing")
    already = (
        select(literal(1))
        .where(
            duplicate.c.file_version_id == file_auto_tag.c.file_version_id,
            duplicate.c.run_id == file_auto_tag.c.run_id,
            duplicate.c.tag_id == target_id,
        )
        .exists()
    )
    moved = await connection.execute(
        update(file_auto_tag)
        .where(file_auto_tag.c.tag_id == source_id, ~already)
        .values(tag_id=target_id)
    )
    await connection.execute(delete(file_auto_tag).where(file_auto_tag.c.tag_id == source_id))
    return moved.rowcount


async def _merge_file_tags(connection: AsyncConnection, *, source_id: UUID, target_id: UUID) -> int:
    rows = await connection.execute(
        select(
            file_tag.c.file_id, file_tag.c.tag_id, file_tag.c.provenance, file_tag.c.user_id
        ).where(file_tag.c.tag_id.in_([source_id, target_id]))
    )
    states: dict[UUID, dict[UUID, tuple[str, UUID]]] = {}
    for row in rows.all():
        states.setdefault(row.file_id, {})[row.tag_id] = (row.provenance, row.user_id)

    moved = 0
    for file_id, curation in states.items():
        source_row = curation.get(source_id)
        if source_row is None:
            continue
        target_row = curation.get(target_id)
        if target_row is None:
            await connection.execute(
                update(file_tag)
                .where(file_tag.c.file_id == file_id, file_tag.c.tag_id == source_id)
                .values(tag_id=target_id, updated_at=func.now())
            )
            moved += 1
            continue
        source_state, source_user = source_row
        if _CURATION_RANK[source_state] > _CURATION_RANK[target_row[0]]:
            # The author travels with the statement. Keeping the surviving row's user id would
            # make it say that *they* applied a tag somebody else did.
            await connection.execute(
                update(file_tag)
                .where(file_tag.c.file_id == file_id, file_tag.c.tag_id == target_id)
                .values(provenance=source_state, user_id=source_user, updated_at=func.now())
            )
        await connection.execute(
            delete(file_tag).where(file_tag.c.file_id == file_id, file_tag.c.tag_id == source_id)
        )
        moved += 1
    return moved


async def _merge_folder_tags(
    connection: AsyncConnection, *, source_id: UUID, target_id: UUID
) -> int:
    """Folder tags have one state, so a collision is a duplicate: keep one, drop the other."""
    already = select(folder_tag.c.folder_id).where(folder_tag.c.tag_id == target_id)
    moved = await connection.execute(
        update(folder_tag)
        .where(folder_tag.c.tag_id == source_id, folder_tag.c.folder_id.not_in(already))
        .values(tag_id=target_id)
    )
    await connection.execute(delete(folder_tag).where(folder_tag.c.tag_id == source_id))
    return moved.rowcount


async def _merge_edges(connection: AsyncConnection, *, source_id: UUID, target_id: UUID) -> int:
    """Re-point the source's edges at the target, skipping self-edges and duplicates."""
    parents = {
        row.parent_id
        for row in (
            await connection.execute(
                select(tag_edge.c.parent_id).where(tag_edge.c.child_id == source_id)
            )
        ).all()
    }
    children = {
        row.child_id
        for row in (
            await connection.execute(
                select(tag_edge.c.child_id).where(tag_edge.c.parent_id == source_id)
            )
        ).all()
    }
    await connection.execute(
        delete(tag_edge).where(
            or_(tag_edge.c.parent_id == source_id, tag_edge.c.child_id == source_id)
        )
    )

    moved = 0
    for parent_id in sorted(parents - {target_id}, key=str):
        if await _has_edge(connection, parent_id, target_id):
            continue
        if await _reaches(connection, ancestor=target_id, descendant=parent_id):
            # The merge made this edge a loop: `parent → source` where the parent is already
            # below the target. Dropping it is the only answer that keeps the graph a DAG.
            continue
        await connection.execute(insert(tag_edge).values(parent_id=parent_id, child_id=target_id))
        moved += 1
    for child_id in sorted(children - {target_id}, key=str):
        if await _has_edge(connection, target_id, child_id):
            continue
        if await _reaches(connection, ancestor=child_id, descendant=target_id):
            continue
        await connection.execute(insert(tag_edge).values(parent_id=target_id, child_id=child_id))
        moved += 1
    return moved


async def delete_tag(connection: AsyncConnection, *, tag_id: UUID, actor: Actor) -> None:
    """Erase a tag that nothing carries — ADR-0006's typo-grade mistake with no history.

    Anything with applications is refused: the vocabulary's answer for a word that turned out
    wrong is `rejected` (soft removal, name kept as a suppression record), not a hole where
    somebody's tag used to be.
    """
    found = await get(connection, tag_id)
    if found is None:
        raise UnknownTagError(tag_id)
    files = await _count(
        connection, select(func.count()).select_from(file_tag).where(file_tag.c.tag_id == tag_id)
    )
    folders = await _count(
        connection,
        select(func.count()).select_from(folder_tag).where(folder_tag.c.tag_id == tag_id),
    )
    # A machine's claim counts too: erasing the word would leave a run's output referring to
    # nothing, and "a machine mentioned it" is history in the sense ADR-0006 means.
    claims = await _count(
        connection,
        select(func.count()).select_from(file_auto_tag).where(file_auto_tag.c.tag_id == tag_id),
    )
    if files or folders or claims:
        raise TagInUseError(tag_id, files=files + claims, folders=folders)

    await connection.execute(delete(tag).where(tag.c.id == tag_id))
    await rebuild_closure(connection)
    await events.record(
        connection,
        action=events.TAG_DELETED,
        resource_type=events.RESOURCE_TAG,
        resource_id=tag_id,
        actor=actor,
        details={"name": found.name},
    )


class NotASuggestionError(Exception):
    """Approve and reject are answers to a *proposal*; a tag in the vocabulary is not one.

    Retiring an established word is a different act with different consequences (files carry it,
    searches use it) and deliberately has no endpoint yet: it would need a decision about what
    happens to those files, and nothing in v1 asks for it.
    """

    def __init__(self, tag_id: UUID, status: str) -> None:
        super().__init__(status)
        self.tag_id = tag_id
        self.status = status


async def review(
    connection: AsyncConnection, *, tag_id: UUID, approved: bool, reviewer: UUID, actor: Actor
) -> Tag:
    """Decide about a machine's proposal (F-003/FR-12): into the vocabulary, or turned down.

    Approving is the whole of it — the word becomes `active` and every claim already recorded
    against it becomes visible, searchable and completable without touching a single file row.

    Rejecting does two things, and the second is the point of the state: the word becomes
    `rejected` and **its claims are deleted**. What stays is the row and its name, which is the
    suppression record — a later run proposing the same word finds it already refused instead of
    creating the suggestion again (ADR-0006).
    """
    found = await get(connection, tag_id)
    if found is None:
        raise UnknownTagError(tag_id)
    if not found.is_suggestion:
        raise NotASuggestionError(tag_id, found.status)

    await connection.execute(
        update(tag)
        .where(tag.c.id == tag_id)
        .values(
            status="active" if approved else "rejected",
            reviewed_at=func.now(),
            reviewed_by=reviewer,
        )
    )
    discarded = 0
    if not approved:
        removed = await connection.execute(
            delete(file_auto_tag).where(file_auto_tag.c.tag_id == tag_id)
        )
        discarded = removed.rowcount
    await events.record(
        connection,
        action=events.TAG_APPROVED if approved else events.TAG_REJECTED,
        resource_type=events.RESOURCE_TAG,
        resource_id=tag_id,
        actor=actor,
        details={"name": found.name, "claims_discarded": discarded},
    )
    reviewed = await get(connection, tag_id)
    if reviewed is None:  # pragma: no cover - updated in this transaction
        raise RuntimeError(f"tag {tag_id} vanished while being reviewed")
    return reviewed


# ------------------------------------------------------------------------------- the closure


async def rebuild_closure(connection: AsyncConnection) -> int:
    """Recompute the whole reachability table from the edges. Returns the row count.

    One recursive query, bounded by the number of tags: in an acyclic graph no path can be
    longer than that, so the bound never truncates a legitimate taxonomy — it only keeps a
    graph that *should* be acyclic from spinning forever if it somehow is not. A cycle that got
    in anyway does not corrupt anything quietly either: it would produce a row saying a tag is
    its own ancestor at a depth above zero, which the table's check constraint refuses.
    """
    await connection.execute(delete(tag_closure))
    await connection.execute(
        insert(tag_closure).from_select(
            ["ancestor_id", "descendant_id", "depth"],
            select(tag.c.id, tag.c.id, literal(0)),
        )
    )
    bound = select(func.count()).select_from(tag).scalar_subquery()
    base = select(
        tag_edge.c.parent_id.label("ancestor_id"),
        tag_edge.c.child_id.label("descendant_id"),
        literal(1).label("depth"),
    ).cte("reach", recursive=True)
    step = (
        select(base.c.ancestor_id, tag_edge.c.child_id, base.c.depth + 1)
        .select_from(base.join(tag_edge, tag_edge.c.parent_id == base.c.descendant_id))
        .where(base.c.depth < bound)
    )
    reach = base.union(step)
    inserted = await connection.execute(
        insert(tag_closure).from_select(
            ["ancestor_id", "descendant_id", "depth"],
            # `min` because a DAG can connect two tags by paths of different lengths, and the
            # pair is the key. The shortest one is what a breadcrumb should show.
            select(reach.c.ancestor_id, reach.c.descendant_id, func.min(reach.c.depth)).group_by(
                reach.c.ancestor_id, reach.c.descendant_id
            ),
        )
    )
    return inserted.rowcount


async def _reaches(connection: AsyncConnection, *, ancestor: UUID, descendant: UUID) -> bool:
    """Whether `descendant` is at or below `ancestor` — one indexed lookup."""
    rows = await connection.execute(
        select(literal(1)).where(
            tag_closure.c.ancestor_id == ancestor, tag_closure.c.descendant_id == descendant
        )
    )
    return rows.first() is not None


async def _has_edge(connection: AsyncConnection, parent_id: UUID, child_id: UUID) -> bool:
    rows = await connection.execute(
        select(literal(1)).where(tag_edge.c.parent_id == parent_id, tag_edge.c.child_id == child_id)
    )
    return rows.first() is not None


async def _claim_name(
    connection: AsyncConnection, *, key: str, name: str, tag_id: UUID, is_alias: bool
) -> None:
    """Write one name into the registry, or say that somebody else got there first.

    The primary key is the real guarantee; the lookup that produces the helpful "that word is
    already `invoice`'s synonym" message is only for the common case. Losing this race means two
    admins named the same word in the same instant — rare enough to answer with "try again",
    and the alternative would be reporting the wrong reason after a failed insert.
    """
    try:
        await connection.execute(
            insert(tag_name).values(name_key=key, name=name, tag_id=tag_id, is_alias=is_alias)
        )
    except IntegrityError as clash:
        raise NameRaceError(name) from clash


async def _holder(connection: AsyncConnection, key: str) -> tuple[UUID, bool] | None:
    rows = await connection.execute(
        select(tag_name.c.tag_id, tag_name.c.is_alias).where(tag_name.c.name_key == key)
    )
    row = rows.first()
    return None if row is None else (row.tag_id, row.is_alias)


async def _refuse_taken(connection: AsyncConnection, key: str, display: str) -> None:
    held = await _holder(connection, key)
    if held is not None:
        raise NameTakenError(display, held[0], is_alias=held[1])


async def _count(connection: AsyncConnection, query: Select[tuple[int]]) -> int:
    rows = await connection.execute(query)
    return rows.scalar_one()


__all__ = [
    "COMPLETION_CANDIDATES",
    "Completion",
    "CycleError",
    "InvalidTagNameError",
    "Merged",
    "NameRaceError",
    "NameTakenError",
    "NotASuggestionError",
    "Resolved",
    "Tag",
    "TagInUseError",
    "UnknownTagError",
    "Usage",
    "aliases_of",
    "ancestors_of",
    "by_ids",
    "children_of",
    "complete",
    "create",
    "delete_tag",
    "get",
    "key_of",
    "listing",
    "merge",
    "normalize",
    "parents_of",
    "rebuild_closure",
    "rename",
    "resolve",
    "review",
    "set_aliases",
    "set_parents",
    "usage_of",
    "validate_name",
]
