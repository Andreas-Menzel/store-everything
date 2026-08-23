"""The reference extractor and the conformance kit, against a real instance.

This is the only place where all three sides are the real thing at once: an instance serving
HTTP, a worker draining its queue, and an extractor that is a separate program talking to both
over sockets. Everything else in the suite drives one of them in isolation, which is faster and
usually enough — but "an extractor container analyses an uploaded file" is a claim about the
seams, and a test that mocked any of them would not be testing the seams.

It is also what makes the reference extractor's three jobs true (11 § test infrastructure): the
example an author copies, the double an end-to-end test uses, and the image the conformance kit
validates itself against.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from se_extractor import ExtractorClient, Worker
from se_extractor.conformance import Conformance, run_checks
from se_extractor.reference import EXTRACTOR_ID, build_manifest, handle

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from tests.identity_helpers import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.live_instance import live_instance

# Synchronous on purpose: the SDK is, and so is a container.
pytestmark = [pytest.mark.integration]

_TIMEOUT = 30.0
_POLL = 0.1


@contextmanager
def _signed_in(base_url: str) -> Generator[httpx.Client]:
    """An administrator's client against the live instance — a real cookie over a real socket."""
    with httpx.Client(base_url=base_url, headers={"Origin": base_url}, timeout=30.0) as client:
        response = client.post(
            f"{API_V1_PREFIX}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200, response.text
        yield client


def _active_workspace(client: httpx.Client, name: str = "Papers") -> str:
    created = client.post(f"{API_V1_PREFIX}/workspaces", json={"name": name})
    assert created.status_code == 201, created.text
    identifier = str(created.json()["id"])

    # Provisioning is an operation; the live worker is what makes it happen.
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        state = client.get(f"{API_V1_PREFIX}/workspaces/{identifier}").json()
        if state.get("state") == "active":
            return identifier
        time.sleep(_POLL)
    raise AssertionError("the workspace never became active — did the worker start?")


def _provision(client: httpx.Client, extractor_id: str = EXTRACTOR_ID) -> str:
    response = client.post(f"{API_V1_PREFIX}/extractors", json={"id": extractor_id})
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


def _upload(client: httpx.Client, workspace: str, path: str, body: bytes) -> dict[str, Any]:
    response = client.post(
        f"{API_V1_PREFIX}/workspaces/{workspace}/files",
        params={"path": path},
        content=body,
        headers={"upload-complete": "?1", "Content-Type": "text/plain"},
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


def _running(base_url: str, token: str, mode: str) -> tuple[Worker, threading.Thread]:
    """The reference extractor, as its own thread of control against a real socket."""
    client = ExtractorClient(base_url, token)
    worker = Worker(
        client,
        build_manifest(mode),
        lambda job, context: handle(job, context, mode=mode, delay=0.0),
        # Short, so a test does not wait out a long poll after the last job.
        claim_wait=1,
        worker_name="test",
    )
    thread = threading.Thread(target=worker.run, name=f"reference-{mode}", daemon=True)
    thread.start()
    return worker, thread


@pytest.mark.fr("F-001/FR-3")
def test_an_extractor_container_analyses_an_uploaded_file(identity_settings: Settings) -> None:
    """The whole spine of this phase, in one test: upload → routed → claimed → indexed."""
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin)
        token = _provision(admin)
        worker, thread = _running(instance.base_url, token, "verify")
        try:
            created = _upload(admin, workspace, "report.txt", b"a report worth reading twice")
            assert created["extraction_status"] == "pending"

            finished = _await_status(admin, str(created["id"]), "indexed")
        finally:
            worker.stop()
            thread.join(timeout=10)

        run = finished["runs"][0]
        assert run["extractor"] == EXTRACTOR_ID
        assert run["state"] == "succeeded"
        # Provenance is the version that actually ran, and the reference extractor puts its mode
        # in that version — so this also proves the stamp is not the manifest's default.
        assert run["extractor_version"] == "1.0.0+verify"
        assert run["started_at"] is not None
        assert run["finished_at"] is not None
        assert run["error"] is None


def test_an_extractor_that_cannot_read_its_input_says_so_permanently(
    identity_settings: Settings,
) -> None:
    """The other half of the promise: a broken extractor leaves the file stored and browsable,
    and the failure is visible rather than a job that retries forever."""
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin)
        token = _provision(admin)
        worker, thread = _running(instance.base_url, token, "fail-permanently")
        try:
            created = _upload(admin, workspace, "unreadable.txt", b"whatever this is")
            failed = _await_status(admin, str(created["id"]), "failed")
        finally:
            worker.stop()
            thread.join(timeout=10)

        assert failed["runs"][0]["state"] == "failed"
        assert "asked to fail permanently" in failed["runs"][0]["error"]

        # The file itself is untouched by any of it (02 § invariants #2).
        summary = admin.get(f"{API_V1_PREFIX}/files/{created['id']}").json()
        assert summary["extraction_status"] == "failed"
        content = admin.get(f"{API_V1_PREFIX}/files/{created['id']}/content")
        assert content.status_code == 200
        assert content.content == b"whatever this is"


def test_the_conformance_kit_passes_against_the_core_and_the_reference_extractor(
    identity_settings: Settings,
) -> None:
    """The kit's own test: every check it makes has to pass against the implementations it
    describes. A failure here is either a bug in the core or a bug in the kit — and the kit
    saying so about the reference extractor is what makes it trustworthy about anybody else's.
    """
    with live_instance(identity_settings) as instance, _signed_in(instance.base_url) as admin:
        workspace = _active_workspace(admin, "Conformance")
        token = _provision(admin)
        worker, thread = _running(instance.base_url, token, "verify")
        try:
            with Conformance(
                instance.base_url,
                email=ADMIN_EMAIL,
                password=ADMIN_PASSWORD,
                workspace=workspace,
                timeout=_TIMEOUT,
            ) as conformance:
                report = run_checks(conformance, extractor_id=EXTRACTOR_ID)
        finally:
            worker.stop()
            thread.join(timeout=10)

        failures = [check for check in report.checks if check.outcome == "fail"]
        assert not failures, report.render()
        # A kit that skipped everything would also have no failures.
        assert sum(1 for check in report.checks if check.outcome == "pass") >= 10, report.render()
