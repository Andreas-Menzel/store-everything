"""Authenticating an extractor container.

A separate credential space from the user-facing API, not a scope inside it: an extractor token
authenticates a *component*, and the two spaces do not overlap in either direction — a personal
access token is refused here, and an extractor token is refused on `/api/v1` (its digest is
simply not in the table that endpoint looks in). That is what keeps a leaked extractor
credential from becoming a way to read files.

The token also carries *identity*: it is bound to one extractor id at mint time, so a container
can only ever act as itself — claim its own jobs, register its own manifest, stamp its own
provenance (ADR-0020).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from store_everything import extractors
from store_everything.extractors import ExtractorCredential
from store_everything.security import AuthenticationRequired

_BEARER = "bearer"


async def require_extractor(request: Request) -> ExtractorCredential:
    """Authenticate the calling extractor, or refuse the request.

    On its own short-lived connection rather than the handler's transaction, for the same
    reason `require_auth` is: stamping liveness is a diagnostic that should persist even when
    the handler's own work rolls back, and an unauthenticated caller is refused before any
    request transaction is opened.
    """
    authorization = request.headers.get("authorization")
    if authorization is None:
        raise AuthenticationRequired("This endpoint requires an extractor credential.")

    kind, _, value = authorization.partition(" ")
    if kind.lower() != _BEARER or not value:
        raise AuthenticationRequired("Expected an `Authorization: Bearer <token>` header.")

    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as connection:
        credential = await extractors.authenticate(connection, token=value.strip())
        await connection.commit()

    if credential is None:
        raise AuthenticationRequired("The extractor credential is not valid.")
    return credential


CurrentExtractor = Annotated[ExtractorCredential, Depends(require_extractor)]
