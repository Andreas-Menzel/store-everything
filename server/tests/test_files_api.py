"""Reading a file back: metadata, bytes, ranges — and the ways this endpoint must say no.

Serving content the user uploaded, from the app's own origin, is the classic way a personal
cloud grows an XSS hole. So the interesting assertions here are not "the bytes came back" but
"an HTML file came back as a download", "a symlink out of the workspace was refused", and "the
row is not trusted about what is on disk".
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
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

CONTENT = bytes(range(256)) * 8
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def content_url(file_id: UUID | str) -> str:
    return f"{API_V1_PREFIX}/files/{file_id}/content"


async def test_a_file_reports_what_it_is(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(
            client, workspace, "Photos/beach.jpg", body=CONTENT, content_type="image/jpeg"
        )
        assert created.status_code == 201, created.text
        response = await client.get(f"{API_V1_PREFIX}/files/{created.json()['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "Photos/beach.jpg"
    assert body["name"] == "beach.jpg"
    assert body["media_type"] == "image/jpeg"
    assert body["media_class"] == "image"
    assert body["content_hash"] == DIGEST
    assert body["state"] == "live"
    # The filesystem's own timestamp, which is what a later scan compares against.
    assert body["modified_at"] is not None


async def test_the_content_comes_back_byte_for_byte(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(
            client, workspace, "beach.jpg", body=CONTENT, content_type="image/jpeg"
        )
        response = await client.get(content_url(created.json()["id"]))

    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["etag"] == f'"{DIGEST}"'
    assert response.headers["cache-control"] == "private, no-cache"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["accept-ranges"] == "bytes"


async def test_a_range_request_is_served(
    identity_settings: Settings, identity_database: str
) -> None:
    """Streaming video and byte-range reads by extractors both depend on this (08 § downloads)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(client, workspace, "video.mp4", body=CONTENT)
        response = await client.get(
            content_url(created.json()["id"]), headers={"Range": "bytes=10-19"}
        )
        beyond = await client.get(
            content_url(created.json()["id"]),
            headers={"Range": f"bytes={len(CONTENT) + 10}-{len(CONTENT) + 20}"},
        )

    assert response.status_code == 206
    assert response.content == CONTENT[10:20]
    assert response.headers["content-range"] == f"bytes 10-19/{len(CONTENT)}"
    assert beyond.status_code == 416


async def test_a_matching_etag_answers_not_modified(
    identity_settings: Settings, identity_database: str
) -> None:
    """The content hash is the validator, so a client that has the bytes revalidates cheaply."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(client, workspace, "beach.jpg", body=CONTENT)
        response = await client.get(
            content_url(created.json()["id"]), headers={"If-None-Match": f'"{DIGEST}"'}
        )

    assert response.status_code == 304
    assert response.content == b""


@pytest.mark.parametrize(
    ("name", "declared", "expected"),
    [
        ("beach.jpg", "image/jpeg", "inline"),
        ("clip.mp4", "video/mp4", "inline"),
        ("notes.txt", "text/plain", "inline"),
        # The two that would otherwise run script in our own origin.
        ("page.html", "text/html", "attachment"),
        ("drawing.svg", "image/svg+xml", "attachment"),
        # And anything we cannot vouch for.
        ("archive.zip", "application/zip", "attachment"),
    ],
)
async def test_only_inert_types_are_served_inline(
    identity_settings: Settings, identity_database: str, name: str, declared: str, expected: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(client, workspace, name, body=CONTENT, content_type=declared)
        response = await client.get(content_url(created.json()["id"]))

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(expected)


@pytest.mark.fr("F-001/FR-12")
async def test_content_reached_through_a_symlink_out_of_the_workspace_is_refused(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    """Every open re-resolves and re-checks containment (ADR-0019). The File Browser CVEs are
    exactly this: a path that *looks* inside, resolving somewhere else."""
    secret = tmp_path / "outside.txt"
    secret.write_bytes(b"not yours")

    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "beach.jpg", body=CONTENT)
        # Someone replaces the real file with a link out of the tree, on disk.
        (root / "beach.jpg").unlink()
        (root / "beach.jpg").symlink_to(secret)

        response = await client.get(content_url(created.json()["id"]))

    assert response.status_code == 404
    assert b"not yours" not in response.content


async def test_content_that_vanished_from_disk_is_reported_honestly(
    identity_settings: Settings, identity_database: str
) -> None:
    """The row is not evidence that the bytes are there; reconciling is re-scan's job."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "beach.jpg", body=CONTENT)
        (root / "beach.jpg").unlink()

        response = await client.get(content_url(created.json()["id"]))

    assert response.status_code == 404
    assert "storage" in response.json()["detail"]


async def test_another_users_file_is_not_found(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app:
        async with signed_in(app) as admin:
            await create_member(admin)
            workspace = UUID((await create_workspace(admin, "Mine")).json()["id"])
            await provision_pending(identity_database)
            created = await create_upload(admin, workspace, "beach.jpg", body=CONTENT)
            file_id = created.json()["id"]
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            metadata = await member.get(f"{API_V1_PREFIX}/files/{file_id}")
            content = await member.get(content_url(file_id))

    assert metadata.status_code == 404
    assert content.status_code == 404


async def test_an_unknown_file_is_not_found(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, _ws, _root):
        metadata = await client.get(f"{API_V1_PREFIX}/files/{uuid4()}")
        content = await client.get(content_url(uuid4()))

    assert metadata.status_code == 404
    assert content.status_code == 404
