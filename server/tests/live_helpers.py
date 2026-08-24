"""Talking to a live instance the way a client does, for the tests that run real extractors.

`live_instance` gives you a server and a worker; this gives you the rest of the arrangement — an
administrator with a real cookie, a workspace that finished provisioning, an extractor credential,
and the polling that "wait until the queue has done something" actually requires.

It exists because three suites need the same twenty lines: the reference extractor's conformance
tests, the document-text tests, and the phase-2 walkthrough. Sharing them keeps the *interesting*
part of each of those files visible, and puts the one thing that is easy to get wrong — poll slower
than the API's rate limit — in one place.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import httpx
from se_extractor import ExtractorClient, Worker
from se_extractor.loop import JobContext
from se_extractor.models import Job

from store_everything.api import API_V1_PREFIX
from tests.identity_helpers import ADMIN_EMAIL, ADMIN_PASSWORD

#: Generous, because a step here can involve a container rendering a 300-dpi page.
TIMEOUT = 60.0

#: Slow enough to stay under the API's own rate limit. Workers long-polling for jobs and a test
#: polling for their results share one credential's budget, and a tenth-of-a-second loop spends it
#: inside a minute — which surfaces as a `429` that looks like a bug and is not one.
POLL = 0.25

#: For work measured in seconds per page, where checking four times a second buys nothing.
SLOW_POLL = 1.0

#: Where a run stops moving. `dead_letter` belongs here: a job that exhausted its attempts is a
#: finished job, and a test that kept waiting for it would report a timeout instead of the reason.
SETTLED = frozenset({"succeeded", "failed", "dead_letter"})

Handler = Callable[[Job, JobContext], dict[str, Any] | None]


@contextmanager
def signed_in(base_url: str) -> Generator[httpx.Client]:
    """An administrator's client against the live instance — a real cookie over a real socket."""
    with httpx.Client(base_url=base_url, headers={"Origin": base_url}, timeout=30.0) as client:
        response = client.post(
            f"{API_V1_PREFIX}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200, response.text
        yield client


def active_workspace(client: httpx.Client, name: str = "Documents") -> str:
    """A workspace that finished provisioning — which is an operation, so a worker did it."""
    created = client.post(f"{API_V1_PREFIX}/workspaces", json={"name": name})
    assert created.status_code == 201, created.text
    identifier = str(created.json()["id"])

    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if client.get(f"{API_V1_PREFIX}/workspaces/{identifier}").json().get("state") == "active":
            return identifier
        time.sleep(POLL)
    raise AssertionError("the workspace never became active — did the worker start?")


def provision(client: httpx.Client, extractor_id: str) -> str:
    """Mint an extractor credential the way an operator does, and return the token."""
    response = client.post(f"{API_V1_PREFIX}/extractors", json={"id": extractor_id})
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


def upload(
    client: httpx.Client,
    workspace: str,
    path: str,
    body: bytes,
    media_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """One complete upload in one request — the small-file path of the resumable protocol."""
    response = client.post(
        f"{API_V1_PREFIX}/workspaces/{workspace}/files",
        params={"path": path},
        content=body,
        headers={"upload-complete": "?1", "Content-Type": media_type},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


@contextmanager
def running(
    base_url: str, token: str, manifest: dict[str, Any], handle: Handler
) -> Generator[None]:
    """One extractor, as its own thread of control against a real socket."""
    client = ExtractorClient(base_url, token)
    worker = Worker(client, manifest, handle, claim_wait=1, worker_name="test")
    thread = threading.Thread(target=worker.run, name=str(manifest.get("id")), daemon=True)
    thread.start()
    try:
        yield
    finally:
        worker.stop()
        # Long enough for a job in flight to finish: `stop` is checked between jobs, and one OCR
        # page is seconds. A worker that outlived its test would keep talking to an instance that
        # is being torn down, which is a confusing failure in whatever test runs next.
        thread.join(timeout=30)
        assert not thread.is_alive(), f"{manifest.get('id')} did not stop"
        client.close()


def await_status(client: httpx.Client, file_id: str, wanted: str) -> dict[str, Any]:
    """Wait for the file's overall extraction status to reach `wanted`."""
    deadline = time.monotonic() + TIMEOUT
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = extraction(client, file_id)
        if last["status"] == wanted:
            return last
        time.sleep(POLL)
    raise AssertionError(f"extraction stayed {last.get('status')!r} instead of reaching {wanted!r}")


def await_run(client: httpx.Client, file_id: str, extractor: str) -> dict[str, Any]:
    """Wait for one extractor's run to settle, whatever else is happening to the file.

    Not `await_status`: a file several extractors are interested in reaches "indexed" when the
    first of them is done, and a chained extractor's run is the thing worth waiting for.
    """
    deadline = time.monotonic() + TIMEOUT
    seen: list[str] = []
    while time.monotonic() < deadline:
        runs = extraction(client, file_id)["runs"]
        seen = [f"{run['extractor']}:{run['state']}" for run in runs]
        for run in runs:
            if run["extractor"] == extractor and run["state"] in SETTLED:
                return dict(run)
        time.sleep(SLOW_POLL)
    raise AssertionError(f"{extractor} never ran; the file had {seen}")


def extraction(client: httpx.Client, file_id: str) -> dict[str, Any]:
    response = client.get(f"{API_V1_PREFIX}/files/{file_id}/extraction")
    assert response.status_code == 200, response.text
    return dict(response.json())


def segments(client: httpx.Client, file_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{API_V1_PREFIX}/files/{file_id}/segments", params={"limit": 100})
    response.raise_for_status()
    return list(response.json()["data"])


def facts(client: httpx.Client, file_id: str) -> dict[str, Any]:
    """Typed metadata as a plain mapping, which is how a test wants to read it."""
    response = client.get(f"{API_V1_PREFIX}/files/{file_id}/metadata")
    response.raise_for_status()
    return {entry["key"]: entry["value"] for entry in response.json()}
