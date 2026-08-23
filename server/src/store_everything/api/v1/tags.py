"""The tag vocabulary over HTTP: completion for everyone, taxonomy for admins.

Two audiences, one path. `GET /tags` is what a member's tag box calls on every keystroke, and
`POST`/`PATCH`/`DELETE` are how an admin curates the vocabulary those keystrokes complete
against ([F-003/FR-10](../../../../features/F-003-tagging.md)) — regular users apply existing
tags, they do not mint them.

`GET /tags` answers two different questions and says so in its shape:

- **with `prefix`** it is a *completion* — ranked by how much the caller uses each tag, capped
  at one page, aliases included, `active` tags only. A completion is not a listing: paging
  through what the tenth-best match would have been is not a thing a tag box does.
- **without** it is the *taxonomy listing* — canonical names in key order, cursor-paginated,
  and the one place an admin can ask for `suggested` or `rejected` tags.

The write surface is deliberately **declarative**: `PATCH` carries the parents and aliases a tag
should end up with, not a sequence of add/remove operations. An admin editing a taxonomy thinks
in whole sets, a form submits whole sets, and the server diffing them is what turns one request
into the several audit events the change actually consists of.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field, model_validator

from store_everything import tagging, tags
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
from store_everything.security import AdminCredential, CurrentCredential
from store_everything.tables import MAX_TAG_NAME_LENGTH

router = APIRouter(prefix="/tags", tags=["tags"])

#: A completion is one page — what a tag box shows, and at most what ranking by usage can cost
#: in one grouped count. The taxonomy listing uses the ordinary pagination defaults instead.
DEFAULT_COMPLETION_LIMIT = 20
MAX_COMPLETION_LIMIT = 50

#: How many parents or aliases one tag may be given in a single request. A tag with more than
#: this is a modelling problem, and an unbounded list is a request-size problem.
MAX_RELATIONS = 50

type StatusFilter = Literal["active", "suggested", "rejected", "all"]

_ALL_STATUSES = ("active", "suggested", "rejected")


class TagUsage(BaseSchema):
    """How much of **the caller's own** library carries the tag.

    Caller-scoped, not instance-wide: a member should not learn from an autocomplete how many
    files somebody else has tagged `divorce`, and instance admin is not data access
    ([07](../../../../specs/07-identity-permissions-sharing.md))."""

    files: int
    folders: int


class TagSummary(BaseSchema):
    id: UUID
    name: str
    """The canonical name, as an admin wrote it."""

    status: Literal["active", "suggested", "rejected"]
    usage: TagUsage
    parents: list[UUID]
    """Direct parents, so a client can rebuild the DAG from one listing rather than one request
    per node. Multi-parent is normal here (ADR-0006)."""

    matched: str | None = None
    """Which spelling matched, set only when completing: `car` offered for `automobile` has to
    be able to say why it was offered."""

    matched_alias: bool = False
    created_at: datetime


class TagReference(BaseSchema):
    id: UUID
    name: str


class TagDetail(BaseSchema):
    id: UUID
    name: str
    status: Literal["active", "suggested", "rejected"]
    aliases: list[str]
    parents: list[TagReference]
    children: list[TagReference]
    ancestors: list[TagReference]
    """Every tag above this one, nearest first — the breadcrumb, derived from the closure and
    never stored on anything (ADR-0006)."""

    usage: TagUsage
    created_at: datetime
    created_by: UUID | None
    """The admin who added the word. Absent for one a machine suggested."""


class TagCreateRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=MAX_TAG_NAME_LENGTH)
    parents: list[UUID] = Field(default_factory=list, max_length=MAX_RELATIONS)
    aliases: list[str] = Field(default_factory=list, max_length=MAX_RELATIONS)


class TagUpdateRequest(BaseSchema):
    """Every field optional; absent means unchanged, and a present list is the whole set."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_TAG_NAME_LENGTH)
    parents: list[UUID] | None = Field(default=None, max_length=MAX_RELATIONS)
    aliases: list[str] | None = Field(default=None, max_length=MAX_RELATIONS)

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.name is None and self.parents is None and self.aliases is None:
            raise ValueError("a request that changes nothing is not an update")
        return self


class TagMergeRequest(BaseSchema):
    into: UUID
    """The surviving tag. Everything the merged one carried moves here, and its names become
    synonyms — so whatever anybody typed still resolves."""


class TagMergeResult(BaseSchema):
    tag: TagDetail
    moved_files: int
    moved_folders: int


class AppliedTag(BaseSchema):
    """One tag as it sits on a file or a folder.

    Shared by both surfaces on purpose: a tag on a folder is the same word from the same
    vocabulary, and a client that can render one can render the other. `provenance` is what
    tells them apart in the other direction — a folder tag is always `manual`, because
    extractors never run on folders ([F-015/FR-9](../../../../features/F-015-folders.md))."""

    id: UUID
    name: str
    status: Literal["active", "suggested", "rejected"]
    """`suggested` is a machine's proposal, shown here clearly marked and excluded from search
    and completion until an admin approves it (F-003/FR-12)."""

    provenance: Literal["manual", "confirmed", "auto"]
    """Who put it there, and how much it can be trusted (ADR-0004). Visible in every response
    that carries a tag, which is F-003/FR-3's promise."""

    user: UUID | None
    """The person behind a `manual` or `confirmed` tag — Bob's id on Alice's file, which is what
    makes shared curation legible."""

    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, applied: tagging.Applied) -> AppliedTag:
        return cls(
            id=applied.tag.id,
            name=applied.tag.name,
            status=applied.tag.status,  # pyright: ignore[reportArgumentType]
            provenance=applied.provenance,  # pyright: ignore[reportArgumentType]
            user=applied.user_id,
            created_at=applied.created_at,
            updated_at=applied.updated_at,
        )


class TagApplyRequest(BaseSchema):
    """Which tag to apply — by id, or by any spelling of its name.

    Two ways because there are two callers. A picker has the id from a completion and should not
    re-resolve a word that may have been renamed since; a person typing has only the word, and
    resolving it through the alias table is how `automobile` lands on `car`."""

    tag: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=MAX_TAG_NAME_LENGTH)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.tag is None) == (self.name is None):
            raise ValueError("name a tag by `tag` or by `name`, not both and not neither")
        return self


async def target_of(connection: DatabaseConnection, request: TagApplyRequest) -> tags.Tag:
    """The tag a request means, or the problem explaining why nothing was applied.

    A name that resolves to nothing is a validation failure rather than a quiet creation: the
    vocabulary is admin-governed (F-003/FR-10), so a member typing an unknown word gets told
    that, not a new tag nobody approved.
    """
    if request.tag is not None:
        found = await tags.get(connection, request.tag)
        if found is None:
            raise _invalid(f"no tag {request.tag}", "/tag")
        return found
    resolved = await tags.resolve(connection, request.name or "")
    if resolved is None:
        raise _invalid(
            "no tag goes by that name; an administrator adds words to the vocabulary", "/name"
        )
    return resolved.tag


def not_vocabulary(refused: tagging.NotVocabularyError) -> ProblemException:
    """Why a tag that exists still cannot be applied — and what to do about it."""
    if refused.status == "suggested":
        return _conflict(
            "That tag is a pending suggestion. An administrator approves it before it can be "
            "applied by hand."
        )
    return _conflict("That tag was rejected and is no longer part of the vocabulary.")


def _not_found() -> ProblemException:
    return ProblemException(status=404, slug="not-found", title="Not found")


def _invalid(reason: str, pointer: str) -> ProblemException:
    return ProblemException(
        status=422,
        slug="validation",
        title="Validation failed",
        detail="1 request field(s) are invalid.",
        errors=[FieldProblem(detail=reason, pointer=pointer)],
    )


def _conflict(detail: str) -> ProblemException:
    return ProblemException(status=409, slug="conflict", title="Conflict", detail=detail)


def _taken(refused: tags.NameTakenError) -> ProblemException:
    kind = "a synonym of another tag" if refused.is_alias else "another tag's name"
    return _conflict(f"'{refused.name}' is already {kind} ({refused.tag_id}).")


def _raced(refused: tags.NameRaceError) -> ProblemException:
    return _conflict(f"'{refused.name}' was claimed while this request was running. Retry.")


async def _summaries(
    connection: DatabaseConnection,
    found: list[tags.Tag],
    *,
    owner_id: UUID,
    matched: dict[UUID, tuple[str, bool]] | None = None,
) -> list[TagSummary]:
    """One shape for both modes, with the counts and parents fetched per page, not per row."""
    ids = [one.id for one in found]
    usage = await tags.usage_of(connection, ids, owner_id=owner_id)
    parents = await tags.parents_of(connection, ids)
    return [
        TagSummary(
            id=one.id,
            name=one.name,
            status=one.status,  # pyright: ignore[reportArgumentType]
            usage=_usage(usage.get(one.id)),
            parents=parents.get(one.id, []),
            matched=(matched or {}).get(one.id, (None, False))[0],
            matched_alias=(matched or {}).get(one.id, (None, False))[1],
            created_at=one.created_at,
        )
        for one in found
    ]


def _usage(counted: tags.Usage | None) -> TagUsage:
    counted = counted or tags.Usage()
    return TagUsage(files=counted.files, folders=counted.folders)


async def _detail(connection: DatabaseConnection, found: tags.Tag, *, owner_id: UUID) -> TagDetail:
    usage = await tags.usage_of(connection, [found.id], owner_id=owner_id)
    parents = await tags.by_ids(
        connection, (await tags.parents_of(connection, [found.id]))[found.id]
    )
    return TagDetail(
        id=found.id,
        name=found.name,
        status=found.status,  # pyright: ignore[reportArgumentType]
        aliases=await tags.aliases_of(connection, found.id),
        parents=sorted(
            (TagReference(id=one.id, name=one.name) for one in parents.values()),
            key=lambda reference: reference.name,
        ),
        children=[
            TagReference(id=one.id, name=one.name)
            for one in await tags.children_of(connection, found.id)
        ],
        ancestors=[
            TagReference(id=one.id, name=one.name)
            for one in await tags.ancestors_of(connection, found.id)
        ],
        usage=_usage(usage.get(found.id)),
        created_at=found.created_at,
        created_by=found.created_by,
    )


# ------------------------------------------------------------------------------- reading


@router.get(
    "",
    summary="Complete a tag prefix, or list the taxonomy",
    response_model=Page[TagSummary],
    responses={422: {"description": "The status filter is not the caller's to ask for"}},
)
async def list_tags(
    credential: CurrentCredential,
    connection: DatabaseConnection,
    prefix: Annotated[str | None, Query(max_length=MAX_TAG_NAME_LENGTH)] = None,
    status: StatusFilter = "active",
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    cursor: str | None = None,
) -> Page[TagSummary]:
    """Completion when `prefix` is given, the taxonomy listing otherwise.

    The two modes have different defaults, because a tag box wants a handful of candidates while
    an admin browsing the taxonomy wants a page of it. Only `active` tags are vocabulary, so only
    an admin may ask to see the others: a `suggested` tag is a machine's proposal awaiting review
    (F-003/FR-12) and a `rejected` one is a word the instance turned down. Both are visible on
    the file that carries them; neither belongs in a member's picker.
    """
    if status != "active" and not credential.user.is_admin:
        raise _invalid("only an administrator may list tags that are not active", "/query/status")

    if prefix is not None:
        if cursor is not None:
            raise _invalid("a completion is one page and takes no cursor", "/query/cursor")
        if status != "active":
            raise _invalid("a completion is over active tags only", "/query/status")
        completions = await tags.complete(
            connection,
            prefix=prefix,
            limit=min(limit or DEFAULT_COMPLETION_LIMIT, MAX_COMPLETION_LIMIT),
            owner_id=credential.user.id,
        )
        matched = {one.tag.id: (one.matched, one.is_alias) for one in completions}
        return Page(
            data=await _summaries(
                connection,
                [one.tag for one in completions],
                owner_id=credential.user.id,
                matched=matched,
            )
        )

    statuses = _ALL_STATUSES if status == "all" else (status,)
    after = _decode(cursor)
    page_size = limit or DEFAULT_LIMIT
    found = await tags.listing(connection, statuses=statuses, limit=page_size + 1, after=after)
    page, more = found[:page_size], len(found) > page_size
    return Page(
        data=await _summaries(connection, page, owner_id=credential.user.id),
        next_cursor=encode_keyset_cursor(["tag", page[-1].name_key]) if more and page else None,
    )


def _decode(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    marker, key = decode_keyset_cursor(cursor, parts=2)
    if marker != "tag" or not key:
        raise InvalidCursor()
    return key


@router.get(
    "/{tag_id}",
    summary="Read one tag",
    response_model=TagDetail,
    responses={404: {"description": "No such tag"}},
)
async def read_tag(
    tag_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> TagDetail:
    """The whole picture for one tag: its synonyms, its place in the DAG, its usage.

    Readable by any member, including a `suggested` one: quarantine keeps a suggestion out of
    search and completion, and a member looking at the tag their own file was given has to be
    able to see what it is."""
    found = await tags.get(connection, tag_id)
    if found is None:
        raise _not_found()
    return await _detail(connection, found, owner_id=credential.user.id)


# ------------------------------------------------------------------------------- writing


@router.post(
    "",
    summary="Add a tag to the vocabulary",
    status_code=201,
    response_model=TagDetail,
    responses={
        403: {"description": "Not an administrator"},
        409: {"description": "That name already resolves to a tag"},
        422: {"description": "The name, a parent or an alias was refused"},
    },
)
async def create_tag(
    request: TagCreateRequest, credential: AdminCredential, connection: DatabaseConnection
) -> TagDetail:
    """Create an `active` tag, optionally with its parents and synonyms in one call."""
    actor = Actor.user(credential.user.id)
    try:
        created = await tags.create(
            connection, name=request.name, actor=actor, created_by=credential.user.id
        )
        if request.aliases:
            await tags.set_aliases(
                connection, tag_id=created.id, aliases=request.aliases, actor=actor
            )
        if request.parents:
            await tags.set_parents(
                connection, tag_id=created.id, parents=request.parents, actor=actor
            )
    except tags.NameRaceError as raced:
        raise _raced(raced) from raced
    except tags.NameTakenError as taken:
        raise _taken(taken) from taken
    except tags.InvalidTagNameError as refused:
        raise _invalid(refused.reason, "/name") from refused
    except tags.UnknownTagError as unknown:
        raise _invalid(f"no tag {unknown.tag_id}", "/parents") from unknown
    except tags.CycleError as cycle:
        raise _conflict("A tag cannot be its own ancestor.") from cycle
    return await _detail(connection, created, owner_id=credential.user.id)


@router.patch(
    "/{tag_id}",
    summary="Rename a tag, or change its parents and synonyms",
    response_model=TagDetail,
    responses={
        403: {"description": "Not an administrator"},
        404: {"description": "No such tag"},
        409: {"description": "The name is taken, or the edge would close a cycle"},
        422: {"description": "A name, parent or alias was refused"},
    },
)
async def update_tag(
    tag_id: UUID,
    request: TagUpdateRequest,
    credential: AdminCredential,
    connection: DatabaseConnection,
) -> TagDetail:
    """Move a tag in the DAG, rename it, or change what resolves to it.

    Restructuring is instant and touches no file rows — that is ADR-0006's whole reason for
    expanding at query time. The old name is not kept as a synonym on a rename: a rename says
    the word was wrong, and an admin who wants it to keep resolving adds it back as an alias in
    the same request.
    """
    found = await tags.get(connection, tag_id)
    if found is None:
        raise _not_found()
    actor = Actor.user(credential.user.id)
    try:
        if request.name is not None:
            found = await tags.rename(connection, tag_id=tag_id, name=request.name, actor=actor)
        if request.aliases is not None:
            await tags.set_aliases(connection, tag_id=tag_id, aliases=request.aliases, actor=actor)
        if request.parents is not None:
            await tags.set_parents(connection, tag_id=tag_id, parents=request.parents, actor=actor)
    except tags.NameRaceError as raced:
        raise _raced(raced) from raced
    except tags.NameTakenError as taken:
        raise _taken(taken) from taken
    except tags.InvalidTagNameError as refused:
        raise _invalid(refused.reason, "/name") from refused
    except tags.UnknownTagError as unknown:
        raise _invalid(f"no tag {unknown.tag_id}", "/parents") from unknown
    except tags.CycleError as cycle:
        raise _conflict(
            f"Tag {cycle.parent_id} already sits below {cycle.child_id}, "
            "so this edge would make it its own ancestor."
        ) from cycle
    return await _detail(connection, found, owner_id=credential.user.id)


@router.post(
    "/{tag_id}/merge",
    summary="Merge one tag into another",
    response_model=TagMergeResult,
    responses={
        403: {"description": "Not an administrator"},
        404: {"description": "No such tag"},
        422: {"description": "A tag cannot be merged into itself"},
    },
)
async def merge_tag(
    tag_id: UUID,
    request: TagMergeRequest,
    credential: AdminCredential,
    connection: DatabaseConnection,
) -> TagMergeResult:
    """Fold `tag_id` into `into`: same concept, two words.

    The merged tag's applications move, its names become synonyms of the survivor, and its
    place in the DAG is absorbed. Nothing a user curated is dropped — where both tags were on
    one file, the stronger statement wins (`confirmed` over `manual` over `rejected`).
    """
    if await tags.get(connection, tag_id) is None:
        raise _not_found()
    actor = Actor.user(credential.user.id)
    try:
        merged = await tags.merge(connection, source_id=tag_id, target_id=request.into, actor=actor)
    except tags.UnknownTagError as unknown:
        raise _invalid(f"no tag {unknown.tag_id}", "/into") from unknown
    except tags.InvalidTagNameError as refused:
        raise _invalid(refused.reason, "/into") from refused
    survivor = await tags.get(connection, request.into)
    if survivor is None:  # pragma: no cover - merged into it in this transaction
        raise _not_found()
    return TagMergeResult(
        tag=await _detail(connection, survivor, owner_id=credential.user.id),
        moved_files=merged.files,
        moved_folders=merged.folders,
    )


@router.delete(
    "/{tag_id}",
    summary="Erase a tag nothing carries",
    status_code=204,
    responses={
        403: {"description": "Not an administrator"},
        404: {"description": "No such tag"},
        409: {"description": "Something still carries it"},
    },
)
async def delete_tag(
    tag_id: UUID, credential: AdminCredential, connection: DatabaseConnection
) -> None:
    """Hard-delete a tag — reserved for a typo with no history (ADR-0006).

    A tag anything carries is refused: the answer for a word that turned out wrong is to reject
    it, which keeps the name as a suppression record and leaves the files that used it alone.
    """
    try:
        await tags.delete_tag(connection, tag_id=tag_id, actor=Actor.user(credential.user.id))
    except tags.UnknownTagError as unknown:
        raise _not_found() from unknown
    except tags.TagInUseError as in_use:
        raise _conflict(
            f"{in_use.files} file(s) and {in_use.folders} folder(s) still carry this tag. "
            "Reject it instead — that keeps the name from being suggested again."
        ) from in_use
