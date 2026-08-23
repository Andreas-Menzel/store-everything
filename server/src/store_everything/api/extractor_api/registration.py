"""`PUT /registration` — an extractor declares what it can do.

Called on every container start-up, so it is an idempotent upsert rather than a create: the
same manifest twice is one registration and one event. What it refuses is worth listing, since
each refusal is a contract rule an extractor author will meet:

- a manifest whose `id` is not the one the credential is bound to (`403`) — a container cannot
  register as somebody else;
- a contract version this core does not speak (`409`);
- an exclusive output kind another extractor already produces (`409`), naming the claimant;
- a manifest that could never be routed work, or that declares kinds without the matching
  output (`422`, with a pointer per field).

The response echoes the manifest **as understood**, unknown fields preserved: an author who
mistyped a field name sees it in the echo instead of wondering why nothing happens.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from store_everything import extractors
from store_everything.api.extractor_api.security import CurrentExtractor
from store_everything.db import DatabaseConnection
from store_everything.extractors import (
    ClaimRaceError,
    KindsAlreadyClaimedError,
    Manifest,
    UnsupportedContractVersionError,
)
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema
from store_everything.tables import EXTRACTOR_API_VERSION


class RegistrationAccepted(BaseSchema):
    extractor_id: str
    registered_at: datetime | None
    changed: bool
    """False when this manifest was already the registered one — nothing was written."""

    enabled: bool
    """Whether the core will route work here. An extractor may be registered and disabled."""

    manifest: dict[str, Any]
    """What the core understood, unknown fields included. Compare it against what was sent."""


async def register_extractor(
    payload: Manifest, credential: CurrentExtractor, connection: DatabaseConnection
) -> RegistrationAccepted:
    if payload.id != credential.extractor.id:
        raise ProblemException(
            status=403,
            slug="extractor-identity-mismatch",
            title="Manifest identity mismatch",
            detail=(
                f"This credential registers `{credential.extractor.id}`, "
                f"but the manifest declares `{payload.id}`."
            ),
        )

    try:
        outcome = await extractors.register(
            connection, current=credential.extractor, manifest=payload
        )
    except UnsupportedContractVersionError as unsupported:
        raise ProblemException(
            status=409,
            slug="unsupported-contract-version",
            title="Unsupported contract version",
            detail=(
                f"This instance speaks extractor-api/{EXTRACTOR_API_VERSION}; "
                f"the manifest declares `{unsupported.declared}`."
            ),
        ) from unsupported
    except KindsAlreadyClaimedError as claimed:
        raise ProblemException(
            status=409,
            slug="output-kind-already-claimed",
            title="Output kind already claimed",
            detail=(
                "Each rendition kind, derived-asset kind and embedding space has exactly one "
                "producer: "
                + "; ".join(
                    f"`{conflict.kind}` ({conflict.claim_type}) belongs to "
                    f"`{conflict.extractor_id}`"
                    for conflict in claimed.conflicts
                )
            ),
            errors=[
                FieldProblem(detail="already produced elsewhere", pointer=f"/body/{_field(c)}")
                for c in claimed.conflicts
            ],
        ) from claimed
    except ClaimRaceError as race:
        raise ProblemException(
            status=409,
            slug="output-kind-already-claimed",
            title="Output kind already claimed",
            detail=(
                "Another extractor claimed one of these kinds while this registration was "
                "running. Retry."
            ),
        ) from race

    return RegistrationAccepted(
        extractor_id=outcome.extractor.id,
        registered_at=outcome.extractor.registered_at,
        changed=outcome.changed,
        enabled=outcome.extractor.enabled,
        manifest=outcome.extractor.manifest or {},
    )


def _field(conflict: extractors.ClaimConflict) -> str:
    """The manifest field a conflicting claim came from."""
    return {
        "rendition": "renditions",
        "derived_asset": "derived_asset_kinds",
        "embedding_space": "embedding_spaces",
    }[conflict.claim_type]
