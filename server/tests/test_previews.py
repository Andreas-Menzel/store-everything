"""The descriptor, pages on demand, and renditions.

[F-028](../../features/F-028-thumbnails-and-previews.md) FR-6 to FR-9. Three promises, and the
middle one is the interesting one:

- a client renders from a **descriptor** rather than from the media type, so a preview kind an
  extractor invents next month needs no client change (FR-6);
- a document's pages are rendered **when somebody asks**, stored, and served from storage
  afterwards — the first request queues, the second serves (FR-7);
- a **rendition** is the whole file in another form, downloadable, and `/content` still serves
  the bytes that were uploaded (FR-8, FR-9).

The extractor is driven through the real wire protocol here: an on-demand page is a job the core
queues and a container claims, so a test that wrote the asset directly would prove nothing about
the part that can actually break.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from store_everything.api import API_V1_PREFIX
from store_everything.config import Settings
from tests.extraction_helpers import (
    as_extractor,
    claim_one,
    extraction_ready,
    finish,
    install,
    runs_in,
    stage,
)
from tests.upload_helpers import create_upload
from tests.workspace_helpers import MEMBER_EMAIL, MEMBER_PASSWORD, create_member, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PAGES = 3


async def upload(client: httpx.AsyncClient, workspace: UUID, name: str) -> dict[str, Any]:
    created = await create_upload(client, workspace, f"Papers/{name}", body=b"%PDF-1.4 fixture")
    assert created.status_code == 201, created.text
    return created.json()


async def install_renderers(app: Any, admin: httpx.AsyncClient) -> tuple[Any, Any]:
    """The two extractors this file needs: the thumbnail maker and the page renderer."""
    previews = await install(
        app,
        admin,
        "preview-gen",
        produces=["metadata", "derived_assets"],
        derived_asset_kinds=["thumbnail", "image-preview"],
    )
    pages = await install(
        app,
        admin,
        "pdf-pages",
        produces=["derived_assets"],
        derived_asset_kinds=["page"],
    )
    return previews, pages


async def run(app: Any, token: str, answer: Any) -> int:
    """Claim every job this extractor has and answer it. Returns how many were done."""
    done = 0
    async with as_extractor(app, token) as extractor:
        while (job := await claim_one(extractor)) is not None:
            envelope = await answer(extractor, job)
            response = await finish(extractor, job, **envelope)
            assert response.status_code == 200, response.text
            done += 1
    return done


async def thumbnails(extractor: httpx.AsyncClient, job: dict[str, Any]) -> dict[str, Any]:
    digest, staged = await stage(extractor, job, b"thumb-bytes")
    assert staged.status_code == 200
    preview_digest, _ = await stage(extractor, job, b"preview-bytes")
    return {
        "metadata": [
            {"key": "page_count", "type": "integer", "value": PAGES},
            {"key": "placeholder_hash", "type": "string", "value": "AQQDAyABkM4f"},
        ],
        "derived_assets": [
            {
                "kind": "thumbnail",
                "name": "thumb-256.webp",
                "content_hash": digest,
                "media_type": "image/webp",
                "params": {"size": 256, "width": 198, "height": 256},
            },
            {
                "kind": "image-preview",
                "name": "preview.webp",
                "content_hash": preview_digest,
                "media_type": "image/webp",
                "params": {"width": 1584, "height": 2048},
            },
        ],
    }


def page_answer(page: int, payload: bytes):
    async def answer(extractor: httpx.AsyncClient, job: dict[str, Any]) -> dict[str, Any]:
        assert job["params"].get("page", 1) == page, job["params"]
        digest, staged = await stage(extractor, job, payload)
        assert staged.status_code == 200
        return {
            "derived_assets": [
                {
                    "kind": "page",
                    "name": f"page-{page:04d}.webp",
                    "content_hash": digest,
                    "media_type": "image/webp",
                    "params": {"page": page, "width": 1236, "height": 1600, "pages": PAGES},
                }
            ]
        }

    return answer


@pytest.mark.fr("F-028/FR-6")
async def test_the_descriptor_names_what_exists_and_how_to_get_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """Everything renderable, with its URL — so a client never guesses from a MIME type.

    The kinds are an open vocabulary on purpose: this test asserts the *shape* (kind, params, a
    URL that works), not a fixed list, because a plugin's new kind has to appear here without a
    core change.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        previews, pages = await install_renderers(app, client)
        document = await upload(client, workspace, "report.pdf")
        await run(app, previews.token, thumbnails)
        await run(app, pages.token, page_answer(1, b"page-one"))

        described = await client.get(f"{API_V1_PREFIX}/files/{document['id']}/preview")

    assert described.status_code == 200, described.text
    body = described.json()
    assert body["version"] == document["version"]
    assert body["thumbnail_sizes"] == [256, 512, 1024]
    assert body["placeholder_hash"] == "AQQDAyABkM4f"
    assert body["pages"] == PAGES
    # The pattern is *named*, not assumed: substituting into it is all a client has to know.
    assert body["pages_url"].endswith(f"/preview/pages/{{page}}?v={document['version']}")

    kinds = {asset["kind"]: asset for asset in body["assets"]}
    assert set(kinds) == {"thumbnail", "image-preview", "page"}
    assert kinds["thumbnail"]["params"]["size"] == 256
    assert kinds["page"]["params"]["page"] == 1
    assert kinds["image-preview"]["media_type"] == "image/webp"
    # Every URL the descriptor hands out is pinned, which is what makes it cacheable forever.
    assert all(f"v={document['version']}" in asset["url"] for asset in body["assets"])


@pytest.mark.fr("F-028/FR-6")
async def test_an_asset_the_descriptor_named_can_be_fetched(
    identity_settings: Settings, identity_database: str
) -> None:
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        previews, _pages = await install_renderers(app, client)
        document = await upload(client, workspace, "report.pdf")
        await run(app, previews.token, thumbnails)

        described = await client.get(f"{API_V1_PREFIX}/files/{document['id']}/preview")
        preview = next(
            asset for asset in described.json()["assets"] if asset["kind"] == "image-preview"
        )
        fetched = await client.get(preview["url"])
        missing = await client.get(
            f"{API_V1_PREFIX}/files/{document['id']}"
            "/preview/assets/0198c0de-0000-7000-8000-000000000000"
        )

    assert fetched.status_code == 200
    assert fetched.content == b"preview-bytes"
    assert fetched.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert missing.status_code == 404


@pytest.mark.fr("F-028/FR-7")
async def test_a_page_is_rendered_when_somebody_asks_and_stored_afterwards(
    identity_settings: Settings, identity_database: str
) -> None:
    """The whole point of on-demand generation, in one test.

    Page 1 came with the thumbnail. Page 3 does not exist until somebody asks for it: the first
    request answers `202` and queues the work at interactive priority, and once a container has
    rendered it the second request serves stored bytes — no re-render, and no bulk pass over the
    other 297 pages of a book nobody opened.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        previews, pages = await install_renderers(app, client)
        document = await upload(client, workspace, "report.pdf")
        await run(app, previews.token, thumbnails)
        await run(app, pages.token, page_answer(1, b"page-one"))

        page_url = f"{API_V1_PREFIX}/files/{document['id']}/preview/pages"
        first = await client.get(f"{page_url}/1")
        asked = await client.get(f"{page_url}/3")
        # Asking twice queues once: the job's identity includes which page it is about.
        again = await client.get(f"{page_url}/3")

        rendered = await run(app, pages.token, page_answer(3, b"page-three"))
        served = await client.get(f"{page_url}/3")
        pinned = await client.get(f"{page_url}/3", params={"v": document["version"]})
        # And once stored, it is served from storage rather than rendered again.
        async with as_extractor(app, pages.token) as extractor:
            idle = await claim_one(extractor)

        beyond = await client.get(f"{page_url}/9")
        runs = await runs_in(identity_database)

    assert first.status_code == 200, "page one arrived with the thumbnail"
    assert first.content == b"page-one"

    assert asked.status_code == 202, asked.text
    assert asked.headers["retry-after"] == "1"
    assert again.status_code == 202
    assert rendered == 1, "one job, however many times it was asked for"

    assert served.status_code == 200
    assert served.content == b"page-three"
    # Unpinned follows the current version, so it is revalidated; the descriptor hands out the
    # pinned form, which can be kept for a year (FR-4).
    assert served.headers["cache-control"] == "private, no-cache"
    assert pinned.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert idle is None, "a stored page is not re-rendered"

    assert beyond.status_code == 404, "there is no page nine in a three-page document"

    # The runs say what happened: routing's page-one job, and one variant job for page three.
    variants = sorted(run["variant"] or "-" for run in runs if run["extractor_id"] == "pdf-pages")
    assert variants == ["-", "page:3"]


@pytest.mark.fr("F-028/FR-7")
async def test_a_file_that_has_no_pages_has_no_page_one(
    identity_settings: Settings, identity_database: str
) -> None:
    """Nothing counted its pages, so there is nothing to ask for — and no job is queued."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await install_renderers(app, client)
        plain = await upload(client, workspace, "notes.bin")

        asked = await client.get(f"{API_V1_PREFIX}/files/{plain['id']}/preview/pages/1")
        described = await client.get(f"{API_V1_PREFIX}/files/{plain['id']}/preview")

    assert asked.status_code == 404
    assert described.json()["pages"] is None
    assert described.json()["pages_url"] is None


@pytest.mark.fr("F-028/FR-8", "F-028/FR-9")
async def test_a_rendition_is_downloadable_and_the_original_is_not_touched(
    identity_settings: Settings, identity_database: str
) -> None:
    """A rendition is the file in another form — an addition, never a replacement.

    So `/renditions` lists it, `/renditions/{kind}` downloads it as an attachment, and
    `/content` still answers with the bytes that were uploaded (FR-9, and 02 § invariant 2).
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(
            app,
            client,
            "ocr",
            produces=["derived_assets", "renditions"],
            derived_asset_kinds=["searchable-pdf"],
            renditions=[
                {
                    "kind": "searchable-pdf",
                    "format": "application/pdf",
                    "label": "PDF with a text layer",
                }
            ],
        )
        document = await upload(client, workspace, "scan.pdf")

        async def searchable(extractor: httpx.AsyncClient, job: dict[str, Any]) -> dict[str, Any]:
            digest, staged = await stage(extractor, job, b"%PDF with a text layer")
            assert staged.status_code == 200
            return {
                "derived_assets": [
                    {
                        "kind": "searchable-pdf",
                        "name": "searchable.pdf",
                        "content_hash": digest,
                        "media_type": "application/pdf",
                        "rendition_kind": "searchable-pdf",
                    }
                ]
            }

        await run(app, installed.token, searchable)

        listed = await client.get(f"{API_V1_PREFIX}/files/{document['id']}/renditions")
        downloaded = await client.get(
            f"{API_V1_PREFIX}/files/{document['id']}/renditions/searchable-pdf"
        )
        absent = await client.get(f"{API_V1_PREFIX}/files/{document['id']}/renditions/subtitles")
        original = await client.get(f"{API_V1_PREFIX}/files/{document['id']}/content")
        described = await client.get(f"{API_V1_PREFIX}/files/{document['id']}/preview")

    offered = listed.json()
    assert [one["kind"] for one in offered] == ["searchable-pdf"]
    assert offered[0]["media_type"] == "application/pdf"

    assert downloaded.status_code == 200
    assert downloaded.content == b"%PDF with a text layer"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert absent.status_code == 404

    # The original, unchanged and still served from `/content`.
    assert original.content == b"%PDF-1.4 fixture"
    # A rendition is not a preview asset: it is a download, and the descriptor says so
    # separately rather than mixing the two.
    assert described.json()["renditions"] == ["searchable-pdf"]
    assert all(asset["kind"] != "searchable-pdf" for asset in described.json()["assets"])


@pytest.mark.fr("F-028/FR-10")
async def test_every_preview_surface_is_closed_to_a_stranger(
    identity_settings: Settings, identity_database: str
) -> None:
    """Descriptor, page, asset, rendition: all of them answer as a nonexistent id would."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        previews, pages = await install_renderers(app, client)
        await create_member(client)
        document = await upload(client, workspace, "report.pdf")
        await run(app, previews.token, thumbnails)
        await run(app, pages.token, page_answer(1, b"page-one"))

        base = f"{API_V1_PREFIX}/files/{document['id']}"
        absent = f"{API_V1_PREFIX}/files/0198c0de-0000-7000-8000-000000000000"
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            answers = {
                path: (
                    await member.get(f"{base}{path}"),
                    await member.get(f"{absent}{path}"),
                )
                for path in (
                    "/preview",
                    "/preview/pages/1",
                    "/renditions",
                    "/renditions/searchable-pdf",
                    "/thumbnail",
                )
            }

    for path, (theirs, nothing) in answers.items():
        assert theirs.status_code == 404, f"{path} leaked a status"
        assert theirs.status_code == nothing.status_code, path
        assert theirs.json()["type"] == nothing.json()["type"], path
