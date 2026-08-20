"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncEngine

from store_everything import __version__
from store_everything.api import health
from store_everything.api.v1.router import build_v1_router
from store_everything.config import Settings, load_settings
from store_everything.db import create_engine
from store_everything.log import configure_logging
from store_everything.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from store_everything.problems import install_exception_handlers

_logger = logging.getLogger(__name__)

_SUMMARY = "Self-hosted personal cloud where search is the product."


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
        version=__version__,
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

    install_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(build_v1_router(api_docs_enabled=resolved.api_docs_enabled))

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
