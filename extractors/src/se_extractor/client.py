"""The six calls an extractor makes, and nothing else.

Synchronous on purpose. An extractor's work is a blocking, CPU-bound thing — a subprocess, a
model, a page render — so `async` would buy nothing and cost every author an event loop they did
not ask for. The one thing that genuinely has to happen *while* work is in progress is the
heartbeat, and that is a thread (`loop.py`).

Errors are typed by what the caller should do about them, which is the only distinction that
matters at three in the morning:

- `LeaseLost` — stop. Something else owns this job now, and every write will be refused.
- `ContractError` — this request was wrong and repeating it will not help.
- `Unavailable` — the core could not be reached or could not answer. Back off and retry.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from se_extractor.models import Heartbeat, Job, Registration

#: Generous: a claim may long-poll for up to 30 s, and a large input takes as long as it takes.
DEFAULT_TIMEOUT = httpx.Timeout(60.0, read=300.0)

_CHUNK = 1024 * 1024


class ExtractorError(Exception):
    """Base for everything this client raises, so a loop can catch one thing."""


class LeaseLost(ExtractorError):  # noqa: N818 - a state the caller is in, not a fault to fix
    """This claim is no longer current: stop working and claim again."""


class ContractError(ExtractorError):
    """The core refused the request itself — a bad manifest, a job that is not ours."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class Unavailable(ExtractorError):  # noqa: N818 - names the condition, as the stdlib does
    """The core could not be reached or could not answer. Retryable, after a wait."""


class ExtractorClient:
    """A client bound to one instance and one extractor credential."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        api_path: str = "/extractor-api/v1",
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._api = api_path.rstrip("/")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ExtractorClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ registration

    def register(self, manifest: dict[str, Any]) -> Registration:
        """Declare what this extractor can do. Idempotent; call it on every start-up."""
        return Registration.of(self._json("PUT", f"{self._api}/registration", json=manifest))

    # ------------------------------------------------------------------ the job's life

    def claim(self, *, wait: int = 0, worker: str | None = None) -> Job | None:
        """Claim the next job, or `None` when there is none.

        `wait` long-polls for up to that many seconds — the core bounds it, and waiting there
        costs nothing, so a loop should prefer one long claim over many short ones.
        """
        response = self._request(
            "POST",
            f"{self._api}/jobs/claim",
            params={"wait": wait} if wait else None,
            json={"worker": worker} if worker else {},
        )
        if response.status_code == 204:
            return None
        return Job.of(self._decoded(response))

    def heartbeat(self, job: Job) -> Heartbeat:
        """Keep the lease, and find out whether to stop."""
        return Heartbeat.of(
            self._json(
                "POST", f"{self._api}/jobs/{job.id}/heartbeat", json={"attempt": job.attempt}
            )
        )

    def submit(self, job: Job, result: dict[str, Any] | None = None) -> dict[str, Any]:
        """Finish the job. The envelope carries whatever outputs the contract version accepts."""
        return self._json(
            "POST",
            f"{self._api}/jobs/{job.id}/result",
            json={**(result or {}), "attempt": job.attempt},
        )

    def report_error(self, job: Job, message: str, *, retryable: bool = True) -> dict[str, Any]:
        """Report a failed attempt. `retryable=False` for input this extractor can never read."""
        return self._json(
            "POST",
            f"{self._api}/jobs/{job.id}/error",
            json={"attempt": job.attempt, "message": message[:2000], "retryable": retryable},
        )

    # ------------------------------------------------------------------ the bytes

    def read_input(self, job: Job, index: int = 0) -> bytes:
        """The whole of one input, in memory. Fine for documents; not for video."""
        return self._request("GET", f"{self._api}/jobs/{job.id}/inputs/{index}").content

    def download_input(self, job: Job, destination: Path, index: int = 0) -> Path:
        """Stream one input to a file, so an extractor never holds a whole video in memory."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.stream_input(job, index) as chunks, destination.open("wb") as sink:
            for chunk in chunks:
                sink.write(chunk)
        return destination

    @contextmanager
    def stream_input(self, job: Job, index: int = 0) -> Generator[Iterator[bytes]]:
        """One input as a stream of chunks."""
        try:
            with self._http.stream("GET", f"{self._api}/jobs/{job.id}/inputs/{index}") as response:
                self._raise_for(response, read_body=True)
                yield response.iter_bytes(_CHUNK)
        except httpx.HTTPError as unreachable:
            raise Unavailable(str(unreachable)) from unreachable

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as unreachable:
            raise Unavailable(f"{method} {url}: {unreachable}") from unreachable
        self._raise_for(response)
        return response

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return self._decoded(self._request(method, url, **kwargs))

    @staticmethod
    def _decoded(response: httpx.Response) -> dict[str, Any]:
        try:
            document = response.json()
        except ValueError as malformed:
            raise Unavailable(
                f"the core answered {response.status_code} with no JSON"
            ) from malformed
        return document if isinstance(document, dict) else {}

    @staticmethod
    def _raise_for(response: httpx.Response, *, read_body: bool = False) -> None:
        if response.status_code < 400:
            return
        if read_body:
            response.read()
        if response.status_code == 409 and _problem_type(response).endswith("/lease-lost"):
            raise LeaseLost(_detail(response))
        if response.status_code >= 500:
            raise Unavailable(f"the core answered {response.status_code}")
        raise ContractError(_detail(response), status=response.status_code)


def _problem_type(response: httpx.Response) -> str:
    """The problem's `type` URI, or the empty string — the envelope is RFC 9457 (08 § errors)."""
    try:
        document = response.json()
    except ValueError:
        return ""
    return str(document.get("type", "")) if isinstance(document, dict) else ""


def _detail(response: httpx.Response) -> str:
    try:
        document = response.json()
    except ValueError:
        return f"{response.status_code} {response.reason_phrase}"
    if isinstance(document, dict):
        detail = document.get("detail") or document.get("title")
        if isinstance(detail, str):
            return detail
    return f"{response.status_code} {response.reason_phrase}"
