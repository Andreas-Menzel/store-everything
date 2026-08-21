"""Creating and reading workspaces through the API.

The interesting half is what gets refused. Adoption points the app at a directory a user
already owns, so the checks around it — admin only, allow-listed, non-overlapping, probed —
are the security boundary of this feature, and each one is asserted from the failing side
([F-001/FR-10](../../features/F-001-upload-and-import.md), AC-5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import fscheck, names
from store_everything.config import Settings
from store_everything.problems import problem_type
from store_everything.tables import workspace
from tests.identity_helpers import read_events
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    WORKSPACES,
    as_admin,
    create_member,
    create_workspace,
    instance,
    provision_pending,
    provisioning_states,
    signed_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def tree_with_files(root: Path) -> dict[str, bytes]:
    """A small existing tree, the way a user's NAS folder arrives."""
    root.mkdir(parents=True, exist_ok=True)
    contents = {
        "notes.txt": b"a plain file",
        "holiday/beach.jpg": b"pretend this is a photo",
        "holiday/raw/DSC0001.ARW": b"pretend this is raw",
    }
    for relative, payload in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return contents


async def count_workspaces(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (
                await connection.execute(select(func.count()).select_from(workspace))
            ).scalar_one()
    finally:
        await engine.dispose()


# ------------------------------------------------------------------- managed placement


async def test_a_member_creates_a_managed_workspace(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await create_member(admin)
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            response = await create_workspace(member, "Photos")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["placement"] == "managed"
    assert body["source"] == "local"
    # Not usable yet: the directory is built by the operation this request enqueued.
    assert body["state"] == "provisioning"
    assert body["filesystem"]["usable"] is True
    # The path is ours to shape, and carries the name so the tree is recognisable on disk.
    assert body["root_path"].startswith(str(identity_settings.data_root))
    assert body["root_path"].endswith("/workspaces/Photos/data")

    created = await read_events(identity_database, action="workspace.created")
    assert len(created) == 1
    assert created[0]["actor_type"] == "user"
    assert created[0]["details"]["placement"] == "managed"
    # The intent to build it commits with the row.
    assert await provisioning_states(identity_database, UUID(body["id"])) == ["queued"]


async def test_a_name_colliding_only_in_case_is_refused(identity_settings: Settings) -> None:
    """`Photos` and `photos` cannot be one owner's two workspaces: the name is a path segment."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        first = await create_workspace(admin, "Photos")
        second = await create_workspace(admin, "photos")

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["type"] == problem_type("conflict")
    assert second.json()["errors"][0]["pointer"] == "/body/name"


async def test_two_users_may_use_the_same_name(identity_settings: Settings) -> None:
    """Uniqueness is per owner; the paths differ by owner, so nothing collides on disk."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await create_member(admin)
        mine = await create_workspace(admin, "Photos")
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            theirs = await create_workspace(member, "Photos")

    assert mine.status_code == 201
    assert theirs.status_code == 201
    assert mine.json()["root_path"] != theirs.json()["root_path"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("with/slash", "'/'"),
        ("..", "names"),
        # 200 two-byte characters: inside a 255-character limit, outside the 255-byte one.
        ("é" * 200, "255 bytes"),
    ],
)
async def test_a_name_breaking_the_policy_is_refused(
    identity_settings: Settings, name: str, expected: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        response = await create_workspace(admin, name)

    assert response.status_code == 422
    problem = response.json()
    assert problem["errors"][0]["pointer"] == "/body/name"
    assert expected in problem["errors"][0]["detail"]


async def test_a_name_is_stored_as_typed_but_compared_normalized(
    identity_settings: Settings,
) -> None:
    """Stored as given, unique on the key: the NFD spelling of an NFC name is a collision."""
    decomposed = "Café"
    async with instance(identity_settings) as app, signed_in(app) as admin:
        created = await create_workspace(admin, decomposed)
        again = await create_workspace(admin, "Café")

    assert created.status_code == 201
    # Normalized on the way in (03 § names on disk), so what is stored is one spelling.
    assert created.json()["name"] == names.normalize_api_name(decomposed)
    assert again.status_code == 409


# ------------------------------------------------------------------- adopted placement


@pytest.mark.fr("F-001/FR-10")
async def test_a_member_cannot_adopt_any_path(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Not "a member may adopt inside the allow-list": a member may not adopt at all."""
    allowed = tmp_path / "nas"
    tree_with_files(allowed)

    async with instance(identity_settings, adoption_roots=(allowed,)) as app:
        async with signed_in(app) as admin:
            await create_member(admin)
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            response = await create_workspace(member, "Adopted", adopt_path=allowed)

    assert response.status_code == 403
    assert response.json()["type"] == problem_type("admin-required")
    assert await count_workspaces(identity_database) == 0


@pytest.mark.fr("F-001/FR-10")
async def test_an_admin_adopting_an_allow_listed_tree_copies_and_renames_nothing(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """AC-5's positive half: indexed in place, zero bytes copied, zero entries renamed."""
    allowed = tmp_path / "nas"
    contents = tree_with_files(allowed)

    async with as_admin(identity_settings, adoption_roots=(allowed,)) as admin:
        response = await create_workspace(admin, "The NAS", adopt_path=allowed)
    assert response.status_code == 201, response.text
    assert response.json()["placement"] == "adopted"
    assert response.json()["root_path"] == str(allowed.resolve())

    await provision_pending(identity_database)

    # Every original file is exactly where it was, byte for byte.
    for relative, payload in contents.items():
        assert (allowed / relative).read_bytes() == payload
    # And the only thing that appeared is the one directory we are allowed to add.
    assert {path.name for path in allowed.iterdir()} == {
        "notes.txt",
        "holiday",
        names.CONTROL_DIRECTORY,
    }


@pytest.mark.fr("F-001/FR-10")
async def test_adoption_outside_the_allow_list_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    allowed = tmp_path / "nas"
    allowed.mkdir()
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()

    async with as_admin(identity_settings, adoption_roots=(allowed,)) as admin:
        response = await create_workspace(admin, "Elsewhere", adopt_path=elsewhere)

    assert response.status_code == 422
    assert "SE_ADOPTION_ROOTS" in response.json()["errors"][0]["detail"]
    assert response.json()["errors"][0]["pointer"] == "/body/adopt_path"
    assert await count_workspaces(identity_database) == 0


@pytest.mark.fr("F-001/FR-10")
async def test_adoption_is_refused_outright_when_no_root_is_allow_listed(
    identity_settings: Settings, tmp_path: Path
) -> None:
    """Empty allow-list is the default, and it means "no", not "anywhere"."""
    tree = tmp_path / "nas"
    tree.mkdir()

    async with instance(identity_settings) as app, signed_in(app) as admin:
        response = await create_workspace(admin, "The NAS", adopt_path=tree)

    assert response.status_code == 422
    assert "disabled" in response.json()["errors"][0]["detail"]


@pytest.mark.fr("F-001/FR-10")
async def test_a_symlink_out_of_the_allow_list_is_refused(
    identity_settings: Settings, tmp_path: Path
) -> None:
    """Lexical containment is not containment — the File Browser CVE, as a test."""
    allowed = tmp_path / "nas"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (allowed / "escape").symlink_to(outside, target_is_directory=True)

    async with as_admin(identity_settings, adoption_roots=(allowed,)) as admin:
        response = await create_workspace(admin, "Escape", adopt_path=allowed / "escape")

    assert response.status_code == 422
    assert "SE_ADOPTION_ROOTS" in response.json()["errors"][0]["detail"]


@pytest.mark.fr("F-001/FR-10")
async def test_an_overlapping_root_is_refused(identity_settings: Settings, tmp_path: Path) -> None:
    """Two workspaces over one tree would each reconcile the other's files away."""
    allowed = tmp_path / "nas"
    (allowed / "holiday").mkdir(parents=True)

    async with as_admin(identity_settings, adoption_roots=(allowed,)) as admin:
        first = await create_workspace(admin, "Everything", adopt_path=allowed)
        inside = await create_workspace(admin, "Holiday", adopt_path=allowed / "holiday")
        same = await create_workspace(admin, "Again", adopt_path=allowed)

    assert first.status_code == 201
    assert inside.status_code == 409
    assert "already covers" in inside.json()["detail"]
    assert same.status_code == 409


@pytest.mark.fr("F-001/FR-10")
async def test_adopting_the_app_owned_area_is_refused(
    identity_settings: Settings, tmp_path: Path
) -> None:
    """`versions/` and the derived store stay outside every workspace root (ADR-0018)."""
    identity_settings.app_data_root.mkdir(parents=True, exist_ok=True)

    async with as_admin(
        identity_settings, adoption_roots=(identity_settings.app_data_root,)
    ) as admin:
        response = await create_workspace(
            admin, "App data", adopt_path=identity_settings.app_data_root
        )

    assert response.status_code == 422
    assert "SE_APP_DATA_ROOT" in response.json()["errors"][0]["detail"]


@pytest.mark.fr("F-001/FR-10")
async def test_a_filesystem_that_fails_the_probe_is_refused_naming_the_property(
    identity_settings: Settings, identity_database: str, tmp_path: Path, monkeypatch: Any
) -> None:
    """Monkeypatched on purpose: a real failure needs a filesystem that lies about `fsync`,
    which is exactly what nobody can arrange in CI — and what the probe exists to find. The
    probe's own behaviour is covered in `test_fscheck.py`; what matters here is that a
    refusal reaches the caller with the failing property in it."""
    allowed = tmp_path / "nas"
    allowed.mkdir()

    def unusable(root: Path) -> fscheck.Verdict:
        return fscheck.Verdict(
            root=root,
            properties=(fscheck.Property("directory-fsync", False, "Invalid argument"),),
        )

    monkeypatch.setattr(fscheck, "probe", unusable)

    async with as_admin(identity_settings, adoption_roots=(allowed,)) as admin:
        response = await create_workspace(admin, "Dodgy", adopt_path=allowed)

    assert response.status_code == 422
    assert "directory-fsync" in response.json()["errors"][0]["detail"]
    assert await count_workspaces(identity_database) == 0


# --------------------------------------------------------------------------- ownership


async def test_an_admin_may_create_a_workspace_for_a_member(
    identity_settings: Settings, tmp_path: Path
) -> None:
    """How an adopted tree reaches the member who will own it (ADR-0018)."""
    allowed = tmp_path / "nas"
    tree_with_files(allowed)

    async with as_admin(identity_settings, adoption_roots=(allowed,)) as admin:
        member_id = await create_member(admin)
        response = await create_workspace(admin, "Their NAS", adopt_path=allowed, owner=member_id)
        # And the admin, who is not the owner, cannot read it back.
        unauthorized = await admin.get(f"{WORKSPACES}/{response.json()['id']}")

    assert response.status_code == 201, response.text
    assert response.json()["owner"] == str(member_id)
    assert unauthorized.status_code == 404


async def test_a_member_cannot_create_a_workspace_for_someone_else(
    identity_settings: Settings,
) -> None:
    async with instance(identity_settings) as app:
        async with signed_in(app) as admin:
            await create_member(admin)
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            response = await create_workspace(member, "Not mine", owner=uuid4())

    assert response.status_code == 403


async def test_creating_for_an_unknown_account_is_refused(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        response = await create_workspace(admin, "Ghost", owner=uuid4())

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/body/owner"


async def test_only_the_owner_sees_a_workspace(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app:
        async with signed_in(app) as admin:
            await create_member(admin)
            mine = await create_workspace(admin, "Mine")
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            listed = await member.get(WORKSPACES)
            fetched = await member.get(f"{WORKSPACES}/{mine.json()['id']}")

    assert listed.status_code == 200
    assert listed.json()["data"] == []
    # 404, not 403: a distinguishable refusal would confirm which ids exist.
    assert fetched.status_code == 404


async def test_the_listing_pages_by_cursor(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        for name in ("First", "Second", "Third"):
            assert (await create_workspace(admin, name)).status_code == 201

        first_page = (await admin.get(WORKSPACES, params={"limit": 2})).json()
        second_page = (
            await admin.get(WORKSPACES, params={"limit": 2, "cursor": first_page["next_cursor"]})
        ).json()

    assert [item["name"] for item in first_page["data"]] == ["First", "Second"]
    assert [item["name"] for item in second_page["data"]] == ["Third"]
    assert second_page["next_cursor"] is None


async def test_an_unknown_workspace_is_not_found(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        response = await admin.get(f"{WORKSPACES}/{uuid4()}")

    assert response.status_code == 404
