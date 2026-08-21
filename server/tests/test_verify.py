"""The audit: does it find the things nothing else can, and stay quiet otherwise?

An audit that reports findings on a healthy instance trains people to ignore it, so "clean is
clean" matters as much as detection.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from store_everything import filestore, operations, verify
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.tables import operation
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LONG_AGO = time.time() - timedelta(days=7).total_seconds()


@pytest_asyncio.fixture
async def engine(identity_database: str) -> AsyncIterator[AsyncEngine]:
    made = create_async_engine(identity_database)
    try:
        yield made
    finally:
        await made.dispose()


def settings_for(database_url: str, root: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"database_url": database_url, "app_data_root": root}
    values.update(overrides)
    return make_settings(**values)


async def audit(engine: AsyncEngine, settings: Settings) -> verify.Report:
    async with engine.connect() as connection:
        return await verify.audit(connection, settings=settings)


async def test_a_healthy_instance_audits_clean(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    report = await audit(engine, settings_for(identity_database, tmp_path))

    assert report.clean, report.render()
    assert "clean" in report.render()
    # It says what it checked, so a clean result is not mistaken for "nothing ran".
    assert len(report.checks) == 3


async def test_a_healthy_instance_with_data_audits_clean(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    for index in range(3):
        store.put_bytes(f"version {index}".encode(), operation_id=uuid4())

    report = await audit(engine, settings)

    assert report.clean, report.render()
    assert report.blobs_read == 3


async def test_uncollected_debris_is_reported(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Debris past the grace window means the janitor is not running."""
    settings = settings_for(identity_database, tmp_path)
    staging = settings.versions_root / "staging"
    staging.mkdir(parents=True)
    stale = filestore.staging_path(staging, uuid4())
    stale.write_bytes(b"left behind")
    os.utime(stale, (LONG_AGO, LONG_AGO))

    report = await audit(engine, settings)

    assert not report.clean
    assert [finding.check for finding in report.findings] == ["uncollected-debris"]
    assert "janitor" in report.findings[0].detail


async def test_debris_inside_the_grace_window_is_not_reported(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """A file written a moment ago is normal, not a finding."""
    settings = settings_for(identity_database, tmp_path)
    staging = settings.versions_root / "staging"
    staging.mkdir(parents=True)
    filestore.staging_path(staging, uuid4()).write_bytes(b"in flight")

    assert (await audit(engine, settings)).clean


async def test_a_stuck_operation_is_reported(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Running, lease long gone, nobody claiming it: a worker that is not there."""
    # The heartbeat has to stay shorter than the lease, which the settings enforce.
    settings = settings_for(identity_database, tmp_path, lease_seconds=60, heartbeat_seconds=30)
    async with engine.connect() as connection:
        await operations.enqueue(connection, kind="test.work", max_attempts=3)
        await connection.commit()
        claimed = await operations.claim(
            connection, worker="ghost/1", lease=timedelta(minutes=1), kinds=("test.work",)
        )
        assert claimed is not None
        await connection.execute(
            update(operation)
            .where(operation.c.id == claimed.id)
            .values(lease_expires_at=text("now() - interval '3 hours'"))
        )
        await connection.commit()

    report = await audit(engine, settings)

    assert [finding.check for finding in report.findings] == ["stuck-operations"]
    assert "no worker is claiming it" in report.findings[0].detail


async def test_a_recently_expired_lease_is_not_reported(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """A lease that just lapsed is the reclaim path working, not a fault."""
    settings = settings_for(identity_database, tmp_path, lease_seconds=300)
    async with engine.connect() as connection:
        await operations.enqueue(connection, kind="test.work", max_attempts=3)
        await connection.commit()
        claimed = await operations.claim(
            connection, worker="w/1", lease=timedelta(minutes=5), kinds=("test.work",)
        )
        assert claimed is not None
        await connection.execute(
            update(operation)
            .where(operation.c.id == claimed.id)
            .values(lease_expires_at=text("now() - interval '1 minute'"))
        )
        await connection.commit()

    assert (await audit(engine, settings)).clean


async def test_bit_rot_is_reported(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Nothing on the write path can catch this: the bytes changed underneath us."""
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    digest = store.put_bytes(b"the original version", operation_id=uuid4())
    store.path_for(digest).write_bytes(b"silently corrupted")

    report = await audit(engine, settings)

    assert [finding.check for finding in report.findings] == ["blob-integrity"]
    assert report.findings[0].subject == digest


async def test_the_blob_sample_is_bounded(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Reading every blob means reading the whole store; the audit must stay runnable."""
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    for index in range(10):
        store.put_bytes(f"blob {index}".encode(), operation_id=uuid4())

    async with engine.connect() as connection:
        report = await verify.audit(connection, settings=settings, blob_sample=4)

    assert report.blobs_read == 4


async def test_findings_render_with_their_subject(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """An operator has to be able to act on the output without reading the source."""
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    digest = store.put_bytes(b"content", operation_id=uuid4())
    store.path_for(digest).write_bytes(b"rotten")

    rendered = (await audit(engine, settings)).render()

    assert digest in rendered
    assert "finding(s)" in rendered
