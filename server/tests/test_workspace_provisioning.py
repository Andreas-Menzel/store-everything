"""Provisioning: the operation that turns a requested workspace into a usable one.

Creation is deliberately two transactions (ADR-0010) — the request records the intent, the
operation makes it true — so this is where the crash-only properties get asserted: every step
idempotent, a failure retried rather than half-applied, and no state that says "usable" before
the directory exists.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from store_everything import folders, fscheck, janitor, operations, workspacefs, workspaces
from store_everything.config import Settings
from store_everything.events import Actor
from store_everything.runner import Job, PermanentFailureError, Runner
from store_everything.tables import folder, folder_closure, workspace
from tests.identity_helpers import read_events
from tests.workspace_helpers import (
    as_admin,
    create_workspace,
    provision_pending,
    provisioning_states,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def engine(identity_database: str) -> AsyncIterator[AsyncEngine]:
    made = create_async_engine(identity_database)
    try:
        yield made
    finally:
        await made.dispose()


async def request_workspace(settings: Settings, name: str = "Photos") -> tuple[UUID, Path]:
    """Ask for a managed workspace and return its id and root, without provisioning it."""
    async with as_admin(settings) as admin:
        response = await create_workspace(admin, name)
    assert response.status_code == 201, response.text
    body = response.json()
    return UUID(body["id"]), Path(body["root_path"])


async def count(engine: AsyncEngine, table: Any) -> int:
    async with engine.connect() as connection:
        return (await connection.execute(select(func.count()).select_from(table))).scalar_one()


async def state_of(engine: AsyncEngine, workspace_id: UUID) -> str:
    async with engine.connect() as connection:
        found = await workspaces.get(connection, workspace_id)
        assert found is not None
        return found.state


# ------------------------------------------------------------------------ the happy path


async def test_the_request_records_the_intent_without_touching_the_filesystem(
    identity_settings: Settings,
) -> None:
    """Durable intent before side effects: nothing on disk yet, and a row that says so."""
    _, root = await request_workspace(identity_settings)

    assert not root.exists()


async def test_provisioning_builds_the_root_the_control_directory_and_the_root_folder(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    workspace_id, root = await request_workspace(identity_settings)

    results = await provision_pending(identity_database)

    assert [result["outcome"] for result in results] == ["provisioned"]
    assert root.is_dir()
    assert workspacefs.staging_directory(root).is_dir()
    marker = workspacefs.read_marker(root)
    assert marker is not None and marker.workspace_id == workspace_id
    assert marker.placement == "managed"

    assert await state_of(engine, workspace_id) == "active"
    async with engine.connect() as connection:
        root_folder = await folders.root_of(connection, workspace_id)
    assert root_folder is not None
    assert root_folder.is_root
    assert root_folder.name == ""
    assert root_folder.depth == 0

    provisioned = await read_events(identity_database, action="workspace.provisioned")
    assert len(provisioned) == 1
    assert provisioned[0]["actor_type"] == "system"
    created_folders = await read_events(identity_database, action="folder.created")
    assert [event["details"]["role"] for event in created_folders] == ["root"]


async def test_the_root_folder_carries_its_own_closure_row(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """Depth 0 to itself, so "everything under F" needs no special case for F."""
    workspace_id, _ = await request_workspace(identity_settings)
    await provision_pending(identity_database)

    async with engine.connect() as connection:
        root_folder = await folders.root_of(connection, workspace_id)
        assert root_folder is not None
        rows = (
            await connection.execute(
                select(folder_closure.c.ancestor_id, folder_closure.c.depth).where(
                    folder_closure.c.descendant_id == root_folder.id
                )
            )
        ).all()

    assert [tuple(row) for row in rows] == [(root_folder.id, 0)]


async def test_the_recorded_verdict_describes_the_workspaces_own_root(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """The pre-flight probes `SE_DATA_ROOT`; the run replaces that with the real root."""
    workspace_id, root = await request_workspace(identity_settings)
    await provision_pending(identity_database)

    async with engine.connect() as connection:
        found = await workspaces.get(connection, workspace_id)

    assert found is not None
    assert found.fs_check["root"] == str(root)
    assert found.fs_check["usable"] is True
    # The facts an operator needs when a scan later reports a collision nobody can see.
    assert "case_sensitivity" in found.fs_check["facts"]


# ---------------------------------------------------------------------------- idempotency


async def test_provisioning_twice_changes_nothing(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """A crash after the effects but before the transition replays the whole operation."""
    workspace_id, _ = await request_workspace(identity_settings)
    await provision_pending(identity_database)

    async with engine.connect() as connection:
        await operations.enqueue(
            connection,
            kind=workspaces.KIND,
            max_attempts=3,
            subject_type="workspace",
            subject_id=workspace_id,
        )
        await connection.commit()
    again = await provision_pending(identity_database)

    assert [result["outcome"] for result in again] == ["already-active"]
    assert await count(engine, folder) == 1
    assert len(await read_events(identity_database, action="workspace.provisioned")) == 1


async def test_a_replayed_run_converges_on_one_root_folder(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """The half-done case: the effects landed, the activation did not."""
    workspace_id, _ = await request_workspace(identity_settings)
    await provision_pending(identity_database)
    # Rewind the state the transition set, leaving the folder and the directory in place.
    async with engine.connect() as connection:
        await connection.execute(
            update(workspace).where(workspace.c.id == workspace_id).values(state="provisioning")
        )
        await operations.enqueue(
            connection,
            kind=workspaces.KIND,
            max_attempts=3,
            subject_type="workspace",
            subject_id=workspace_id,
        )
        await connection.commit()

    results = await provision_pending(identity_database)

    assert [result["outcome"] for result in results] == ["provisioned"]
    assert await count(engine, folder) == 1
    assert await count(engine, folder_closure) == 1
    assert await state_of(engine, workspace_id) == "active"


# -------------------------------------------------------------------------- failure paths


async def test_a_vanished_root_is_retried_rather_than_marked_usable(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine, tmp_path: Path
) -> None:
    """An unmounted share is the world changing under us, not a permanent defect."""
    allowed = tmp_path / "nas"
    allowed.mkdir()
    async with as_admin(identity_settings, adoption_roots=(allowed,)) as admin:
        response = await create_workspace(admin, "The NAS", adopt_path=allowed)
    workspace_id = UUID(response.json()["id"])
    shutil.rmtree(allowed)

    runner = Runner(
        engine, identity_settings, {workspaces.KIND: workspaces.provision}, worker="test/1"
    )
    assert await runner.run_once() is True

    assert await state_of(engine, workspace_id) == "provisioning"
    # Back in the queue for another attempt, not dead-lettered and not "active".
    assert await provisioning_states(identity_database, workspace_id) == ["queued"]


async def test_a_workspace_that_no_longer_exists_is_a_no_op(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    workspace_id, _ = await request_workspace(identity_settings)
    async with engine.connect() as connection:
        await connection.execute(delete(workspace).where(workspace.c.id == workspace_id))
        await connection.commit()

    results = await provision_pending(identity_database)

    assert [result["outcome"] for result in results] == ["gone"]


async def test_activating_an_already_active_workspace_changes_nothing(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """The transition is guarded, so a reclaimed lease cannot announce a workspace twice."""
    workspace_id, _ = await request_workspace(identity_settings)
    await provision_pending(identity_database)

    async with engine.connect() as connection:
        found = await workspaces.get(connection, workspace_id)
        assert found is not None
        again = await workspaces.mark_active(
            connection,
            workspace_id=workspace_id,
            verdict=fscheck.Verdict(root=found.root_path),
            actor=Actor.system(),
        )
        await connection.commit()

    assert again is None
    assert len(await read_events(identity_database, action="workspace.provisioned")) == 1


async def test_an_operation_without_a_subject_fails_permanently(engine: AsyncEngine) -> None:
    """Retrying a malformed payload cannot help, so it must not consume the attempt budget."""
    async with engine.connect() as connection:
        queued = await operations.enqueue(connection, kind=workspaces.KIND, max_attempts=3)
        await connection.commit()
        claimed = await operations.claim(
            connection,
            worker="test/1",
            lease=timedelta(minutes=5),
            kinds=(workspaces.KIND,),
        )
        assert claimed is not None and claimed.id == queued.id

        with pytest.raises(PermanentFailureError):
            await workspaces.provision(Job(operation=claimed, connection=connection))


# ------------------------------------------------------------------- the janitor's reach


async def test_the_janitor_reaches_a_workspaces_own_staging_area(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """Staging lives in the user's tree, so nobody but the database knows where to sweep."""
    _, root = await request_workspace(identity_settings)
    await provision_pending(identity_database)

    async with engine.connect() as connection:
        roots = await janitor.all_staging_roots(connection, identity_settings)

    assert workspacefs.staging_directory(root) in roots


async def test_a_folder_cannot_be_orphaned_from_its_workspace(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """Containment is structural: removing a workspace takes its folders with it."""
    workspace_id, _ = await request_workspace(identity_settings)
    await provision_pending(identity_database)

    async with engine.connect() as connection:
        await connection.execute(delete(workspace).where(workspace.c.id == workspace_id))
        await connection.commit()

    assert await count(engine, folder) == 0
    assert await count(engine, folder_closure) == 0


async def test_a_second_root_folder_is_refused_by_the_database(
    identity_settings: Settings, identity_database: str, engine: AsyncEngine
) -> None:
    """`create_root` converges instead of adding one, and the index is why it can."""
    workspace_id, _ = await request_workspace(identity_settings)
    await provision_pending(identity_database)

    async with engine.connect() as connection:
        second, created = await folders.create_root(
            connection, workspace_id=workspace_id, actor=Actor.system()
        )
        await connection.commit()

    assert created is False
    async with engine.connect() as connection:
        existing = await folders.root_of(connection, workspace_id)
    assert existing is not None and existing.id == second.id
    assert await count(engine, folder) == 1


async def test_an_unrelated_uuid_has_no_root_folder(
    identity_settings: Settings, engine: AsyncEngine
) -> None:
    async with engine.connect() as connection:
        assert await folders.root_of(connection, uuid4()) is None
