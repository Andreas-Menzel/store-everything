"""Debris collection: does it collect what it should, and — more importantly — nothing else?

The dangerous direction is over-collection. A janitor that deletes a staging file an operation
is still writing, or a blob whose row has not committed yet, destroys data that the crash-only
model promised to keep. So most of these tests assert what survives.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from store_everything import filestore, janitor, operations
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.runner import Job
from tests.conftest import make_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Older than any grace window used here, so age is never the reason a test passes.
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


def stage(settings: Settings, operation_id: UUID, *, aged: bool = True) -> Path:
    """Put a staging file in the versions staging area, optionally back-dated."""
    root = settings.versions_root / "staging"
    root.mkdir(parents=True, exist_ok=True)
    path = filestore.staging_path(root, operation_id)
    path.write_bytes(b"partially written")
    if aged:
        os.utime(path, (LONG_AGO, LONG_AGO))
    return path


async def sweep(engine: AsyncEngine, settings: Settings, **kwargs: Any) -> dict[str, Any]:
    """Run the janitor as the worker would: a claimed operation on its own connection."""
    async with engine.connect() as connection:
        await operations.enqueue(connection, kind=janitor.KIND, max_attempts=3)
        await connection.commit()
        claimed = await operations.claim(
            connection, worker="janitor/1", lease=timedelta(minutes=5), kinds=(janitor.KIND,)
        )
        assert claimed is not None
        result = await janitor.collect(
            Job(operation=claimed, connection=connection), settings=settings, **kwargs
        )
        await operations.succeed(connection, claimed=claimed, result=result)
        await connection.commit()
    return result


async def finish(engine: AsyncEngine, operation_id: UUID) -> None:
    """Drive an operation to a terminal state, so its debris becomes collectable."""
    async with engine.connect() as connection:
        claimed = await operations.claim(
            connection, worker="w/1", lease=timedelta(minutes=5), kinds=("test.work",)
        )
        assert claimed is not None and claimed.id == operation_id
        await operations.succeed(connection, claimed=claimed)
        await connection.commit()


async def enqueue_work(engine: AsyncEngine) -> UUID:
    async with engine.connect() as connection:
        queued = await operations.enqueue(connection, kind="test.work", max_attempts=3)
        await connection.commit()
    return queued.id


# ------------------------------------------------------------------ what gets collected


async def test_debris_of_a_finished_operation_is_collected(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    settings = settings_for(identity_database, tmp_path)
    operation_id = await enqueue_work(engine)
    debris = stage(settings, operation_id)
    await finish(engine, operation_id)

    result = await sweep(engine, settings)

    assert not debris.exists()
    assert result["staging_collected"] == 1


async def test_debris_of_an_operation_that_no_longer_exists_is_collected(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Terminal rows are pruned (12 § queue hygiene), so absence counts as finished."""
    settings = settings_for(identity_database, tmp_path)
    debris = stage(settings, uuid4())

    await sweep(engine, settings)

    assert not debris.exists()


async def test_a_file_we_did_not_name_is_collected_on_age_alone(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    settings = settings_for(identity_database, tmp_path)
    root = settings.versions_root / "staging"
    root.mkdir(parents=True)
    stray = root / "not-a-uuid.partial"
    stray.write_bytes(b"from an older format, perhaps")
    os.utime(stray, (LONG_AGO, LONG_AGO))

    await sweep(engine, settings)

    assert not stray.exists()


# ------------------------------------------------------------------ what must survive


async def test_debris_of_a_running_operation_survives(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """The operation is still writing this file. Deleting it would destroy live work."""
    settings = settings_for(identity_database, tmp_path)
    operation_id = await enqueue_work(engine)
    in_flight = stage(settings, operation_id)
    async with engine.connect() as connection:
        await operations.claim(
            connection, worker="w/1", lease=timedelta(minutes=5), kinds=("test.work",)
        )
        await connection.commit()

    result = await sweep(engine, settings)

    assert in_flight.exists()
    assert result["staging_collected"] == 0


async def test_debris_of_a_queued_operation_survives(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """A retry will reuse this exact file — that is what deterministic staging paths are for."""
    settings = settings_for(identity_database, tmp_path)
    operation_id = await enqueue_work(engine)
    awaiting_retry = stage(settings, operation_id)

    await sweep(engine, settings)

    assert awaiting_retry.exists()


async def test_fresh_debris_survives_the_grace_window(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """The window exists so the janitor cannot race the gap between bytes and row."""
    settings = settings_for(identity_database, tmp_path)
    recent = stage(settings, uuid4(), aged=False)

    result = await sweep(engine, settings)

    assert recent.exists()
    assert result["staging_inspected"] == 1
    assert result["staging_collected"] == 0


async def test_a_finished_file_is_not_mistaken_for_debris(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Only staging entries are candidates; a committed blob is not debris."""
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    digest = store.put_bytes(b"a superseded version", operation_id=uuid4())
    os.utime(store.path_for(digest), (LONG_AGO, LONG_AGO))

    await sweep(engine, settings)

    assert store.contains(digest)


# ------------------------------------------------------------------ blob collection


async def test_blobs_are_never_collected_without_a_reference_source(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """The fail-safe that matters most: no reference list means skip, not delete everything.

    `versions/` holds the only copy of every superseded version. Collecting against an empty
    list would empty it.
    """
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    digest = store.put_bytes(b"the only copy of an old version", operation_id=uuid4())
    os.utime(store.path_for(digest), (LONG_AGO, LONG_AGO))

    result = await sweep(engine, settings)

    assert store.contains(digest)
    assert result["blobs_collected"] == 0


async def test_an_unreferenced_blob_is_collected_when_references_are_known(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    kept = store.put_bytes(b"still referenced", operation_id=uuid4())
    orphan = store.put_bytes(b"nothing points here", operation_id=uuid4())
    for digest in (kept, orphan):
        os.utime(store.path_for(digest), (LONG_AGO, LONG_AGO))

    def only_kept_is_referenced() -> list[str]:
        return [kept]

    result = await sweep(engine, settings, references=only_kept_is_referenced)

    assert store.contains(kept)
    assert not store.contains(orphan)
    assert result["blobs_collected"] == 1


async def test_a_fresh_unreferenced_blob_survives(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Its row may be committing right now — bytes are written before the row exists."""
    settings = settings_for(identity_database, tmp_path)
    store = BlobStore(settings.versions_root)
    just_written = store.put_bytes(b"row not committed yet", operation_id=uuid4())

    def nothing_is_referenced() -> list[str]:
        return []

    result = await sweep(engine, settings, references=nothing_is_referenced)

    assert store.contains(just_written)
    assert result["blobs_collected"] == 0


# ------------------------------------------------------------------ scheduling


async def test_the_janitor_re_arms_itself(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    settings = settings_for(identity_database, tmp_path)

    await sweep(engine, settings)

    async with engine.connect() as connection:
        assert await operations.count_by_state(connection, kind=janitor.KIND) == {
            "succeeded": 1,
            "queued": 1,
        }


async def test_sweeping_an_empty_instance_is_harmless(
    engine: AsyncEngine, identity_database: str, tmp_path: Path
) -> None:
    """Nothing on disk yet is the state of every fresh install."""
    result = await sweep(engine, settings_for(identity_database, tmp_path))

    assert result["staging_inspected"] == 0
    assert result["staging_collected"] == 0
