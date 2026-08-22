"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncEngine

from store_everything import bootstrap, web
from store_everything.api import health
from store_everything.api.v1.router import build_public_v1_router, build_v1_router
from store_everything.config import Settings, load_settings
from store_everything.db import create_engine
from store_everything.log import configure_logging
from store_everything.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from store_everything.problems import install_exception_handlers
from store_everything.ratelimit import RequestLimiter

_logger = logging.getLogger(__name__)

_SUMMARY = "Self-hosted personal cloud where search is the product."

#: The contract's version, deliberately independent of the app's SemVer and of the
#: extractor contract (08-api-principles.md § stable versioning). The API is
#: path-versioned, so this changes only when a /v2 appears — never on a release, which
#: would otherwise churn openapi.json and every generated client on every bump.
API_VERSION = "1"


def _operation_id(route: APIRoute) -> str:
    """Name operations after their handler.

    FastAPI's default (`healthz_healthz_get`) becomes the function name in every
    generated client, so the contract fixes readable ids instead. Uniqueness is asserted
    by the test suite, because a collision would produce an invalid document.
    """
    return route.name


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings: Settings = app.state.settings
    _logger.info("service starting", extra={"app_env": settings.app_env})
    # Start-up is migrations plus starting the loops (12-reliability.md § startup); this is
    # the one exception, and it is idempotent and non-fatal by construction.
    await bootstrap.run_at_startup(app.state.engine, settings)
    try:
        yield
    finally:
        engine: AsyncEngine = app.state.engine
        await engine.dispose()
        _logger.info("service stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings if settings is not None else load_settings()
    configure_logging(resolved.log_level)

    app = FastAPI(
        title="Store Everything",
        summary=_SUMMARY,
        version=API_VERSION,
        lifespan=_lifespan,
        generate_unique_id_function=_operation_id,
        # The built-in docs routes are public; ours are mounted under /api/v1 behind
        # authentication instead (08-api-principles.md).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.engine = create_engine(resolved)
    # Per process, deliberately: these counters are a courtesy backstop, and losing them on
    # restart costs nothing. The durable limit — failed logins — is derived from the event
    # log instead (07 § abuse protection).
    app.state.request_limiter = RequestLimiter(resolved.rate_limit_per_minute)

    install_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(build_public_v1_router())
    app.include_router(build_v1_router(api_docs_enabled=resolved.api_docs_enabled))
    # Last, so its fallback route is reached only after every API route has declined
    # (F-027/FR-1).
    app.state.serves_web = web.install(app, resolved.web_root)

    # Last added is outermost: CORS → security headers → request context → routes.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    if resolved.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved.cors_allow_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    return app
