"""The extractor registry: which containers may analyse files, and what each can do.

One lifecycle with two halves, deliberately (ADR-0020):

- **an admin provisions** an extractor id and mints its credential. Nothing registers itself
  into existence — a token is bound to one id at mint time, so a leaked credential cannot
  invent a second extractor, and it cannot stamp another extractor's provenance either;
- **the container registers** its manifest on every start-up: an idempotent upsert declaring
  what it accepts, what it produces, its implementation and model versions.

Three rules are worth reading the code for:

1. **Single-provider kinds.** Rendition kinds, derived-asset kinds and embedding spaces have
   exactly one producer, enforced by the primary key of `extractor_claim` rather than by a
   check somewhere in this module. Two providers of `searchable-pdf` are therefore not
   "rejected", they are impossible — and the question of which one wins never arises.
   Segments, metadata and tags are the deliberate opposite: many extractors write them, and
   every row carries the run that produced it.
2. **Registration is silent when nothing changed.** Containers restart; the event log is the
   one table nothing ever deletes. So a re-declaration of an identical manifest updates
   liveness and writes no event, while a changed version writes one carrying what it replaced
   — which is exactly the eligibility data reprocessing needs (F-009/FR-2).
3. **The manifest is kept whole.** The contract tolerates fields a *newer* extractor declares
   and this core does not know yet (05 § compatibility rules), so unknown fields are preserved
   rather than dropped, and what the core understood is echoed back in the response — an
   author comparing the two can see a typo that would otherwise pass silently.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, insert, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, tokens
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import (
    EXTRACTOR_API_VERSION,
    EXTRACTOR_OUTPUT_KINDS,
    MAX_SLUG_LENGTH,
    SLUG_PATTERN,
    extractor,
    extractor_claim,
    extractor_token,
)
from store_everything.tokens import LAST_USED_RESOLUTION

type OutputKind = Literal[
    "metadata",
    "text_segments",
    "tags",
    "embeddings",
    "derived_assets",
    "renditions",
    "faces",
]
type ClaimType = Literal["rendition", "derived_asset", "embedding_space"]
type CostClass = Literal["light", "medium", "heavy"]
type GpuMode = Literal["none", "optional", "required"]
type NetworkMode = Literal["none", "outbound"]

#: Ceilings on the list-valued manifest fields. Not a policy about what an extractor may do —
#: a bound on what one registration can assert, so a manifest cannot become a denial of service
#: against routing.
MAX_MIME_PATTERNS = 100
MAX_DERIVED_KINDS = 50
MAX_RENDITIONS = 50
MAX_EMBEDDING_SPACES = 20
MAX_PARAMS_FROM = 20

#: `type/subtype`, `type/*`, or `*/*`. Deliberately narrower than RFC 2045's grammar: these are
#: routing patterns we match against detected media types, not values parsed out of a header.
_MIME_PATTERN = r"^(\*/\*|[a-z0-9][a-z0-9!#$&^_.+-]*/(\*|[a-z0-9][a-z0-9!#$&^_.+-]*))$"


# ------------------------------------------------------------------------------- manifest


class _ManifestPart(BaseModel):
    """Base for every manifest object.

    `extra="allow"` where the rest of the API forbids unknown fields (08 § conventions), and
    the reason is the direction of the compatibility promise: a request body is written by a
    client we ship, where a typo is a bug worth failing on, while a manifest is written by a
    third-party image that may speak a *later* `v1.x` than this core knows. Refusing it would
    make the contract's own compatibility rule unimplementable. What guards against typos
    instead is the echo: registration returns what the core understood.
    """

    model_config = ConfigDict(extra="allow", frozen=True)


class ManifestModelInfo(_ManifestPart):
    """The model an extractor runs, when it runs one.

    Versioned because the version is provenance: every row an extractor writes is stamped with
    it, and a bump is what makes files eligible for reprocessing (ADR-0004).
    """

    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=64)


class ManifestPredicate(_ManifestPart):
    """`accepts.when` — route here only once a well-known metadata key says so.

    How `pdf-text` hands scanned pages to OCR without either extractor knowing the other
    exists: one writes `needs_ocr`, the other declares it accepts PDFs when that key is true
    (04 § routing). Bound to keys, never to extractor ids.
    """

    key: str = Field(min_length=1, max_length=100)
    equals: bool | int | float | str


class ManifestAccepts(_ManifestPart):
    """What a job for this extractor may be about."""

    mime_types: tuple[str, ...] = Field(default=(), max_length=MAX_MIME_PATTERNS)
    derived_kinds: tuple[str, ...] = Field(default=(), max_length=MAX_DERIVED_KINDS)
    when: ManifestPredicate | None = None
    #: Well-known metadata key → job parameter name, copied into the job's params at routing.
    params_from: dict[str, str] = Field(default_factory=dict, max_length=MAX_PARAMS_FROM)

    @model_validator(mode="after")
    def _patterns_are_routable(self) -> Self:
        for pattern in self.mime_types:
            if not re.match(_MIME_PATTERN, pattern):
                raise ValueError(
                    f"{pattern!r} is not a media-type pattern; expected `type/subtype`, "
                    "`type/*` or `*/*`"
                )
        return self


class ManifestRendition(_ManifestPart):
    """One downloadable alternative form of a whole file (ADR-0008)."""

    kind: str = Field(pattern=SLUG_PATTERN, max_length=MAX_SLUG_LENGTH)
    format: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)


class Manifest(_ManifestPart):
    """What an extractor declares about itself (05 § manifest)."""

    id: str = Field(pattern=SLUG_PATTERN, max_length=MAX_SLUG_LENGTH)
    version: str = Field(min_length=1, max_length=64)
    api_version: str = Field(min_length=1, max_length=16)
    #: `model` on the wire. Spelled differently here because Pydantic reserves the `model_`
    #: namespace, and a field called `model` in a `BaseModel` is a trap waiting for a reader.
    declared_model: ManifestModelInfo | None = Field(default=None, alias="model")
    accepts: ManifestAccepts = Field(default_factory=ManifestAccepts)
    produces: tuple[OutputKind, ...] = Field(min_length=1, max_length=len(EXTRACTOR_OUTPUT_KINDS))
    renditions: tuple[ManifestRendition, ...] = Field(default=(), max_length=MAX_RENDITIONS)
    derived_asset_kinds: tuple[str, ...] = Field(default=(), max_length=MAX_DERIVED_KINDS)
    embedding_spaces: tuple[str, ...] = Field(default=(), max_length=MAX_EMBEDDING_SPACES)
    cost_class: CostClass = "medium"
    gpu: GpuMode = "none"
    network: NetworkMode = "none"

    @model_validator(mode="after")
    def _is_coherent(self) -> Self:
        if not self.accepts.mime_types and not self.accepts.derived_kinds:
            raise ValueError(
                "accepts must name at least one media-type pattern or derived kind; "
                "an extractor that accepts nothing can never be routed work"
            )

        # An output kind and the names it produces under have to arrive together: a declared
        # `renditions` output with no kinds is unroutable, and kinds without the output are a
        # claim on a namespace the extractor never writes to.
        for output, names, field in (
            ("renditions", self.renditions, "renditions"),
            ("derived_assets", self.derived_asset_kinds, "derived_asset_kinds"),
            ("embeddings", self.embedding_spaces, "embedding_spaces"),
        ):
            declares = output in self.produces
            if declares and not names:
                raise ValueError(f"produces `{output}`, so `{field}` must name at least one")
            if names and not declares:
                raise ValueError(f"`{field}` is declared, so `produces` must include `{output}`")

        for field, values in (
            ("produces", self.produces),
            ("renditions", tuple(rendition.kind for rendition in self.renditions)),
            ("derived_asset_kinds", self.derived_asset_kinds),
            ("embedding_spaces", self.embedding_spaces),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"`{field}` contains a duplicate")

        for field, values in (
            ("derived_asset_kinds", self.derived_asset_kinds),
            ("embedding_spaces", self.embedding_spaces),
        ):
            for value in values:
                _require_slug(value, field=field)
        for value in self.accepts.derived_kinds:
            _require_slug(value, field="accepts.derived_kinds")
        return self

    def document(self) -> dict[str, Any]:
        """The manifest as it will be stored and echoed: wire names, JSON-safe values."""
        return self.model_dump(by_alias=True, mode="json")


def _require_slug(value: str, *, field: str) -> None:
    if not re.match(SLUG_PATTERN, value) or len(value) > MAX_SLUG_LENGTH:
        raise ValueError(
            f"`{field}` entry {value!r} is not a valid name: lower-case letters, digits and "
            f"single hyphens, at most {MAX_SLUG_LENGTH} characters"
        )


# --------------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class Extractor:
    """A row of the registry. Manifest fields are absent until the container first registers."""

    id: str
    version: str | None
    api_version: str | None
    model_name: str | None
    model_version: str | None
    cost_class: str | None
    gpu: str | None
    network: str | None
    manifest: dict[str, Any] | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    registered_at: datetime | None
    last_seen_at: datetime | None

    @property
    def is_registered(self) -> bool:
        return self.manifest is not None


@dataclass(frozen=True, slots=True)
class ExtractorToken:
    id: UUID
    extractor_id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExtractorCredential:
    """An authenticated extractor container: which extractor it is, and on which token."""

    extractor: Extractor
    token_id: UUID


@dataclass(frozen=True, slots=True)
class Claim:
    """One exclusive output name an extractor declares."""

    claim_type: ClaimType
    kind: str


@dataclass(frozen=True, slots=True)
class ClaimConflict:
    claim_type: ClaimType
    kind: str
    extractor_id: str


@dataclass(frozen=True, slots=True)
class Registration:
    extractor: Extractor
    changed: bool
    """False when the manifest was already exactly this — no event, no new `registered_at`."""


class UnsupportedContractVersionError(Exception):
    """The extractor speaks a contract version this core does not."""

    def __init__(self, declared: str) -> None:
        super().__init__(declared)
        self.declared = declared


class KindsAlreadyClaimedError(Exception):
    """Another extractor already produces one of the declared exclusive kinds."""

    def __init__(self, conflicts: Sequence[ClaimConflict]) -> None:
        super().__init__(
            ", ".join(f"{c.claim_type} {c.kind} ({c.extractor_id})" for c in conflicts)
        )
        self.conflicts = tuple(conflicts)


class ClaimRaceError(Exception):
    """A concurrent registration took one of the declared kinds between check and insert."""


_COLUMNS = (
    extractor.c.id,
    extractor.c.version,
    extractor.c.api_version,
    extractor.c.model_name,
    extractor.c.model_version,
    extractor.c.cost_class,
    extractor.c.gpu,
    extractor.c.network,
    extractor.c.manifest,
    extractor.c.enabled,
    extractor.c.created_at,
    extractor.c.updated_at,
    extractor.c.registered_at,
    extractor.c.last_seen_at,
)

_TOKEN_COLUMNS = (
    extractor_token.c.id,
    extractor_token.c.extractor_id,
    extractor_token.c.name,
    extractor_token.c.created_at,
    extractor_token.c.last_used_at,
)


def _as_extractor(row: Sequence[Any]) -> Extractor:
    return Extractor(*row)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------------ the admin surface


async def get(connection: AsyncConnection, extractor_id: str) -> Extractor | None:
    result = await connection.execute(select(*_COLUMNS).where(extractor.c.id == extractor_id))
    row = result.first()
    return _as_extractor(tuple(row)) if row is not None else None


async def list_extractors(connection: AsyncConnection) -> list[Extractor]:
    """Every registered extractor, by id.

    Unpaginated on purpose: there is one row per installed container, so the set is bounded by
    the compose file rather than by the library (10-30 accounts, a handful of extractors).
    """
    result = await connection.execute(select(*_COLUMNS).order_by(extractor.c.id))
    return [_as_extractor(tuple(row)) for row in result.all()]


async def provision(
    connection: AsyncConnection, *, extractor_id: str, token_name: str, actor: Actor
) -> tuple[str, Extractor, ExtractorToken]:
    """Allow an extractor id to exist, and mint its first credential.

    Returns the token's plaintext, which is shown once and never stored.
    """
    result = await connection.execute(
        insert(extractor).values(id=extractor_id).returning(*_COLUMNS)
    )
    created = _as_extractor(tuple(result.one()))

    await events.record(
        connection,
        action=events.EXTRACTOR_PROVISIONED,
        resource_type=events.RESOURCE_EXTRACTOR,
        actor=actor,
        details={"extractor_id": created.id},
    )
    plaintext, minted = await mint_token(
        connection, extractor_id=created.id, name=token_name, actor=actor
    )
    return plaintext, created, minted


async def set_enabled(
    connection: AsyncConnection, *, extractor_id: str, enabled: bool, actor: Actor
) -> Extractor | None:
    """Turn routing to this extractor on or off. `None` when there is no such extractor.

    A no-op change writes no event: an audit reader looking for "who turned OCR off" should
    find that, not a run of identical rows from a client that resends its state.
    """
    current = await get(connection, extractor_id)
    if current is None:
        return None
    if current.enabled == enabled:
        return current

    result = await connection.execute(
        update(extractor)
        .where(extractor.c.id == extractor_id)
        .values(enabled=enabled, updated_at=func.now())
        .returning(*_COLUMNS)
    )
    updated = _as_extractor(tuple(result.one()))
    await events.record(
        connection,
        action=events.EXTRACTOR_ENABLED if enabled else events.EXTRACTOR_DISABLED,
        resource_type=events.RESOURCE_EXTRACTOR,
        actor=actor,
        details={"extractor_id": extractor_id},
    )
    return updated


# ---------------------------------------------------------------------------- credentials


async def mint_token(
    connection: AsyncConnection, *, extractor_id: str, name: str, actor: Actor
) -> tuple[str, ExtractorToken]:
    """Mint a credential for one extractor. Its plaintext is returned once and never stored."""
    minted = tokens.mint(tokens.EXTRACTOR_TOKEN_PREFIX)
    result = await connection.execute(
        insert(extractor_token)
        .values(
            id=new_id(),
            extractor_id=extractor_id,
            name=name.strip(),
            token_hash=minted.digest,
        )
        .returning(*_TOKEN_COLUMNS)
    )
    created = ExtractorToken(*tuple(result.one()))  # pyright: ignore[reportArgumentType]

    await events.record(
        connection,
        action=events.EXTRACTOR_TOKEN_CREATED,
        resource_type=events.RESOURCE_EXTRACTOR,
        resource_id=created.id,
        actor=actor,
        # The name and the extractor, never the value.
        details={"extractor_id": extractor_id, "name": created.name},
    )
    return minted.plaintext, created


async def token_name_taken(connection: AsyncConnection, *, extractor_id: str, name: str) -> bool:
    """Whether this extractor has *ever* had a token of this name.

    Revoked rows keep their name — a credential that was used has to stay identifiable in the
    audit trail — and the unique constraint counts them, so reuse has to be refused here rather
    than surfacing as a constraint violation.
    """
    result = await connection.execute(
        select(extractor_token.c.id).where(
            extractor_token.c.extractor_id == extractor_id,
            extractor_token.c.name == name.strip(),
        )
    )
    return result.first() is not None


async def list_tokens(connection: AsyncConnection, *, extractor_id: str) -> list[ExtractorToken]:
    result = await connection.execute(
        select(*_TOKEN_COLUMNS)
        .where(
            extractor_token.c.extractor_id == extractor_id,
            extractor_token.c.revoked_at.is_(None),
        )
        .order_by(extractor_token.c.created_at.desc())
    )
    return [
        ExtractorToken(*tuple(row))  # pyright: ignore[reportArgumentType]
        for row in result.all()
    ]


async def revoke_token(
    connection: AsyncConnection, *, extractor_id: str, token_id: UUID, actor: Actor
) -> bool:
    result = await connection.execute(
        update(extractor_token)
        .where(
            extractor_token.c.id == token_id,
            extractor_token.c.extractor_id == extractor_id,
            extractor_token.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(extractor_token.c.name)
    )
    row = result.first()
    if row is None:
        return False

    await events.record(
        connection,
        action=events.EXTRACTOR_TOKEN_REVOKED,
        resource_type=events.RESOURCE_EXTRACTOR,
        resource_id=token_id,
        actor=actor,
        details={"extractor_id": extractor_id, "name": row[0]},
    )
    return True


async def authenticate(connection: AsyncConnection, *, token: str) -> ExtractorCredential | None:
    """Resolve an extractor's bearer token, or `None`.

    Deliberately indifferent to `enabled`: a disabled extractor still authenticates and still
    registers, keeping its manifest current, and simply gets no work routed to it. That keeps
    `enabled` meaning exactly one thing.
    """
    now = datetime.now(UTC)
    result = await connection.execute(
        select(extractor_token.c.id, extractor_token.c.last_used_at, *_COLUMNS)
        .join(extractor, extractor.c.id == extractor_token.c.extractor_id)
        .where(
            extractor_token.c.token_hash == tokens.digest(token),
            extractor_token.c.revoked_at.is_(None),
        )
    )
    row = result.first()
    if row is None:
        return None

    token_id, last_used_at, *columns = tuple(row)
    found = _as_extractor(columns)

    if last_used_at is None or now - last_used_at >= LAST_USED_RESOLUTION:
        await connection.execute(
            update(extractor_token).where(extractor_token.c.id == token_id).values(last_used_at=now)
        )
        # Liveness of the container, not of the credential: "is this extractor running" is the
        # question an admin asks, and it is answered by any authenticated call.
        await connection.execute(
            update(extractor).where(extractor.c.id == found.id).values(last_seen_at=now)
        )

    return ExtractorCredential(extractor=found, token_id=token_id)


# --------------------------------------------------------------------------- registration


async def claimant(connection: AsyncConnection, *, claim_type: str, kind: str) -> str | None:
    """Which extractor produces this kind — the single-provider rule read back (ADR-0020).

    `None` means nothing installed produces it, which is a deployment fact rather than an error:
    an instance without an OCR container simply cannot make a searchable PDF, and the surface
    that was asked has to say so.
    """
    rows = await connection.execute(
        select(extractor_claim.c.extractor_id).where(
            extractor_claim.c.claim_type == claim_type, extractor_claim.c.kind == kind
        )
    )
    row = rows.first()
    return None if row is None else row.extractor_id


def claims_of(manifest: Manifest) -> tuple[Claim, ...]:
    """The exclusive output names this manifest asserts ownership of."""
    return (
        *(Claim("rendition", rendition.kind) for rendition in manifest.renditions),
        *(Claim("derived_asset", kind) for kind in manifest.derived_asset_kinds),
        *(Claim("embedding_space", space) for space in manifest.embedding_spaces),
    )


async def register(
    connection: AsyncConnection, *, current: Extractor, manifest: Manifest
) -> Registration:
    """Upsert one extractor's manifest.

    Idempotent by design — a container calls this on every start-up. The order matters: the
    exclusive kinds are settled *before* the manifest lands, so a refused registration leaves
    the previous manifest in place rather than a half-applied one.
    """
    if manifest.api_version != EXTRACTOR_API_VERSION:
        raise UnsupportedContractVersionError(manifest.api_version)

    document = manifest.document()
    if current.manifest == document:
        return Registration(extractor=current, changed=False)

    await _reconcile_claims(connection, extractor_id=current.id, declared=claims_of(manifest))

    model = manifest.declared_model
    result = await connection.execute(
        update(extractor)
        .where(extractor.c.id == current.id)
        .values(
            version=manifest.version,
            api_version=manifest.api_version,
            model_name=model.name if model is not None else None,
            model_version=model.version if model is not None else None,
            cost_class=manifest.cost_class,
            gpu=manifest.gpu,
            network=manifest.network,
            manifest=document,
            registered_at=func.now(),
            updated_at=func.now(),
        )
        .returning(*_COLUMNS)
    )
    updated = _as_extractor(tuple(result.one()))

    await events.record(
        connection,
        action=events.EXTRACTOR_REGISTERED,
        resource_type=events.RESOURCE_EXTRACTOR,
        actor=Actor.extractor(),
        details={
            "extractor_id": updated.id,
            "version": updated.version,
            "model_version": updated.model_version,
            # What it replaced — null on a first registration. This pair is what makes
            # "which files predate the current version" answerable (F-009/FR-2).
            "previous_version": current.version,
            "previous_model_version": current.model_version,
        },
    )
    return Registration(extractor=updated, changed=True)


def _pairs(claims: Sequence[Claim]) -> list[tuple[str, str]]:
    return [(claim.claim_type, claim.kind) for claim in claims]


async def _reconcile_claims(
    connection: AsyncConnection, *, extractor_id: str, declared: Sequence[Claim]
) -> None:
    """Make this extractor's claims exactly `declared`, or refuse the whole registration.

    Dropping a kind from a manifest releases it, so a capability can move between extractors
    by editing the two manifests — the release and the acquisition just cannot be simultaneous,
    which is the point of the constraint.
    """
    key = tuple_(extractor_claim.c.claim_type, extractor_claim.c.kind)
    pairs = _pairs(declared)

    if pairs:
        taken = await connection.execute(
            select(
                extractor_claim.c.claim_type,
                extractor_claim.c.kind,
                extractor_claim.c.extractor_id,
            ).where(key.in_(pairs), extractor_claim.c.extractor_id != extractor_id)
        )
        conflicts = [ClaimConflict(*tuple(row)) for row in taken.all()]  # pyright: ignore[reportArgumentType]
        if conflicts:
            raise KindsAlreadyClaimedError(conflicts)

    stale = delete(extractor_claim).where(extractor_claim.c.extractor_id == extractor_id)
    if pairs:
        stale = stale.where(key.not_in(pairs))
    await connection.execute(stale)

    if not pairs:
        return

    held = await connection.execute(
        select(extractor_claim.c.claim_type, extractor_claim.c.kind).where(
            extractor_claim.c.extractor_id == extractor_id
        )
    )
    existing = {(row[0], row[1]) for row in held.all()}
    missing = [
        {"claim_type": claim_type, "kind": kind}
        for claim_type, kind in pairs
        if (claim_type, kind) not in existing
    ]
    if not missing:
        return

    try:
        await connection.execute(
            insert(extractor_claim),
            [{**row, "extractor_id": extractor_id} for row in missing],
        )
    except IntegrityError as clash:
        # The primary key is the real guarantee; the query above is only what turns the common
        # case into a helpful message. Losing this race means another extractor registered the
        # same kind in the same instant — rare enough to answer with "try again".
        raise ClaimRaceError(str(clash)) from clash


__all__ = [
    "Claim",
    "ClaimConflict",
    "ClaimRaceError",
    "Extractor",
    "ExtractorCredential",
    "ExtractorToken",
    "KindsAlreadyClaimedError",
    "Manifest",
    "Registration",
    "UnsupportedContractVersionError",
    "authenticate",
    "claimant",
    "claims_of",
    "get",
    "list_extractors",
    "list_tokens",
    "mint_token",
    "provision",
    "register",
    "revoke_token",
    "set_enabled",
    "token_name_taken",
]
