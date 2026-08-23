"""Tags on directories: the same vocabulary, none of the machinery.

[F-015/FR-9](../../features/F-015-folders.md). A folder tag is manual and self-only, and both
halves are deliberate: extractors never run on folders, so there is no confidence, no generation
and no state machine — and a tag on `2024/tax` describes the *directory*, not the files under it.
Inheritance to contents needs precedence rules nobody has written yet (how it displays on the
file, what it does to a rejected file tag, how facets count it), so v1 does not pretend to have
them (Q23).
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from store_everything import tags
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.events import Actor
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.tag_helpers import (
    TAGS,
    added,
    connected,
    folder_tags_url,
    names_on_file,
    tag_file,
    tag_folder,
)
from tests.upload_helpers import create_upload
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    create_member,
    create_workspace,
    instance,
    provision_pending,
    signed_in,
    workspace_ready,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _folder(client: httpx.AsyncClient, workspace: UUID | str, name: str) -> str:
    """A folder under the workspace root, returned as its id."""
    response = await client.post(
        f"{API_V1_PREFIX}/workspaces/{workspace}/folders",
        json={"name": name},
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.fr("F-015/FR-9")
async def test_a_folder_carries_tags_from_the_same_vocabulary(
    identity_settings: Settings, identity_database: str
) -> None:
    """Tagged by id or by name, stamped with who did it, and counted as folder usage."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        me = (await client.get(f"{API_V1_PREFIX}/auth/me")).json()["user"]["id"]
        tax = await added(client, "tax", aliases=["Steuer"])
        folder = await _folder(client, workspace, "2024")

        applied = await tag_folder(client, folder, tag=tax)
        by_alias = await tag_folder(client, folder, name="steuer")
        listed = await client.get(folder_tags_url(folder))
        counted = await client.get(TAGS, params={"prefix": "ta"})

    assert applied.status_code == 201, applied.text
    body = applied.json()
    assert body["name"] == "tax"
    assert body["provenance"] == "manual", "a folder tag is always a person's word"
    assert body["user"] == me
    # The synonym resolves to the same tag: applying it again is the row that is already there.
    assert by_alias.status_code == 201
    assert by_alias.json()["id"] == body["id"]
    assert [one["name"] for one in listed.json()] == ["tax"]
    assert counted.json()["data"][0]["usage"] == {"files": 0, "folders": 1}

    recorded = await read_events(identity_database, action="folder.tagged")
    assert len(recorded) == 1
    assert recorded[0]["details"]["tag"] == "tax"
    assert recorded[0]["resource_type"] == "folder"


@pytest.mark.fr("F-015/FR-9")
async def test_a_folder_tag_does_not_reach_the_files_inside_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """The self-only half of FR-9, and the reason it is its own test.

    Tagging a directory `tax` must not make every file in it a tax document — the file's own
    tags are the file's. The search half of this (a folder returned as a hit for `tag:tax` while
    its files are not) arrives with search in phase 3.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        tax = await added(client, "tax")
        folder = await _folder(client, workspace, "2024")
        created = await create_upload(client, workspace, "2024/receipt.pdf", body=b"pdf")
        assert created.status_code == 201, created.text
        await tag_folder(client, folder, tag=tax)

        on_file = await names_on_file(client, created.json()["id"])
        summary = await client.get(f"{API_V1_PREFIX}/files/{created.json()['id']}")

    assert on_file == []
    assert summary.json()["tags"] == []


@pytest.mark.fr("F-015/FR-9")
async def test_a_folder_tag_is_removed_and_audited(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        tax = await added(client, "tax")
        folder = await _folder(client, workspace, "2024")
        await tag_folder(client, folder, tag=tax)

        removed = await client.delete(f"{folder_tags_url(folder)}/{tax}", headers=SAME_ORIGIN)
        again = await client.delete(f"{folder_tags_url(folder)}/{tax}", headers=SAME_ORIGIN)
        listed = await client.get(folder_tags_url(folder))

    assert removed.status_code == 204, removed.text
    assert again.status_code == 404, "there is nothing left to remove"
    assert listed.json() == []
    assert len(await read_events(identity_database, action="folder.untagged")) == 1


@pytest.mark.fr("F-015/FR-9")
async def test_another_users_folder_cannot_be_read_or_tagged(
    identity_settings: Settings, identity_database: str
) -> None:
    """Tags are not a way around folder permissions: absent and forbidden are one answer."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        tax = await added(admin, "tax")
        await create_member(admin)
        mine = await create_workspace(admin, "Mine")
        await provision_pending(identity_database)
        folder = await _folder(admin, mine.json()["id"], "2024")
        await tag_folder(admin, folder, tag=tax)

        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            read = await member.get(folder_tags_url(folder))
            tagged = await tag_folder(member, folder, tag=tax)
            removed = await member.delete(f"{folder_tags_url(folder)}/{tax}", headers=SAME_ORIGIN)
        still_there = await admin.get(folder_tags_url(folder))

    assert read.status_code == 404, read.text
    assert tagged.status_code == 404
    assert removed.status_code == 404
    assert [one["name"] for one in still_there.json()] == ["tax"]


@pytest.mark.fr("F-015/FR-9")
async def test_a_word_that_is_not_vocabulary_is_refused_on_folders(
    identity_settings: Settings, identity_database: str
) -> None:
    """The same admin-governed vocabulary rule as for files — one taxonomy, two surfaces.

    Including the quarantine: a machine-suggested tag is not vocabulary, so it cannot be put on
    a folder either, however it got into the database.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        folder = await _folder(client, workspace, "2024")
        async with connected(identity_database) as connection:
            suggested = await tags.create(
                connection, name="wombat", actor=Actor.extractor(), status="suggested"
            )

        unknown = await tag_folder(client, folder, name="whatever")
        quarantined = await tag_folder(client, folder, tag=suggested.id)
        listed = await client.get(TAGS)

    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["errors"][0]["pointer"] == "/name"
    assert quarantined.status_code == 409, quarantined.text
    assert listed.json()["data"] == []


@pytest.mark.fr("F-015/FR-9")
async def test_a_folder_keeps_its_tags_when_it_moves(
    identity_settings: Settings, identity_database: str
) -> None:
    """Folder tags hang on the UUID, so a rename or a move carries them along (02 § folder)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        tax = await added(client, "tax")
        parent = await _folder(client, workspace, "Archive")
        folder = await _folder(client, workspace, "2024")
        await tag_folder(client, folder, tag=tax)
        # A rename and a move are the same operation (F-015/FR-4), so one call covers both.
        moved = await client.post(
            f"{API_V1_PREFIX}/folders/{folder}/move",
            json={"parent": parent, "name": "2024-tax"},
            headers=SAME_ORIGIN,
        )
        listed = await client.get(folder_tags_url(folder))
        # Tagging a file inside the file's own tags stay separate from the folder's.
        created = await create_upload(client, workspace, "Archive/2024-tax/one.pdf", body=b"pdf")
        await tag_file(client, created.json()["id"], tag=tax)
        on_file = await names_on_file(client, created.json()["id"])

    assert moved.status_code == 200, moved.text
    assert [one["name"] for one in listed.json()] == ["tax"]
    assert on_file == ["tax"]
