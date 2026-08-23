"""The `/api/v1` router.

Two routers, one prefix. Everything mounted on the authenticated one inherits
`require_auth` plus the request ceiling — that is the deny-by-default rule made
structural: a route added without an auth decision is closed, not open.

The public one carries the documented exceptions, and there is exactly one today:
`POST /auth/login`, which cannot require a credential because it issues them. Anything
else appearing there is a spec bug (08-api-principles.md § endpoint map), and a test
asserts the whole public surface rather than trusting review to notice.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from store_everything.api.v1 import auth, extractors, files, folders, uploads, users, workspaces
from store_everything.security import enforce_request_ceiling, require_auth

API_V1_PREFIX = "/api/v1"


async def openapi_schema(request: Request) -> JSONResponse:
    """The machine-readable API contract — for authenticated users only.

    A deliberate deviation from "no docs in production": for an API-first self-hosted
    product the schema is a feature (08-api-principles.md). It is never public, and
    `SE_API_DOCS_ENABLED=false` removes the route entirely.
    """
    return JSONResponse(request.app.openapi())


def build_v1_router(*, api_docs_enabled: bool) -> APIRouter:
    """The authenticated half of `/api/v1`."""
    router = APIRouter(
        prefix=API_V1_PREFIX,
        # The ceiling first, deliberately: dependencies resolve in order, so with `require_auth`
        # ahead of it every bad credential was refused `401` before it was ever counted — an
        # unlimited supply of unauthenticated work, one pooled connection each.
        dependencies=[Depends(enforce_request_ceiling), Depends(require_auth)],
    )

    if api_docs_enabled:
        router.add_api_route(
            "/openapi.json",
            openapi_schema,
            methods=["GET"],
            summary="OpenAPI schema",
            tags=["meta"],
            response_model=None,
        )

    router.include_router(auth.router)
    router.include_router(users.router)
    router.include_router(extractors.router)
    router.include_router(workspaces.router)
    router.include_router(uploads.router)
    router.include_router(folders.router)
    router.include_router(files.router)
    return router


def build_public_v1_router() -> APIRouter:
    """The documented unauthenticated surface under `/api/v1`.

    Deliberately assembled separately so that "which endpoints are public" is a list one
    can read, not a property emerging from decorators scattered across modules.
    """
    router = APIRouter(prefix=API_V1_PREFIX)
    router.include_router(auth.public_router)
    return router
