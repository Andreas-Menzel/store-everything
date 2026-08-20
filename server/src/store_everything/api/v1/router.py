"""The `/api/v1` router.

Every route mounted here inherits `require_auth`. That is the deny-by-default rule made
structural: a route added without an auth decision is closed, not open.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from store_everything.security import require_auth

API_V1_PREFIX = "/api/v1"


async def openapi_schema(request: Request) -> JSONResponse:
    """The machine-readable API contract — for authenticated users only.

    A deliberate deviation from "no docs in production": for an API-first self-hosted
    product the schema is a feature (08-api-principles.md). It is never public, and
    `SE_API_DOCS_ENABLED=false` removes the route entirely.
    """
    return JSONResponse(request.app.openapi())


def build_v1_router(*, api_docs_enabled: bool) -> APIRouter:
    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(require_auth)])

    if api_docs_enabled:
        router.add_api_route(
            "/openapi.json",
            openapi_schema,
            methods=["GET"],
            summary="OpenAPI schema",
            tags=["meta"],
            response_model=None,
        )

    return router
