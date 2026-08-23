"""Extractor administration: install one, credential it, turn it off.

Admin-only, and instance administration rather than data access (07) — nothing here reads a
file, a tag or a result. The surface exists because installing an extractor is a deployment
act with a database half: a compose block gives the container a home, and this gives it an
identity and a credential (ADR-0020).

There is deliberately **no delete**, for the same reason accounts have none: an extractor id is
stamped into the provenance of every row it has ever produced (02 § invariants #3), so removing
one is a question about derived data, not about a registry row. `enabled: false` stops work
being routed to it, is reversible, and is what an operator actually needs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Response
from pydantic import Field

from store_everything import extractors
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.extractors import CostClass, Extractor, ExtractorToken, GpuMode, NetworkMode
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema
from store_everything.security import AdminCredential
from store_everything.tables import MAX_SLUG_LENGTH, SLUG_PATTERN

router = APIRouter(prefix="/extractors", tags=["extractors"])

ExtractorId = Annotated[str, Path(pattern=SLUG_PATTERN, max_length=MAX_SLUG_LENGTH)]

_MAX_TOKEN_NAME = 100


class ExtractorSummary(BaseSchema):
    id: str
    enabled: bool
    """Whether the core routes work here — the only thing this flag means."""

    registered: bool
    """Whether the container has ever declared itself. False = provisioned, never started."""

    version: str | None
    api_version: str | None
    model_name: str | None
    model_version: str | None
    cost_class: CostClass | None
    gpu: GpuMode | None
    network: NetworkMode | None
    manifest: dict[str, Any] | None
    """The manifest as registered, unknown fields included — what it declared, not only what
    this core understood (05 § compatibility rules)."""

    created_at: datetime
    registered_at: datetime | None
    last_seen_at: datetime | None

    @classmethod
    def of(cls, found: Extractor) -> ExtractorSummary:
        return cls(
            id=found.id,
            enabled=found.enabled,
            registered=found.is_registered,
            version=found.version,
            api_version=found.api_version,
            model_name=found.model_name,
            model_version=found.model_version,
            cost_class=found.cost_class,  # pyright: ignore[reportArgumentType]
            gpu=found.gpu,  # pyright: ignore[reportArgumentType]
            network=found.network,  # pyright: ignore[reportArgumentType]
            manifest=found.manifest,
            created_at=found.created_at,
            registered_at=found.registered_at,
            last_seen_at=found.last_seen_at,
        )


class ExtractorTokenSummary(BaseSchema):
    id: UUID
    extractor_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None

    @classmethod
    def of(cls, token: ExtractorToken) -> ExtractorTokenSummary:
        return cls(
            id=token.id,
            extractor_id=token.extractor_id,
            name=token.name,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
        )


class ExtractorProvisionRequest(BaseSchema):
    id: str = Field(pattern=SLUG_PATTERN, max_length=MAX_SLUG_LENGTH)
    """The extractor's own id, as its manifest declares it — `pdf-text`, `tesseract-ocr`."""

    token_name: str = Field(default="initial", min_length=1, max_length=_MAX_TOKEN_NAME)


class ExtractorUpdateRequest(BaseSchema):
    enabled: bool


class ExtractorTokenCreateRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=_MAX_TOKEN_NAME)


class ExtractorTokenCreated(BaseSchema):
    token: str
    """The plaintext, shown exactly once. It is not stored and cannot be shown again."""

    extractor_token: ExtractorTokenSummary


class ExtractorProvisioned(BaseSchema):
    token: str
    """The plaintext of the first credential — put it in the container's environment."""

    extractor: ExtractorSummary
    extractor_token: ExtractorTokenSummary


@router.get("", summary="List extractors", response_model=list[ExtractorSummary])
async def list_extractors(
    _admin: AdminCredential, connection: DatabaseConnection
) -> list[ExtractorSummary]:
    """Every provisioned extractor.

    Unpaginated: one row per installed container, so the set is bounded by the compose file.
    """
    found = await extractors.list_extractors(connection)
    return [ExtractorSummary.of(item) for item in found]


@router.post(
    "",
    summary="Provision an extractor",
    status_code=201,
    response_model=ExtractorProvisioned,
    responses={409: {"description": "That extractor id is already provisioned"}},
)
async def provision_extractor(
    payload: ExtractorProvisionRequest, admin: AdminCredential, connection: DatabaseConnection
) -> ExtractorProvisioned:
    """Allow an extractor id to exist, and mint its first credential.

    Provisioning comes first on purpose: an extractor cannot register itself into existence, so
    a credential is always bound to an id an administrator chose (ADR-0020).
    """
    if await extractors.get(connection, payload.id) is not None:
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="An extractor with that id is already provisioned.",
            errors=[FieldProblem(detail="already provisioned", pointer="/body/id")],
        )

    plaintext, created, token = await extractors.provision(
        connection,
        extractor_id=payload.id,
        token_name=payload.token_name,
        actor=Actor.user(admin.user.id),
    )
    return ExtractorProvisioned(
        token=plaintext,
        extractor=ExtractorSummary.of(created),
        extractor_token=ExtractorTokenSummary.of(token),
    )


@router.get(
    "/{extractor_id}",
    summary="Read one extractor",
    response_model=ExtractorSummary,
    responses={404: {"description": "No such extractor"}},
)
async def read_extractor(
    extractor_id: ExtractorId, _admin: AdminCredential, connection: DatabaseConnection
) -> ExtractorSummary:
    found = await extractors.get(connection, extractor_id)
    if found is None:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return ExtractorSummary.of(found)


@router.patch(
    "/{extractor_id}",
    summary="Enable or disable an extractor",
    response_model=ExtractorSummary,
    responses={404: {"description": "No such extractor"}},
)
async def update_extractor(
    extractor_id: ExtractorId,
    payload: ExtractorUpdateRequest,
    admin: AdminCredential,
    connection: DatabaseConnection,
) -> ExtractorSummary:
    """Disabling stops jobs being routed here. It does not stop the container registering:
    a disabled extractor keeps its manifest current so that re-enabling needs no restart."""
    updated = await extractors.set_enabled(
        connection,
        extractor_id=extractor_id,
        enabled=payload.enabled,
        actor=Actor.user(admin.user.id),
    )
    if updated is None:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return ExtractorSummary.of(updated)


@router.get(
    "/{extractor_id}/tokens",
    summary="List an extractor's credentials",
    response_model=list[ExtractorTokenSummary],
    responses={404: {"description": "No such extractor"}},
)
async def list_extractor_tokens(
    extractor_id: ExtractorId, _admin: AdminCredential, connection: DatabaseConnection
) -> list[ExtractorTokenSummary]:
    await _require_extractor(connection, extractor_id)
    found = await extractors.list_tokens(connection, extractor_id=extractor_id)
    return [ExtractorTokenSummary.of(token) for token in found]


@router.post(
    "/{extractor_id}/tokens",
    summary="Mint another credential for an extractor",
    status_code=201,
    response_model=ExtractorTokenCreated,
    responses={
        404: {"description": "No such extractor"},
        409: {"description": "That credential name has been used before"},
    },
)
async def create_extractor_token(
    extractor_id: ExtractorId,
    payload: ExtractorTokenCreateRequest,
    admin: AdminCredential,
    connection: DatabaseConnection,
) -> ExtractorTokenCreated:
    """Rotation: mint the replacement, restart the container with it, then revoke the old one."""
    await _require_extractor(connection, extractor_id)
    if await extractors.token_name_taken(connection, extractor_id=extractor_id, name=payload.name):
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail=(
                "This extractor has had a credential of that name. Names are kept when a "
                "credential is revoked, so that the audit trail stays readable."
            ),
            errors=[FieldProblem(detail="already used", pointer="/body/name")],
        )

    plaintext, created = await extractors.mint_token(
        connection,
        extractor_id=extractor_id,
        name=payload.name,
        actor=Actor.user(admin.user.id),
    )
    return ExtractorTokenCreated(token=plaintext, extractor_token=ExtractorTokenSummary.of(created))


@router.delete(
    "/{extractor_id}/tokens/{token_id}",
    summary="Revoke an extractor's credential",
    status_code=204,
    responses={404: {"description": "No such credential for this extractor"}},
)
async def revoke_extractor_token(
    extractor_id: ExtractorId,
    token_id: UUID,
    admin: AdminCredential,
    connection: DatabaseConnection,
) -> Response:
    revoked = await extractors.revoke_token(
        connection,
        extractor_id=extractor_id,
        token_id=token_id,
        actor=Actor.user(admin.user.id),
    )
    if not revoked:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return Response(status_code=204)


async def _require_extractor(connection: DatabaseConnection, extractor_id: str) -> Extractor:
    found = await extractors.get(connection, extractor_id)
    if found is None:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return found
