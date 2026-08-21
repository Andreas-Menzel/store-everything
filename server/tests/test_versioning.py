"""Overwriting a file through the app: the half of versioning where nothing is ever lost.

[F-007/FR-9](../../features/F-007-versioning.md)'s "option b" has two sides, and this is the
good one. When the *app* mediates a change it holds the previous content in the app-owned
`versions/` area before writing, so the predecessor is genuinely restorable — as against a file
edited directly on the storage, where the bytes were gone before the app knew
([test_reconciliation.py](test_reconciliation.py) covers that side).

Three properties are asserted here that no unit test could reach:

- **The destination path never holds nothing.** The snapshot is a copy, not a move, because a
  scan interleaving with an upload would read an absent name as a deletion.
- **An overwrite of content the app has not seen is refused**, rather than losing the edit it
  never recorded (F-001/FR-20).
- **The janitor cannot collect a version's only copy**, because `files.restorable_digests` is
  the reference source the worker registers — asserted here against real rows rather than a
  fake, since forgetting that wiring would fail silently.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import files, janitor, operations, verify
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.runner import Job
from store_everything.tables import file_version
from tests.identity_helpers import read_events
from tests.upload_helpers import create_upload
from tests.workspace_helpers import workspace_ready

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIRST = b"the first draft of a document" * 8
SECOND = b"the second draft, which is longer" * 16


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def versions_of(database_url: str, file_id: UUID) -> list[dict[str, Any]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        file_version.c.content_hash,
                        file_version.c.size_bytes,
                        file_version.c.origin,
                        file_version.c.is_current,
                        file_version.c.restorable,
                    )
                    .where(file_version.c.file_id == file_id)
                    .order_by(file_version.c.created_at)
                )
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        await engine.dispose()


async def upload(
    client: Any, workspace: UUID, path: str, body: bytes, *, if_exists: str | None = None
) -> Any:
    return await create_upload(client, workspace, path, body=body, if_exists=if_exists)


# ---------------------------------------------------------------- the new-version path


@pytest.mark.fr("F-001/FR-7", "F-007/FR-1", "F-007/FR-2")
async def test_uploading_over_a_file_keeps_the_old_content_as_a_version(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The phase-1 exit criterion: overwriting through the app loses nothing."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await upload(client, workspace, "Docs/draft.txt", FIRST)
        assert created.status_code == 201, created.text
        file_id = UUID(created.json()["id"])

        replaced = await upload(
            client, workspace, "Docs/draft.txt", SECOND, if_exists="new_version"
        )
        assert replaced.status_code == 201, replaced.text
        assert replaced.json()["id"] == str(file_id), "an overwrite is not a new file"
        assert replaced.json()["content_hash"] == digest(SECOND)

        served = await client.get(f"{API_V1_PREFIX}/files/{file_id}/content")

    # The real file at the real path is the current version (03 § versioning).
    assert (root / "Docs/draft.txt").read_bytes() == SECOND
    assert served.status_code == 200 and served.content == SECOND

    history = await versions_of(identity_database, file_id)
    assert [row["content_hash"] for row in history] == [digest(FIRST), digest(SECOND)]
    assert [row["is_current"] for row in history] == [False, True]
    assert [row["origin"] for row in history] == ["upload", "upload"]
    # The whole point: the app mediated this change, so the predecessor is restorable.
    assert [row["restorable"] for row in history] == [True, True]

    # And its bytes really are in the app-owned area, byte for byte.
    store = BlobStore(identity_settings.versions_root)
    assert store.contains(digest(FIRST))
    assert store.open(digest(FIRST)).read_bytes() == FIRST
    # Never in the user's tree: browsing the workspace over SMB shows one file, not two.
    assert sorted(path.name for path in (root / "Docs").iterdir()) == ["draft.txt"]

    created_events = await read_events(identity_database, action="file.version_created")
    assert len(created_events) == 1
    assert created_events[0]["actor_type"] == "user"
    assert created_events[0]["details"]["predecessor_restorable"] is True
    assert created_events[0]["details"]["path"] == "Docs/draft.txt"


@pytest.mark.fr("F-001/FR-7")
async def test_uploading_over_a_file_is_refused_by_default(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Overwriting is a decision the client has to make explicitly."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        first = await upload(client, workspace, "notes.txt", FIRST)
        assert first.status_code == 201, first.text

        second = await upload(client, workspace, "notes.txt", SECOND)

    assert second.status_code == 409
    assert (root / "notes.txt").read_bytes() == FIRST, "the refusal left the file alone"


async def test_new_version_on_a_free_path_is_an_ordinary_upload(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Asking for a new version where nothing exists yet registers the file, not an error."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await upload(client, workspace, "fresh.txt", FIRST, if_exists="new_version")

    assert created.status_code == 201, created.text
    assert (root / "fresh.txt").read_bytes() == FIRST
    assert len(await versions_of(identity_database, UUID(created.json()["id"]))) == 1


async def test_a_folder_in_the_way_is_still_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """No `if_exists` mode can turn a directory into a version of a file."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        await upload(client, workspace, "Docs/draft.txt", FIRST)

        blocked = await upload(client, workspace, "Docs", SECOND, if_exists="new_version")

    assert blocked.status_code == 409


@pytest.mark.fr("F-001/FR-20")
async def test_an_overwrite_of_content_the_app_has_not_seen_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The lost-update case, and the one place this path says no.

    Someone edited the file on the storage and no scan has recorded it yet. Overwriting now
    would destroy that edit with nothing to show for it — no version row can describe content
    the app never hashed — so the upload is refused and a re-scan is told to catch up first.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await upload(client, workspace, "notes.txt", FIRST)
        assert created.status_code == 201, created.text
        file_id = UUID(created.json()["id"])

        (root / "notes.txt").write_bytes(b"edited on the NAS behind the app's back")
        refused = await upload(client, workspace, "notes.txt", SECOND, if_exists="new_version")

    assert refused.status_code == 409
    assert "changed on the storage" in refused.json()["detail"]
    # Nothing was overwritten and nothing was recorded: the edit is still there to be found.
    assert (root / "notes.txt").read_bytes() == b"edited on the NAS behind the app's back"
    assert len(await versions_of(identity_database, file_id)) == 1


async def test_an_overwrite_of_content_that_vanished_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Deleted on the storage, not yet reconciled: there is nothing to keep as a version."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await upload(client, workspace, "notes.txt", FIRST)
        assert created.status_code == 201, created.text

        (root / "notes.txt").unlink()
        refused = await upload(client, workspace, "notes.txt", SECOND, if_exists="new_version")

    assert refused.status_code == 409
    assert "no longer on the storage" in refused.json()["detail"]


@pytest.mark.fr("F-014/FR-10")
async def test_re_uploading_deleted_content_reactivates_its_file(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """FR-10's other half: the same content at the same path, this time through the API.

    The row comes back rather than a second identity being created, which is what makes tags
    and history survive a file someone deleted on the NAS and then re-copied.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await upload(client, workspace, "notes.txt", FIRST)
        file_id = UUID(created.json()["id"])

        # Trash it the way phase 1 can: remove it on the storage and let a scan notice.
        (root / "notes.txt").unlink()
        engine = create_async_engine(identity_database)
        try:
            async with engine.connect() as connection:
                found = await files.get(connection, file_id)
                assert found is not None
                from store_everything import trash
                from store_everything.events import Actor

                await trash.record(
                    connection,
                    found=found,
                    path="notes.txt",
                    origin="detected_on_disk",
                    batch_id=uuid4(),
                    actor=Actor.system(),
                    restorable=False,
                )
                await connection.commit()
        finally:
            await engine.dispose()

        again = await upload(client, workspace, "notes.txt", FIRST)

    assert again.status_code == 201, again.text
    assert again.json()["id"] == str(file_id), "the original row did not come back"
    assert again.json()["state"] == "live"
    assert again.json()["trash"] is None
    assert len(await versions_of(identity_database, file_id)) == 1, "no version was added"


# --------------------------------------------------------- what protects the only copy


async def test_the_janitor_never_collects_a_version_the_app_owes(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """`versions/` holds the only copy of a superseded original, against real rows.

    The reference source is registered in `handlers.registry`; forgetting it would make this
    blob collectable and nothing else would notice. Aged past the grace window on purpose, so
    only its being *referenced* can save it.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await upload(client, workspace, "notes.txt", FIRST)
        assert created.status_code == 201, created.text
        replaced = await upload(client, workspace, "notes.txt", SECOND, if_exists="new_version")
        assert replaced.status_code == 201, replaced.text

    store = BlobStore(identity_settings.versions_root)
    snapshot = store.path_for(digest(FIRST))
    age = timedelta(days=7).total_seconds()
    stat = snapshot.stat()
    import os

    os.utime(snapshot, (stat.st_atime - age, stat.st_mtime - age))

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            await operations.enqueue(connection, kind=janitor.KIND, max_attempts=3)
            await connection.commit()
            claimed = await operations.claim(
                connection,
                worker="janitor/1",
                lease=timedelta(minutes=5),
                kinds=(janitor.KIND,),
            )
            assert claimed is not None
            result = await janitor.collect(
                Job(operation=claimed, connection=connection),
                settings=identity_settings,
                references=files.restorable_digests,
            )
            await connection.commit()

            report = await verify.audit(connection, settings=identity_settings)
    finally:
        await engine.dispose()

    assert store.contains(digest(FIRST)), "the only copy of a superseded version was collected"
    assert result["blobs_collected"] == 0
    assert report.clean, report.render()


async def test_verify_reports_a_restorable_version_whose_blob_is_missing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """The audit that would catch the bug the test above rules out.

    A restore that fails when someone finally tries it is the worst kind of data loss: silent
    for years. So the audit asserts the pairing rather than trusting the write path.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        await upload(client, workspace, "notes.txt", FIRST)
        await upload(client, workspace, "notes.txt", SECOND, if_exists="new_version")

    store = BlobStore(identity_settings.versions_root)
    assert store.remove(digest(FIRST))

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            report = await verify.audit(connection, settings=identity_settings)
    finally:
        await engine.dispose()

    assert not report.clean
    assert [finding.check for finding in report.findings] == ["version-snapshots"]
    assert digest(FIRST) in report.findings[0].detail
