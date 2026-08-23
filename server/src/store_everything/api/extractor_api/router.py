"""The `/extractor-api/v1` router, and the document that describes it.

A second contract, not a corner of the first one (ADR-0020). It has its own audience (people
writing extractor images), its own version (`extractor-api/v1`, independent of the app's SemVer
and of `/api/v1`), and its own credential space — so it gets **its own OpenAPI document**,
exported to `openapi-extractor.json` beside the user-facing one. Routes are added to the app
with `include_in_schema=False`, which keeps them out of the user-facing document; this module
builds theirs from a throwaway application holding nothing else.

Everything here is authenticated with an extractor credential, and everything is
extractor-initiated: the core never calls into a container, which is what lets an extractor run
on a network with no inbound route and no egress at all (ADR-0021).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from store_everything.api import operation_id
from store_everything.api.extractor_api import registration
from store_everything.api.extractor_api.security import require_extractor
from store_everything.problems import install_exception_handlers
from store_everything.security import enforce_request_ceiling
from store_everything.tables import EXTRACTOR_API_VERSION

EXTRACTOR_API_PREFIX = f"/extractor-api/{EXTRACTOR_API_VERSION}"

_TITLE = "Store Everything — extractor contract"
_SUMMARY = "The fixed API between the core and an extractor container."
_DESCRIPTION = (
    "Every call is authenticated with the extractor's own bearer credential "
    "(`Authorization: Bearer seext_…`), minted by an administrator and bound to one extractor "
    "id. Dispatch is poll-based: the extractor claims work, heartbeats it, and submits one "
    "result envelope per job. See specs/05-extractor-contract.md."
)


async def extractor_openapi_schema(request: Request) -> JSONResponse:
    """The extractor contract, for the containers that speak it.

    Behind the same credential as the rest of this API, for the same reason the user-facing
    schema is authenticated (08): a self-hosted instance publishes nothing it need not. Extractor
    authors read the copy committed in the repository rather than one served by somebody's box.
    """
    return JSONResponse(extractor_api_document())


def build_extractor_api_router(*, api_docs_enabled: bool) -> APIRouter:
    router = APIRouter(
        prefix=EXTRACTOR_API_PREFIX,
        tags=["extractor-api"],
        # Same order as `/api/v1`: authenticate first, then count — an unauthenticated caller
        # must not be able to spend the budget of a credential it does not have.
        dependencies=[Depends(enforce_request_ceiling), Depends(require_extractor)],
    )

    if api_docs_enabled:
        router.add_api_route(
            "/openapi.json",
            extractor_openapi_schema,
            methods=["GET"],
            summary="The extractor contract",
            response_model=None,
        )

    router.add_api_route(
        "/registration",
        registration.register_extractor,
        methods=["PUT"],
        summary="Declare what this extractor can do",
        response_model=registration.RegistrationAccepted,
        responses={
            403: {"description": "The manifest declares a different extractor id"},
            409: {"description": "Unsupported contract version, or a kind another produces"},
        },
    )
    return router


def extractor_api_document() -> dict[str, Any]:
    """The extractor contract as an OpenAPI document.

    Built from an application containing only these routes, so the paths and schemas are exactly
    the contract and nothing else. `openapi.json` and this document are therefore disjoint by
    construction rather than by a filter someone has to maintain.
    """
    document_app = FastAPI(
        title=_TITLE,
        summary=_SUMMARY,
        description=_DESCRIPTION,
        version=EXTRACTOR_API_VERSION,
        generate_unique_id_function=operation_id,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_exception_handlers(document_app)
    document_app.include_router(build_extractor_api_router(api_docs_enabled=True))
    return document_app.openapi()
