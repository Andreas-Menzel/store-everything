"""HTTP surface."""

from __future__ import annotations

from fastapi.routing import APIRoute

#: Where the versioned API lives. Here rather than in the router that mounts it, because the
#: routers *inside* it need to write URLs into their own responses — a preview descriptor names
#: the endpoint that serves each asset (F-028/FR-6) — and importing the router from a router it
#: includes is a cycle.
API_V1_PREFIX = "/api/v1"


def operation_id(route: APIRoute) -> str:
    """Name operations after their handler.

    FastAPI's default (`healthz_healthz_get`) becomes the function name in every generated
    client, so the contract fixes readable ids instead. Uniqueness is asserted by the test
    suite, because a collision would produce an invalid document.

    Lives here rather than in `app.py` because both published documents use it — the
    user-facing one and the extractor contract (ADR-0020) — and the module that builds the
    second one cannot import the application factory that mounts it.
    """
    return route.name
