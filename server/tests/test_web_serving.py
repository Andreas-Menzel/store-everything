"""Serving the built client from the API's own origin.

[F-027/FR-1 and FR-2](../../features/F-027-web-application-shell.md). One image and one origin is
what makes the session cookie same-site by construction
([10 § topology](../../specs/10-deployment-and-operations.md#topology)), and it puts three
questions on the API that it did not have before: what answers an unknown path, what may be
cached, and what a document from this origin is allowed to load.

The last one is the security one. An origin that holds a session cookie must not be able to run
somebody else's JavaScript, so the policy is asserted here rather than eyeballed in a browser.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from store_everything import web
from store_everything.app import create_app
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio

ENTRY = "<!doctype html><title>Store Everything</title><div id=app></div>"


def build(root: Path) -> Path:
    """A directory shaped like a Vite build: an entry document and a fingerprinted asset."""
    root.mkdir(parents=True, exist_ok=True)
    (root / web.ENTRY_DOCUMENT).write_text(ENTRY, encoding="utf-8")
    assets = root / web.ASSETS
    assets.mkdir()
    (assets / "app-a1b2c3d4.js").write_text("export const app = 1;\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return root


async def serving(root: Path) -> httpx.AsyncClient:
    app = create_app(make_settings(web_root=root))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.fr("F-027/FR-1")
async def test_a_client_route_gets_the_entry_document_and_is_never_cached(
    tmp_path: Path,
) -> None:
    """A deep link has to survive a reload: the router that understands it is *in* the document.

    And the document may not be cached, or a deployed change would need someone to purge
    something before anyone saw it.
    """
    async with await serving(build(tmp_path / "web")) as client:
        deep = await client.get("/folders/01a02900-0000-7000-8000-000000000000")
        root = await client.get("/")

    assert deep.status_code == 200
    assert deep.text == ENTRY
    assert deep.headers["cache-control"] == "no-store"
    assert root.text == ENTRY


@pytest.mark.fr("F-027/FR-1")
async def test_a_fingerprinted_asset_is_immutable_and_a_plain_file_is_not(
    tmp_path: Path,
) -> None:
    """Vite puts a content hash in every asset name, so `immutable` is the truth for those —
    and a trap for anything whose name stays the same across builds."""
    async with await serving(build(tmp_path / "web")) as client:
        asset = await client.get(f"/{web.ASSETS}/app-a1b2c3d4.js")
        icon = await client.get("/favicon.svg")

    assert asset.status_code == 200
    assert asset.headers["cache-control"] == web.IMMUTABLE
    assert icon.status_code == 200
    assert icon.headers["cache-control"] == "no-store"


@pytest.mark.fr("F-027/FR-1")
async def test_api_paths_keep_their_own_answers(tmp_path: Path) -> None:
    """The fallback sits under everything `/api/v1` does not claim — including the paths it
    *should* have claimed. An unknown API path answering with an HTML document would be handed
    to a client that parses it as data."""
    async with await serving(build(tmp_path / "web")) as client:
        health = await client.get("/healthz")
        unknown = await client.get("/api/v1/no-such-thing")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/problem+json")


@pytest.mark.fr("F-027/FR-1")
async def test_an_image_without_a_client_still_serves_the_api(tmp_path: Path) -> None:
    """The development container runs the client from Vite, so a missing build is a state rather
    than a failure — and the API must not become unreachable because of it."""
    app = create_app(make_settings(web_root=tmp_path / "absent"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/healthz")
        nothing = await client.get("/folders")

    assert app.state.serves_web is False
    assert health.status_code == 200
    assert nothing.status_code == 404, "with no client to serve there is no fallback either"


@pytest.mark.fr("F-027/FR-1")
async def test_a_path_climbing_out_of_the_build_directory_is_not_served(tmp_path: Path) -> None:
    """*(negative space)* The same containment rule the file store enforces, for the same reason.

    A path from a request is never trusted to stay inside the directory it names — and here the
    directory sits in an image beside application code.
    """
    root = build(tmp_path / "web")
    (tmp_path / "secret.txt").write_text("not yours", encoding="utf-8")

    async with await serving(root) as client:
        escaped = await client.get("/../secret.txt")
        encoded = await client.get("/%2e%2e/secret.txt")

    for response in (escaped, encoded):
        assert "not yours" not in response.text


@pytest.mark.fr("F-027/FR-2")
async def test_every_document_carries_the_app_origin_policy(tmp_path: Path) -> None:
    """The other half of a same-origin session cookie: this origin runs our scripts only.

    `unsafe-eval` is the line — a documentation viewer that needed it would be replaced rather
    than accommodated (F-027/FR-9) — and nothing may come from a third-party host, so an instance
    on a private network with no egress at all works.
    """
    async with await serving(build(tmp_path / "web")) as client:
        document = await client.get("/")
        asset = await client.get(f"/{web.ASSETS}/app-a1b2c3d4.js")

    for response in (document, asset):
        policy = response.headers["content-security-policy"]
        assert "script-src 'self'" in policy
        assert "unsafe-eval" not in policy
        assert "frame-ancestors 'none'" in policy
        assert "object-src 'none'" in policy
        # No third-party host anywhere in the policy: every source is this origin or a scheme.
        assert "http://" not in policy and "https://" not in policy

    assert document.headers["x-frame-options"] == "DENY", "the older header still applies too"


@pytest.mark.fr("F-027/FR-2")
async def test_the_api_keeps_its_own_policies(tmp_path: Path) -> None:
    """Three policies, three purposes. The app's documents get one; JSON needs none; and the
    endpoint that serves a user's own bytes has a stricter one of its own, which this must not
    have loosened."""
    async with await serving(build(tmp_path / "web")) as client:
        health = await client.get("/healthz")

    assert "content-security-policy" not in health.headers
