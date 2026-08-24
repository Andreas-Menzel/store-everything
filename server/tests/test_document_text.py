"""Document text, from an upload to the segments a search will point into.

The extractors themselves are tested in `extractors/tests` — every branch of the decision tree,
every encoding, against fixtures whose truth is exact. This file tests something they cannot: that
what PyMuPDF read arrives at `GET /files/{id}/segments` with its page still attached.

That distinction is why these are not mocked.
[F-004/FR-1](../../features/F-004-document-text-extraction.md) is a claim about an API response,
and a fake extractor emitting a hand-written envelope would verify the plumbing while leaving the
requirement untested — the text has to come out of the real tool, over a real socket, in the real
result shape. So this runs the same three-sided arrangement
as `test_reference_extractor.py`: an instance serving HTTP, a worker draining its queue, and the
official extractors as separate threads of control talking to both.

The corpus is what makes the assertions exact
([ADR-0015](../../decisions/ADR-0015-ground-truth-corpus.md)): `three-pages.pdf` has a known line
per page, `mixed-text.pdf` has one page with text and one
without, `known-phrases.txt` has a known line 5. Nothing here asserts "some text was found".
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from se_extractor import ExtractorClient, Worker
from se_extractor import pdf_text as pdf_text_extractor
from se_extractor import text_plain as text_plain_extractor
from se_extractor.loop import JobContext
from se_extractor.models import Job

from store_everything.api import API_V1_PREFIX
from store_everything.config import Settings
from tests.identity_helpers import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.live_instance import live_instance

# Synchronous on purpose: the SDK is, and so is a container.
pytestmark = [pytest.mark.integration]

_TIMEOUT = 60.0
_POLL = 0.1

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "fixtures"


@contextmanager
def _signed_in(base_url: str) -> Generator[httpx.Client]:
    with httpx.Client(base_url=base_url, headers={"Origin": base_url}, timeout=30.0) as client:
        response = client.post(
            f"{API_V1_PREFIX}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200, response.text
        yield client


def _active_workspace(client: httpx.Client, name: str = "Documents") -> str:
    created = client.post(f"{API_V1_PREFIX}/workspaces", json={"name": name})
    assert created.status_code == 201, created.text
    identifier = str(created.json()["id"])

    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        if client.get(f"{API_V1_PREFIX}/workspaces/{identifier}").json().get("state") == "active":
            return identifier
        time.sleep(_POLL)
    raise AssertionError("the workspace never became active — did the worker start?")


def _provision(client: httpx.Client, extractor_id: str) -> str:
    response = client.post(f"{API_V1_PREFIX}/extractors", json={"id": extractor_id})
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


def _upload(
    client: httpx.Client, workspace: str, path: str, body: bytes, media_type: str
) -> dict[str, Any]:
    response = client.post(
        f"{API_V1_PREFIX}/workspaces/{workspace}/files",
        params={"path": path},
        content=body,
        headers={"upload-complete": "?1", "Content-Type": media_type},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _await_status(client: httpx.Client, file_id: str, wanted: str) -> dict[str, Any]:
    deadline = time.monotonic() + _TIMEOUT
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get(f"{API_V1_PREFIX}/files/{file_id}/extraction").json()
        if last["status"] == wanted:
            return last
        time.sleep(_POLL)
    raise AssertionError(f"extraction stayed {last.get('status')!r} instead of reaching {wanted!r}")


@contextmanager
def _running(
    base_url: str,
    token: str,
    manifest: dict[str, Any],
    handle: Callable[[Job, JobContext], dict[str, Any] | None],
) -> Generator[None]:
    """One official extractor, as its own thread of control against a real socket."""
    client = ExtractorClient(base_url, token)
    worker = Worker(client, manifest, handle, claim_wait=1, worker_name="test")
    thread = threading.Thread(target=worker.run, name=manifest["id"], daemon=True)
    thread.start()
    try:
        yield
    finally:
        worker.stop()
        thread.join(timeout=10)
        client.close()


def _segments(client: httpx.Client, file_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{API_V1_PREFIX}/files/{file_id}/segments", params={"limit": 100})
    response.raise_for_status()
    return list(response.json()["data"])


def _facts(client: httpx.Client, file_id: str) -> dict[str, Any]:
    response = client.get(f"{API_V1_PREFIX}/files/{file_id}/metadata")
    response.raise_for_status()
    return {entry["key"]: entry["value"] for entry in response.json()}


@pytest.mark.fr("F-004/FR-1")
@pytest.mark.fr("F-004/FR-7")
def test_a_born_digital_pdf_becomes_segments_that_know_their_page(
    identity_settings: Settings,
) -> None:
    """Page 3's phrase is findable *as page 3* — which is the whole point of an anchor."""
    document = (CORPUS / "documents" / "three-pages.pdf").read_bytes()
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin)
        token = _provision(admin, pdf_text_extractor.EXTRACTOR_ID)
        with _running(
            instance.base_url, token, pdf_text_extractor.MANIFEST, pdf_text_extractor.handle
        ):
            created = _upload(admin, workspace, "three-pages.pdf", document, "application/pdf")
            _await_status(admin, str(created["id"]), "indexed")
            found = _segments(admin, str(created["id"]))
            facts = _facts(admin, str(created["id"]))
            content = admin.get(f"{API_V1_PREFIX}/files/{created['id']}/content")

        assert [span["anchor_kind"] for span in found] == ["page", "page", "page"]
        assert [span["anchor"]["page"] for span in found] == [1, 2, 3]
        # The exact fixture lines, not "some text": the corpus states what each page says.
        assert "fixture page one" in found[0]["text"]
        assert "quick brown fox" in found[1]["text"]
        assert "xylophone marmalade" in found[2]["text"]

        assert facts["has_text_layer"] is True
        # No page needed OCR, so nothing routes there — and `ocr_pages` is absent rather than
        # empty, because a key that says "these pages" should not exist when there are none.
        assert facts["needs_ocr"] is False
        assert "ocr_pages" not in facts

        # FR-7: reading a text layer is reading. The bytes are the bytes that were uploaded.
        assert content.content == document
        assert hashlib.sha256(content.content).hexdigest() == hashlib.sha256(document).hexdigest()


@pytest.mark.fr("F-004/FR-2")
def test_a_page_without_text_is_named_for_ocr_while_its_neighbour_is_extracted(
    identity_settings: Settings,
) -> None:
    """The per-page decision, at the boundary: one document, one segment, one page sent onward.

    This is the mechanism the OCR extractor's routing predicate binds to
    ([ADR-0020](../../decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md)) — so it has to
    be true of what the *API* reports, not only of what the extractor returned.
    """
    document = (CORPUS / "documents" / "mixed-text.pdf").read_bytes()
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin)
        token = _provision(admin, pdf_text_extractor.EXTRACTOR_ID)
        with _running(
            instance.base_url, token, pdf_text_extractor.MANIFEST, pdf_text_extractor.handle
        ):
            created = _upload(admin, workspace, "mixed-text.pdf", document, "application/pdf")
            _await_status(admin, str(created["id"]), "indexed")
            found = _segments(admin, str(created["id"]))
            facts = _facts(admin, str(created["id"]))

        assert [span["anchor"]["page"] for span in found] == [1]
        assert "carries its own text" in found[0]["text"]

        assert facts["has_text_layer"] is True
        assert facts["needs_ocr"] is True
        assert facts["ocr_pages"] == [2]


@pytest.mark.fr("F-004/FR-3")
@pytest.mark.fr("F-004/FR-4")
def test_a_text_file_becomes_segments_that_know_their_lines(identity_settings: Settings) -> None:
    """Line anchors and a language, for the files there are most of."""
    body = (CORPUS / "text" / "known-phrases.txt").read_bytes()
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin)
        token = _provision(admin, text_plain_extractor.EXTRACTOR_ID)
        with _running(
            instance.base_url, token, text_plain_extractor.MANIFEST, text_plain_extractor.handle
        ):
            created = _upload(admin, workspace, "known-phrases.txt", body, "text/plain")
            _await_status(admin, str(created["id"]), "indexed")
            found = _segments(admin, str(created["id"]))
            facts = _facts(admin, str(created["id"]))

        # Five lines with no blank line between them: one segment covering all five, which is
        # the honest answer — there is no paragraph boundary to split on.
        assert [span["anchor_kind"] for span in found] == ["line"]
        assert found[0]["anchor"] == {"start_line": 1, "end_line": 5}
        assert "xylophone marmalade" in found[0]["text"]

        # FR-4: the language reaches the API as a typed fact *and* rides on the segment, which is
        # what a language-aware analyser will read it from.
        assert facts["language"] == "en"
        assert found[0]["language"] == "en"
        assert facts["line_count"] == 6  # five lines and the trailing newline's empty sixth


@pytest.mark.fr("F-004/FR-3")
def test_paragraphs_become_their_own_segments_with_their_own_lines(
    identity_settings: Settings,
) -> None:
    """A markdown file has boundaries, and a hit should point at the paragraph it is in."""
    body = (CORPUS / "text" / "sample.md").read_bytes()
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin)
        token = _provision(admin, text_plain_extractor.EXTRACTOR_ID)
        with _running(
            instance.base_url, token, text_plain_extractor.MANIFEST, text_plain_extractor.handle
        ):
            created = _upload(admin, workspace, "sample.md", body, "text/markdown")
            _await_status(admin, str(created["id"]), "indexed")
            found = _segments(admin, str(created["id"]))

        # The fixture's five blocks, in reading order, each anchored where it starts.
        assert [span["anchor"]["start_line"] for span in found] == [1, 3, 5, 7, 10]
        assert found[0]["text"] == "# Fixture heading"
        assert "xylophone marmalade" in found[-1]["text"]
        assert [span["ordinal"] for span in found] == sorted(span["ordinal"] for span in found)


@pytest.mark.fr("F-004/FR-6")
def test_a_file_that_is_not_text_fails_visibly_and_stays_stored(
    identity_settings: Settings,
) -> None:
    """FR-6's promise, end to end: the failure is a status, not a lost file.

    The fixture is the mislabeled one — plain bytes that are not text under a name that claims
    otherwise. Here it is uploaded *as* `text/plain` with a payload no encoding explains, which is
    the shape a real broken export has.
    """
    body = bytes(range(0, 32)) * 40
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin)
        token = _provision(admin, text_plain_extractor.EXTRACTOR_ID)
        with _running(
            instance.base_url, token, text_plain_extractor.MANIFEST, text_plain_extractor.handle
        ):
            created = _upload(admin, workspace, "broken-export.txt", body, "text/plain")
            failed = _await_status(admin, str(created["id"]), "failed")
            content = admin.get(f"{API_V1_PREFIX}/files/{created['id']}/content")

        run = failed["runs"][0]
        assert run["extractor"] == text_plain_extractor.EXTRACTOR_ID
        assert run["state"] == "failed"
        assert "not text" in (run["error"] or "")

        # Stored, downloadable, and findable by name — a failed extraction is not a failed upload.
        assert content.status_code == 200
        assert content.content == body
        assert not _segments(admin, str(created["id"]))
