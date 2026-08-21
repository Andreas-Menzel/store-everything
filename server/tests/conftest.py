"""Shared fixtures.

Integration tests run against a **real** PostgreSQL in a throwaway container — no mocks
that lie (11-engineering-standards.md § test layers). Each such test gets its own freshly
created database so the suite stays order-independent.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from alembic import command
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from store_everything.app import create_app
from store_everything.config import Settings
from store_everything.db import alembic_config
from tests.identity_helpers import ADMIN_EMAIL, ADMIN_PASSWORD, BASE_URL

POSTGRES_IMAGE = os.environ.get("SE_TEST_POSTGRES_IMAGE", "pgvector/pgvector:pg18")
"""The datastore under test. Overridable so a PostgreSQL upgrade can be trialled in CI
before it becomes the pinned default in the compose stack."""

# Points at a port nothing listens on: used to prove the "database unreachable" path.
UNREACHABLE_DATABASE_URL = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/absent"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": UNREACHABLE_DATABASE_URL,
        "log_level": "CRITICAL",
        "app_env": "development",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def app() -> FastAPI:
    return create_app(make_settings())


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture
def fresh_database(postgres_url: str) -> Iterator[str]:
    """An empty database, dropped again afterwards."""
    name = f"test_{uuid4().hex[:12]}"
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()

    yield make_url(postgres_url).set(database=name).render_as_string(hide_password=False)

    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


# ------------------------------------------------------------------ identity fixtures
#
# A migrated database with exactly one bootstrapped administrator: the state a freshly
# installed instance is in. Tests that need accounts therefore exercise the real first-run
# path rather than rows inserted behind the app's back.


@pytest.fixture
def identity_database(fresh_database: str) -> str:
    command.upgrade(alembic_config(fresh_database), "head")
    return fresh_database


@pytest.fixture
def identity_settings(identity_database: str) -> Settings:
    return make_settings(
        database_url=identity_database,
        bootstrap_admin_email=ADMIN_EMAIL,
        bootstrap_admin_password=ADMIN_PASSWORD,
        # The test client speaks plain HTTP, and a `__Host-` cookie without `Secure` is
        # invalid — the same reason a development instance sets this (config.py).
        session_cookie_secure=False,
        # Low enough to reach the lockout in a test without hammering argon2.
        login_max_attempts=3,
        rate_limit_per_minute=500,
    )


@pytest_asyncio.fixture
async def identity_app(identity_settings: Settings) -> AsyncIterator[FastAPI]:
    """The app with its lifespan actually run, so start-up bootstrap happens for real."""
    app = create_app(identity_settings)
    async with app.router.lifespan_context(app):
        yield app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


@pytest_asyncio.fixture
async def identity_client(identity_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(identity_app) as client:
        yield client


@pytest_asyncio.fixture
async def identity_app_client(identity_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A second client against the same instance — its own cookie jar, like another device."""
    async with _client(identity_app) as client:
        yield client
