"""Liveness and readiness — outside `/api/v1`, unauthenticated by design.

Two of the four documented public endpoints (10-deployment-and-operations.md § health).
`/healthz` reveals nothing: no version, no internals. `/readyz` answers the question a
proxy actually asks — may this instance receive traffic — which is true only when the
database is reachable *and* the schema matches the running code.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from store_everything.db import migrations_are_current, ping
from store_everything.problems import problem_response
from store_everything.schemas import BaseSchema

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseSchema):
    status: Literal["ok"]


class ReadyResponse(BaseSchema):
    status: Literal["ready"]


@router.get(
    "/healthz",
    summary="Liveness probe",
    response_model=HealthResponse,
)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/readyz",
    summary="Readiness probe",
    response_model=ReadyResponse,
    responses={503: {"description": "Not ready to receive traffic"}},
)
async def readyz(request: Request) -> JSONResponse | ReadyResponse:
    engine = request.app.state.engine

    try:
        await ping(engine)
    except Exception:
        # The cause belongs in the log (correlated by request id), not in an
        # unauthenticated response body.
        _logger.warning("readiness failed: database unreachable", exc_info=True)
        return _not_ready("The database is unreachable.")

    try:
        current = await migrations_are_current(engine)
    except Exception:
        _logger.warning("readiness failed: schema version unreadable", exc_info=True)
        return _not_ready("The schema version could not be read.")

    if not current:
        _logger.warning("readiness failed: migrations pending")
        return _not_ready("Database migrations are pending.")

    return ReadyResponse(status="ready")


def _not_ready(detail: str) -> JSONResponse:
    return problem_response(
        status=503,
        slug="service-not-ready",
        title="Service not ready",
        detail=detail,
    )
