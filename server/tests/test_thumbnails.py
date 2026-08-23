"""The thumbnail surface: fixed sizes, pinned URLs, and an honest "nothing to render".

[F-028](../../features/F-028-thumbnails-and-previews.md) FR-1 to FR-5, FR-9 and FR-10. The
extractor that *makes* thumbnails is tested against real pixels in `extractors/`; what is tested
here is everything the API promises about them:

- **snapping** — ask for 300, get 512, because that is what makes a URL immutable;
- **pinning** — `?v=` is cacheable for a year, the unpinned URL is not;
- **absence** — a file with no visual source answers with a typed problem, never a placeholder
  image pretending to be the file;
- **the grid's two inline facts** — the placeholder and whether a thumbnail exists at all, so a
  listing paints without a request per row;
- **permission** — every one of those answers is `404` for somebody else's file.

The assets are staged through the extractor API rather than written into the derived store
behind the app's back: what a real `preview-gen` does is upload bytes and reference them, and a
test that skipped that would not exercise the path that stores them.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

import httpx
import pytest

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.previews import THUMBNAIL_SIZES
from tests.extraction_helpers import (
    as_extractor,
    claim_one,
    extraction_ready,
    finish,
    install,
    stage,
)
from tests.upload_helpers import create_upload
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    create_member,
    signed_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Stand-in bytes per tier. The image itself does not matter here — what matters is that the
#: right *tier* comes back, and distinct bytes per tier are how a test can tell.
TIER_BYTES = {size: f"webp-{size}".encode() for size in THUMBNAIL_SIZES}


def thumbnail_url(file_id: str) -> str:
    return f"{API_V1_PREFIX}/files/{file_id}/thumbnail"


async def upload(client: httpx.AsyncClient, workspace: UUID, name: str) -> dict[str, Any]:
    created = await create_upload(client, workspace, f"Papers/{name}", body=b"a document")
    assert created.status_code == 201, created.text
    return created.json()


async def render_thumbnails(
    app: Any, token: str, *, placeholder: str | None = "AQQDAyABkM4f"
) -> None:
    """Claim every queued job and answer it the way `preview-gen` would."""
    async with as_extractor(app, token) as extractor:
        while (job := await claim_one(extractor)) is not None:
            assets = []
            for size, payload in TIER_BYTES.items():
                digest, staged = await stage(extractor, job, payload)
                assert staged.status_code == 200, staged.text
                assets.append(
                    {
                        "kind": "thumbnail",
                        "name": f"thumb-{size}.webp",
                        "content_hash": digest,
                        "media_type": "image/webp",
                        "params": {"size": size, "width": size, "height": size // 2},
                    }
                )
            metadata = (
                []
                if placeholder is None
                else [{"key": "placeholder_hash", "type": "string", "value": placeholder}]
            )
            done = await finish(extractor, job, metadata=metadata, derived_assets=assets)
            assert done.status_code == 200, done.text


@pytest.mark.fr("F-028/FR-1")
async def test_a_size_snaps_up_into_the_fixed_set(
    identity_settings: Settings, identity_database: str
) -> None:
    """Ask for anything; get the nearest tier at or above it — and the largest beyond the set.

    Snapping *up* is the rule that matters: a client asking for 300 px is describing the space it
    has to fill, and 256 would make it upscale. Snapping at all is what keeps the set of stored
    files bounded and every URL cacheable.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(
            app, client, produces=["derived_assets"], derived_asset_kinds=["thumbnail"]
        )
        document = await upload(client, workspace, "scan.pdf")
        await render_thumbnails(app, installed.token)

        answers = {
            asked: await client.get(thumbnail_url(document["id"]), params={"size": asked})
            for asked in (1, 256, 257, 300, 512, 513, 1024, 4096)
        }
        default = await client.get(thumbnail_url(document["id"]))

    assert {asked: answer.status_code for asked, answer in answers.items()} == dict.fromkeys(
        answers, 200
    )
    assert answers[1].content == TIER_BYTES[256]
    assert answers[256].content == TIER_BYTES[256]
    assert answers[257].content == TIER_BYTES[512]
    assert answers[300].content == TIER_BYTES[512]
    assert answers[512].content == TIER_BYTES[512]
    assert answers[513].content == TIER_BYTES[1024]
    assert answers[1024].content == TIER_BYTES[1024]
    # Above the set: the largest there is, rather than a refusal or a resize.
    assert answers[4096].content == TIER_BYTES[1024]
    # No size at all is the grid size, which is what a client that does not care wants.
    assert default.content == TIER_BYTES[256]
    assert default.headers["content-type"] == "image/webp"


@pytest.mark.fr("F-028/FR-4")
async def test_a_pinned_thumbnail_can_be_cached_forever(
    identity_settings: Settings, identity_database: str
) -> None:
    """`?v=` names bytes that can never change; without it the URL follows the current version.

    That difference is the whole caching contract: an immutable answer is kept for a year without
    asking, and the unpinned one is revalidated because a new upload changes what it means.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(
            app, client, produces=["derived_assets"], derived_asset_kinds=["thumbnail"]
        )
        document = await upload(client, workspace, "scan.pdf")
        await render_thumbnails(app, installed.token)

        pinned = await client.get(
            thumbnail_url(document["id"]), params={"size": 256, "v": document["version"]}
        )
        current = await client.get(thumbnail_url(document["id"]), params={"size": 256})
        revalidated = await client.get(
            thumbnail_url(document["id"]),
            params={"size": 256},
            headers={"If-None-Match": current.headers["etag"]},
        )
        # A version of *another* file is not a way in: the pin is checked against this file.
        other = await upload(client, workspace, "other.pdf")
        borrowed = await client.get(thumbnail_url(document["id"]), params={"v": other["version"]})

    assert pinned.status_code == 200
    assert pinned.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert current.status_code == 200
    assert "immutable" not in current.headers["cache-control"]
    assert revalidated.status_code == 304, "an unchanged thumbnail costs no bytes"
    assert borrowed.status_code == 404


@pytest.mark.fr("F-028/FR-3")
async def test_a_file_with_nothing_to_render_says_so(
    identity_settings: Settings, identity_database: str
) -> None:
    """A typed problem, not a broken image and not an error placeholder.

    The distinction matters to a client: `no-thumbnail` means *render a type icon*, while any
    other failure means *something went wrong*. A picture saying "no picture" would be
    indistinguishable from a thumbnail of a file that happens to look like that.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        _app,
        client,
        workspace,
        _root,
    ):
        document = await upload(client, workspace, "notes.bin")

        answered = await client.get(thumbnail_url(document["id"]), params={"size": 256})

    assert answered.status_code == 404
    body = answered.json()
    assert body["type"].endswith("/no-thumbnail")
    assert answered.headers["content-type"].startswith("application/problem+json")


@pytest.mark.fr("F-028/FR-5")
async def test_the_row_of_a_rendered_file_paints_before_the_image_arrives(
    identity_settings: Settings, identity_database: str
) -> None:
    """Everything a grid needs to paint a cell, in the row itself.

    The placeholder so the cell has the right shape and roughly the right colours immediately,
    the version id so the client can build a pinned (and therefore permanently cacheable)
    thumbnail URL, and `has_thumbnail` so a file with nothing to render gets an icon rather than
    a failed image request.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(
            app, client, produces=["derived_assets"], derived_asset_kinds=["thumbnail"]
        )
        rendered = await upload(client, workspace, "scan.pdf")
        await render_thumbnails(app, installed.token, placeholder="AQQDAyABkM4fJcIiLSdAmRtDoQ")

        root = (await client.get(f"{API_V1_PREFIX}/workspaces/{workspace}")).json()["root_folder"]
        papers = next(
            row
            for row in (await client.get(f"{API_V1_PREFIX}/folders/{root}/children")).json()["data"]
            if row["kind"] == "folder"
        )
        listed = await client.get(f"{API_V1_PREFIX}/folders/{papers['id']}/children")

    row = next(one for one in listed.json()["data"] if one["id"] == rendered["id"])
    assert row["placeholder_hash"] == "AQQDAyABkM4fJcIiLSdAmRtDoQ"
    assert len(row["placeholder_hash"]) <= 64, "F-028/FR-5's bound, inline in every row"
    assert row["has_thumbnail"] is True
    assert row["version"] == rendered["version"]


@pytest.mark.fr("F-028/FR-3")
async def test_a_row_with_nothing_rendered_says_so_too(
    identity_settings: Settings, identity_database: str
) -> None:
    async with extraction_ready(identity_settings, identity_database) as (
        _app,
        client,
        workspace,
        _root,
    ):
        plain = await upload(client, workspace, "notes.bin")

        root = (await client.get(f"{API_V1_PREFIX}/workspaces/{workspace}")).json()["root_folder"]
        papers = next(
            row
            for row in (await client.get(f"{API_V1_PREFIX}/folders/{root}/children")).json()["data"]
            if row["kind"] == "folder"
        )
        listed = await client.get(f"{API_V1_PREFIX}/folders/{papers['id']}/children")

    row = next(one for one in listed.json()["data"] if one["id"] == plain["id"])
    assert row["has_thumbnail"] is False
    assert row["placeholder_hash"] is None


@pytest.mark.fr("F-028/FR-9")
async def test_the_original_is_untouched_by_any_of_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """Generating assets never edits the file (FR-9, and 02 § invariant 2).

    The content hash after rendering is the hash of the bytes that were uploaded, and `/content`
    still serves them — a rendition is an addition, never a replacement.
    """
    payload = b"a document"
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(
            app, client, produces=["derived_assets"], derived_asset_kinds=["thumbnail"]
        )
        document = await upload(client, workspace, "scan.pdf")
        await render_thumbnails(app, installed.token)

        after = await client.get(f"{API_V1_PREFIX}/files/{document['id']}")
        content = await client.get(f"{API_V1_PREFIX}/files/{document['id']}/content")

    assert after.json()["content_hash"] == hashlib.sha256(payload).hexdigest()
    assert content.content == payload


@pytest.mark.fr("F-028/FR-10")
async def test_another_users_thumbnail_does_not_exist(
    identity_settings: Settings, identity_database: str
) -> None:
    """Every visual surface answers a stranger exactly as a nonexistent id would."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(
            app, client, produces=["derived_assets"], derived_asset_kinds=["thumbnail"]
        )
        await create_member(client)
        document = await upload(client, workspace, "scan.pdf")
        await render_thumbnails(app, installed.token)

        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            theirs = await member.get(thumbnail_url(document["id"]), params={"size": 256})
            absent = await member.get(
                thumbnail_url("0198c0de-0000-7000-8000-000000000000"), params={"size": 256}
            )

    assert theirs.status_code == absent.status_code == 404
    assert theirs.json()["type"] == absent.json()["type"], "the same answer, to the letter"
