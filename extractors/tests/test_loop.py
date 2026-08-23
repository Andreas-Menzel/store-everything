"""The loop's behaviour, against a scripted core.

A mock transport rather than a real instance on purpose: what these tests are about is what the
loop does with the *answers* — a core that is not up yet, a lease lost mid-job, a cancellation
arriving on a heartbeat — and those are answers, not databases. The loop against a real core is
`server/tests/test_reference_extractor.py`, where the reference extractor earns its third job.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from se_extractor import (
    Cancelled,
    ExtractorClient,
    Job,
    JobContext,
    LeaseLost,
    PermanentFailure,
    Worker,
)

MANIFEST: dict[str, Any] = {
    "id": "probe",
    "version": "1.0.0",
    "api_version": "v1",
    "accepts": {"mime_types": ["*/*"]},
    "produces": ["metadata"],
}

JOB: dict[str, Any] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "attempt": 1,
    "idempotency_key": "extract:v:probe:1.0.0:-:1",
    "extractor_id": "probe",
    "generation": 1,
    "params": {},
    "lease_expires_at": "2026-08-23T12:00:00Z",
    "heartbeat_interval_seconds": 2,
    "cancel_requested": False,
    "file_version": {
        "id": "22222222-2222-2222-2222-222222222222",
        "content_hash": "a" * 64,
        "size": 5,
        "media_type": "text/plain",
        "media_class": "document",
        "is_current": True,
    },
    "inputs": [
        {
            "index": 0,
            "kind": "original",
            "url": "/extractor-api/v1/jobs/11111111-1111-1111-1111-111111111111/inputs/0",
            "media_type": "text/plain",
            "size": 5,
            "content_hash": "a" * 64,
        }
    ],
}


class Core:
    """A core that answers from a script, and remembers what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict[str, Any]] = []
        self.routes: dict[str, list[httpx.Response]] = {}

    def on(self, path: str, *responses: httpx.Response) -> Core:
        self.routes.setdefault(path, []).extend(responses)
        return self

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.method, request.url.path))
            if request.content:
                with contextlib.suppress(ValueError):  # the tests only ever send JSON
                    self.bodies.append(json.loads(request.content))
            queued = self.routes.get(request.url.path)
            if not queued:
                return httpx.Response(404, json=_problem(404, "not-found"))
            # The last answer repeats, so a heartbeat loop does not have to be scripted N times.
            return queued.pop(0) if len(queued) > 1 else queued[0]

        return httpx.MockTransport(handle)

    def client(self) -> ExtractorClient:
        return ExtractorClient("http://core", "seext_probe", transport=self.transport())

    def paths(self) -> list[str]:
        return [path for _, path in self.calls]


def _problem(status: int, slug: str, detail: str = "") -> dict[str, Any]:
    return {
        "type": f"https://docs.store-everything.example/errors/{slug}",
        "title": slug,
        "status": status,
        "detail": detail,
    }


def _registered(**overrides: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "extractor_id": "probe",
            "changed": True,
            "enabled": True,
            "manifest": MANIFEST,
            **overrides,
        },
    )


def _worker(core: Core, handler: Callable[[Job, JobContext], dict[str, Any] | None]) -> Worker:
    return Worker(core.client(), MANIFEST, handler, claim_wait=0, sleep=lambda _seconds: None)


def test_a_registered_extractor_claims_works_and_submits() -> None:
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(200, json=JOB))
    core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/result",
        httpx.Response(200, json={"id": JOB["id"], "state": "succeeded", "finished_at": None}),
    )
    seen: list[Job] = []

    _worker(core, lambda job, _context: seen.append(job) or None).run(once=True)

    assert [job.id for job in seen] == [JOB["id"]]
    assert seen[0].attempt == 1
    assert seen[0].original is not None
    assert core.paths() == [
        "/extractor-api/v1/registration",
        "/extractor-api/v1/jobs/claim",
        f"/extractor-api/v1/jobs/{JOB['id']}/result",
    ]
    # The fencing token goes back with the result, unchanged.
    assert core.bodies[-1]["attempt"] == 1


def test_registration_waits_for_a_core_that_is_not_up_yet() -> None:
    """A container starts before the instance does, and that is not an error."""
    core = Core()
    core.on(
        "/extractor-api/v1/registration",
        httpx.Response(503, json=_problem(503, "service-not-ready")),
        httpx.Response(503, json=_problem(503, "service-not-ready")),
        _registered(),
    )
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(204))

    _worker(core, lambda _job, _context: None).run(once=True)

    assert core.paths().count("/extractor-api/v1/registration") == 3


def test_a_manifest_the_core_refuses_is_not_retried_forever() -> None:
    """A refused manifest will be refused again; spinning on it hides the message that says why."""
    core = Core()
    core.on(
        "/extractor-api/v1/registration",
        httpx.Response(422, json=_problem(422, "validation", "produces is empty")),
    )
    worker = _worker(core, lambda _job, _context: None)

    with pytest.raises(Exception, match="produces is empty"):
        worker.register()

    assert core.paths().count("/extractor-api/v1/registration") == 1


def test_an_idle_queue_is_not_a_failure() -> None:
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(204))
    called: list[Job] = []

    _worker(core, lambda job, _context: called.append(job) or None).run(once=True)

    assert called == []


def test_a_handlers_failure_is_reported_as_retryable() -> None:
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(200, json=JOB))
    core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/error",
        httpx.Response(200, json={"id": JOB["id"], "state": "queued", "finished_at": None}),
    )

    def explode(_job: Job, _context: JobContext) -> None:
        raise RuntimeError("the model would not load")

    _worker(core, explode).run(once=True)

    assert core.paths()[-1] == f"/extractor-api/v1/jobs/{JOB['id']}/error"
    assert core.bodies[-1]["retryable"] is True
    assert "the model would not load" in core.bodies[-1]["message"]


def test_a_permanent_failure_skips_the_retries() -> None:
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(200, json=JOB))
    core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/error",
        httpx.Response(200, json={"id": JOB["id"], "state": "failed", "finished_at": None}),
    )

    def hopeless(_job: Job, _context: JobContext) -> None:
        raise PermanentFailure("this is not a PDF")

    _worker(core, hopeless).run(once=True)

    assert core.bodies[-1]["retryable"] is False
    assert core.bodies[-1]["message"] == "this is not a PDF"


def test_a_job_already_cancelled_when_claimed_is_left_alone() -> None:
    """Superseded between being queued and being claimed: nothing to do, nothing to report."""
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on(
        "/extractor-api/v1/jobs/claim",
        httpx.Response(200, json={**JOB, "cancel_requested": True}),
    )
    called: list[Job] = []

    _worker(core, lambda job, _context: called.append(job) or None).run(once=True)

    assert called == []
    assert core.paths() == ["/extractor-api/v1/registration", "/extractor-api/v1/jobs/claim"]


def test_a_result_submitted_after_the_lease_lapsed_is_not_an_exception() -> None:
    """The work is lost, not the loop: the job comes back and is done again."""
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(200, json=JOB))
    core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/result",
        httpx.Response(409, json=_problem(409, "lease-lost", "the lease expired")),
    )

    _worker(core, lambda _job, _context: None).run(once=True)

    assert core.paths()[-1] == f"/extractor-api/v1/jobs/{JOB['id']}/result"


def test_the_handler_is_told_to_stop_when_the_core_says_so() -> None:
    """The heartbeat is the cancellation channel, and a handler is asked rather than killed."""
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(200, json=JOB))
    core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/heartbeat",
        httpx.Response(200, json={"lease_expires_at": "2026-08-23T12:05:00Z", "cancel": True}),
    )
    core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/result",
        httpx.Response(200, json={"id": JOB["id"], "state": "succeeded", "finished_at": None}),
    )
    stopped: list[bool] = []

    def patient(_job: Job, context: JobContext) -> None:
        # What a real handler does between units of work: ask, and give up when told.
        for _ in range(60):
            try:
                context.raise_if_cancelled()
            except Cancelled:
                stopped.append(True)
                raise
            time.sleep(0.05)

    _worker(core, patient).run(once=True)

    assert stopped == [True], "the handler was never told to stop"
    # Cancellation is not a failure: nothing was reported and nothing was submitted.
    assert f"/extractor-api/v1/jobs/{JOB['id']}/error" not in core.paths()
    assert f"/extractor-api/v1/jobs/{JOB['id']}/result" not in core.paths()


def test_the_input_is_read_through_the_job_reference() -> None:
    core = Core()
    core.on("/extractor-api/v1/registration", _registered())
    core.on("/extractor-api/v1/jobs/claim", httpx.Response(200, json=JOB))
    core.on(f"/extractor-api/v1/jobs/{JOB['id']}/inputs/0", httpx.Response(200, content=b"hello"))
    core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/result",
        httpx.Response(200, json={"id": JOB["id"], "state": "succeeded", "finished_at": None}),
    )
    read: list[bytes] = []

    _worker(core, lambda job, context: read.append(context.client.read_input(job)) or None).run(
        once=True
    )

    assert read == [b"hello"]


def test_a_job_for_somebody_else_is_a_contract_error() -> None:
    core = Core()
    client = core.on(
        f"/extractor-api/v1/jobs/{JOB['id']}/heartbeat",
        httpx.Response(404, json=_problem(404, "not-found")),
    ).client()

    with pytest.raises(Exception) as refused:
        client.heartbeat(Job.of(JOB))

    assert not isinstance(refused.value, LeaseLost), "404 is not a lost lease"
