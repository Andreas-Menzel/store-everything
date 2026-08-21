"""Workspaces: creating one, and listing your own.

Two shapes of creation through one endpoint, because they differ only in who chose the
directory (ADR-0018). A member creates a `managed` workspace for themselves and never names a
path. An **admin** may pass `adopt_path` to index an existing tree in place — restricted to
the `SE_ADOPTION_ROOTS` allow-list, refused with the reason named when it is not
([F-001/FR-10](../../../../features/F-001-upload-and-import.md)).

Creation answers `201` with the workspace in state `provisioning`: the row and the intent to
build it commit together, and the `workspace.provision` operation creates the directory, plants
the control directory and registers the root folder (ADR-0010). Clients poll the workspace
until it is `active` rather than waiting on a filesystem inside a request.

Reading is **owner-only**, and a workspace someone else owns answers `404` rather than `403` —
instance administration is not data access (07), and a "forbidden" would confirm that the id
exists.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import Field

from store_everything import events, identity, names, operations, workspaces
from store_everything.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    decode_cursor,
    encode_cursor,
)
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema
from store_everything.security import CurrentCredential, Forbidden, settings_of

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class WorkspaceCreateRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=names.MAX_NAME_BYTES)
    owner: UUID | None = None
    """Whose workspace this is. Defaults to the caller; naming anyone else requires an admin,
    which is how an admin sets up an adopted tree for the member who will own it."""

    adopt_path: str | None = Field(default=None, min_length=1, max_length=names.MAX_PATH_BYTES)
    """An existing directory to index **in place**, nothing moved or copied. Admin-only, and
    only inside `SE_ADOPTION_ROOTS`. Absent means a managed workspace under `SE_DATA_ROOT`."""


class FilesystemVerdict(BaseSchema):
    """What the `fs-check` probe found on the filesystem holding this workspace."""

    probed: str
    usable: bool
    properties: dict[str, bool]
    facts: dict[str, str]
    """Behaviour that differs without being wrong — case folding, Unicode normalization. The
    first thing to look at when a scan reports a name collision nobody can see."""


class WorkspaceSummary(BaseSchema):
    id: UUID
    owner: UUID
    name: str
    source: Literal["local"]
    placement: Literal["managed", "adopted"]
    state: Literal["provisioning", "active"]
    root_path: str
    """The directory on the storage. Shown because recognising your own data without the app
    is the point of the layout (03), and only ever to the workspace's owner."""

    filesystem: FilesystemVerdict
    created_at: datetime

    @classmethod
    def of(cls, record: workspaces.Workspace) -> WorkspaceSummary:
        verdict = record.fs_check
        properties = verdict.get("properties", {})
        return cls(
            id=record.id,
            owner=record.owner_id,
            name=record.name,
            source=record.source,
            placement=record.placement,
            state=record.state,
            root_path=str(record.root_path),
            filesystem=FilesystemVerdict(
                probed=str(verdict.get("root", record.root_path)),
                usable=bool(verdict.get("usable", False)),
                properties={
                    name: bool(detail.get("satisfied", False))
                    for name, detail in properties.items()
                },
                facts=dict(verdict.get("facts", {})),
            ),
            created_at=record.created_at,
        )


def _invalid(reason: str, pointer: str) -> ProblemException:
    """The standard field-level refusal: the rule that was broken, never the value."""
    return ProblemException(
        status=422,
        slug="validation",
        title="Validation failed",
        detail="1 request field(s) are invalid.",
        errors=[FieldProblem(detail=reason, pointer=pointer)],
    )


def _conflict(detail: str, *, pointer: str | None = None) -> ProblemException:
    return ProblemException(
        status=409,
        slug="conflict",
        title="Conflict",
        detail=detail,
        errors=[FieldProblem(detail="already in use", pointer=pointer)] if pointer else [],
    )


@router.post(
    "",
    summary="Create a workspace",
    status_code=201,
    response_model=WorkspaceSummary,
    responses={
        403: {"description": "Only an admin may adopt a directory or act for another user"},
        409: {"description": "That name is taken, or another workspace covers that directory"},
        422: {"description": "The name, the owner, or the directory was refused"},
    },
)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    credential: CurrentCredential,
    connection: DatabaseConnection,
    request: Request,
) -> WorkspaceSummary:
    settings = settings_of(request)

    name = names.normalize_api_name(payload.name)
    try:
        names.validate_name(name)
    except names.InvalidNameError as invalid:
        raise _invalid(invalid.reason, "/body/name") from invalid

    owner = await _resolve_owner(connection, payload.owner, credential)

    if payload.adopt_path is None:
        placement: workspaces.Placement = "managed"
        root = await asyncio.to_thread(
            workspaces.managed_root, settings, owner_id=owner.id, name=name
        )
    else:
        if not credential.user.is_admin:
            # F-001/FR-10: a member cannot adopt at all, whatever the path.
            raise Forbidden(
                slug="admin-required",
                title="Administrator role required",
                detail="Adopting an existing directory is restricted to administrators.",
            )
        placement = "adopted"
        try:
            root = await asyncio.to_thread(
                workspaces.resolve_adopted_root, settings, payload.adopt_path
            )
        except workspaces.AdoptionRefusedError as refused:
            raise _invalid(refused.reason, "/body/adopt_path") from refused

    # Probed before the lock is taken: the probe does filesystem work, and holding a lock
    # across it would serialize every creation behind the slowest mount.
    verdict = await asyncio.to_thread(
        workspaces.preflight, settings, placement=placement, root=root
    )
    if not verdict.usable:
        raise _invalid(
            f"the filesystem there is not usable — {verdict.explain()}",
            "/body/adopt_path" if placement == "adopted" else "/body/name",
        )

    # "No root overlaps another" is a relation between rows, so it cannot be a constraint;
    # this makes the check that follows it hold until the insert.
    await workspaces.lock_admission(connection)
    if await workspaces.find_by_name(connection, owner_id=owner.id, name=name) is not None:
        raise _conflict("A workspace with that name already exists.", pointer="/body/name")
    clash = await workspaces.conflicting_root(connection, root)
    if clash is not None:
        raise _conflict(
            f"Another workspace already covers that directory: {clash.name}.",
            pointer="/body/adopt_path" if placement == "adopted" else None,
        )

    created = await workspaces.create(
        connection,
        owner_id=owner.id,
        name=name,
        placement=placement,
        root_path=root,
        verdict=verdict,
        actor=Actor.user(credential.user.id),
    )
    # Same transaction as the row: there is no window in which a workspace exists and nothing
    # is going to build it.
    await operations.enqueue(
        connection,
        kind=workspaces.KIND,
        max_attempts=settings.max_attempts,
        priority=operations.PRIORITY_INTERACTIVE,
        idempotency_key=f"{workspaces.KIND}:{created.id}",
        subject_type=events.RESOURCE_WORKSPACE,
        subject_id=created.id,
    )
    return WorkspaceSummary.of(created)


@router.get("", summary="List your workspaces", response_model=Page[WorkspaceSummary])
async def list_workspaces(
    credential: CurrentCredential,
    connection: DatabaseConnection,
    limit: Annotated[int, Query(gt=0, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[WorkspaceSummary]:
    after = decode_cursor(cursor) if cursor else None
    found = await workspaces.list_for_owner(
        connection, owner_id=credential.user.id, limit=limit + 1, after=after
    )

    page = found[:limit]
    next_cursor = (
        encode_cursor(page[-1].created_at, page[-1].id) if len(found) > limit and page else None
    )
    return Page(data=[WorkspaceSummary.of(record) for record in page], next_cursor=next_cursor)


@router.get(
    "/{workspace_id}",
    summary="Read one workspace",
    response_model=WorkspaceSummary,
    responses={404: {"description": "No such workspace, or not yours"}},
)
async def read_workspace(
    workspace_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> WorkspaceSummary:
    found = await workspaces.get(connection, workspace_id)
    # One answer for "does not exist" and "not yours": a distinguishable `403` would let
    # anyone enumerate which ids are real.
    if found is None or found.owner_id != credential.user.id:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return WorkspaceSummary.of(found)


async def _resolve_owner(
    connection: DatabaseConnection, requested: UUID | None, credential: identity.Credential
) -> identity.User:
    """Who will own the workspace. Anyone but the caller requires an admin."""
    if requested is None or requested == credential.user.id:
        return credential.user
    if not credential.user.is_admin:
        raise Forbidden(
            slug="admin-required",
            title="Administrator role required",
            detail="Only an administrator can create a workspace for another user.",
        )

    owner = await identity.get_user(connection, requested)
    if owner is None:
        raise _invalid("no such account", "/body/owner")
    if not owner.is_active:
        raise _invalid("that account is disabled", "/body/owner")
    return owner
