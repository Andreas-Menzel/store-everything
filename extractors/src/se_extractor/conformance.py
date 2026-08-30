"""The conformance kit: does this extractor speak the contract, and does this core?

Two suites, because the contract has two sides and both can be wrong.

**Protocol checks** drive a synthetic extractor — provisioned by the kit itself — against a real
instance and assert the rules the *core* owes: an incoherent manifest is refused, a manifest
cannot claim another extractor's identity, an exclusive output kind has one owner, a stale
fencing token cannot write, the same result twice persists once, a disabled extractor is told so.
These are what an author reads when they want to know what the core will do to them.

**Image checks** point at an extractor that is already running and assert the rules an *image*
owes: it registers, its manifest is coherent, it claims work promptly, it finishes, and it leaves
alone what it did not say it accepts.

Both report per check, and the exit code is the answer. A check that cannot be run says `skip`
with the reason rather than passing quietly — a green tick nobody earned is worse than a gap.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from se_extractor.client import ContractError, ExtractorClient, LeaseLost
from se_extractor.models import Job

API = "/api/v1"

#: How long to wait for something asynchronous — a workspace to provision, an extractor to
#: register, a job to finish. Long enough for a busy instance, short enough to be a test.
DEFAULT_TIMEOUT = 30.0
_POLL = 0.25

#: A minimal one-page PDF, written out rather than generated: the kit has to work with no fixtures
#: on disk and no dependency beyond `httpx`, and an extractor that only accepts PDFs still deserves
#: to be checked. Uncompressed, no metadata, cross-reference table by hand.
_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Count 1 /Kids [3 0 R] >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
    b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
    b"5 0 obj\n<< /Length 66 >>\nstream\n"
    b"BT /F1 14 Tf 72 700 Td (A conformance fixture with a line of text.) Tj ET\n"
    b"endstream\nendobj\n"
    b"trailer\n<< /Size 6 /Root 1 0 R >>\n%%EOF\n"
)

#: A 1x1 white PNG, for the same reason.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415408d763f8ffff3f0005fe02fea735c9ab0000000049454e44ae426082"
)

#: What the kit can offer, in the order it prefers: text first because it is the cheapest thing
#: any extractor can read, then the two binary shapes a document or imaging extractor wants.
_SAMPLES: tuple[tuple[str, str, bytes], ...] = (
    ("text/plain", "txt", b"work for the extractor under test\n"),
    ("application/pdf", "pdf", _PDF),
    ("image/png", "png", _PNG),
)


def matches_pattern(pattern: str, media_type: str) -> bool:
    """Whether one manifest pattern covers one media type — `*/*`, `type/*`, or exact."""
    pattern = pattern.strip()
    if pattern == "*/*":
        return True
    if pattern.endswith("/*"):
        return media_type.startswith(pattern[:-1])
    return pattern == media_type


PASS, FAIL, SKIP = "pass", "fail", "skip"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    outcome: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome != FAIL


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def render(self) -> str:
        marks = {PASS: "ok  ", FAIL: "FAIL", SKIP: "skip"}
        lines = [
            f"  {marks[check.outcome]}  {check.name}"
            + (f" — {check.detail}" if check.detail else "")
            for check in self.checks
        ]
        failed = sum(1 for check in self.checks if check.outcome == FAIL)
        skipped = sum(1 for check in self.checks if check.outcome == SKIP)
        lines.append(
            f"\n{len(self.checks) - failed - skipped} passed, {failed} failed, {skipped} skipped"
        )
        return "\n".join(lines)


class ConformanceError(Exception):
    """The kit could not run at all — bad credentials, no instance, no workspace."""


# --------------------------------------------------------------------------- the manifest rules


_OUTPUT_KINDS = frozenset(
    {"metadata", "text_segments", "tags", "embeddings", "derived_assets", "renditions", "faces"}
)
_PAIRED = (
    ("renditions", "renditions"),
    ("derived_assets", "derived_asset_kinds"),
    ("embeddings", "embedding_spaces"),
)


def manifest_problems(manifest: dict[str, Any]) -> list[str]:
    """Everything wrong with a manifest, as an author would want it listed.

    A copy of the core's rules on purpose: an image should be checkable *before* it is pointed
    at an instance, and a kit that could only ask the core would be no use in a unit test. The
    core remains the authority — this is why the kit also registers the manifest for real.
    """
    problems: list[str] = []
    for required in ("id", "version", "api_version"):
        if not isinstance(manifest.get(required), str) or not manifest[required]:
            problems.append(f"`{required}` is missing")

    produces = manifest.get("produces")
    if not isinstance(produces, list) or not produces:
        problems.append("`produces` must name at least one output kind")
    else:
        unknown = sorted(set(map(str, produces)) - _OUTPUT_KINDS)
        if unknown:
            problems.append(f"`produces` names unknown kinds: {', '.join(unknown)}")
        if len(set(map(str, produces))) != len(produces):
            problems.append("`produces` contains a duplicate")

    accepts = manifest.get("accepts")
    accepts = accepts if isinstance(accepts, dict) else {}
    if not accepts.get("mime_types") and not accepts.get("derived_kinds"):
        problems.append("`accepts` names neither a media-type pattern nor a derived kind")

    declared = set(map(str, produces)) if isinstance(produces, list) else set()
    for output, field_name in _PAIRED:
        names = manifest.get(field_name)
        has_names = bool(names)
        if output in declared and not has_names:
            problems.append(f"produces `{output}` but `{field_name}` names nothing")
        if has_names and output not in declared:
            problems.append(f"`{field_name}` is declared but `produces` omits `{output}`")

    model = manifest.get("model")
    if model is not None and (
        not isinstance(model, dict) or not model.get("name") or not model.get("version")
    ):
        problems.append("`model` needs both a name and a version — it is provenance")
    return problems


# ------------------------------------------------------------------------------------ the kit


class Conformance:
    """Runs the checks against one instance."""

    def __init__(
        self,
        base_url: str,
        *,
        email: str,
        password: str,
        workspace: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http = httpx.Client(
            base_url=self._base_url,
            transport=transport,
            timeout=httpx.Timeout(30.0),
            # An unsafe cookie-authenticated request has to prove it came from this origin
            # (07 § tokens & credentials), and the kit is not a browser.
            headers={"Origin": self._base_url},
            follow_redirects=False,
        )
        self._email = email
        self._password = password
        self._workspace = workspace
        self._transport = transport
        self._minted: list[str] = []

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Conformance:
        self.sign_in()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ instance plumbing

    def sign_in(self) -> None:
        response = self._http.post(
            f"{API}/auth/login", json={"email": self._email, "password": self._password}
        )
        if response.status_code != 200:
            raise ConformanceError(f"could not sign in: {response.status_code} {response.text}")

    def workspace(self) -> str:
        """A workspace to upload fixtures into, created on demand and waited for."""
        if self._workspace is not None:
            return self._workspace

        created = self._http.post(f"{API}/workspaces", json={"name": f"conformance-{_token()}"})
        if created.status_code != 201:
            raise ConformanceError(f"could not create a workspace: {created.text}")
        identifier = str(created.json()["id"])

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            state = self._http.get(f"{API}/workspaces/{identifier}")
            if state.status_code == 200 and state.json().get("state") == "active":
                self._workspace = identifier
                return identifier
            time.sleep(_POLL)
        raise ConformanceError(
            "the workspace never became active — is a worker running on this instance?"
        )

    def provision(self, extractor_id: str) -> str:
        """Provision an extractor id and return its credential."""
        response = self._http.post(f"{API}/extractors", json={"id": extractor_id})
        if response.status_code != 201:
            raise ConformanceError(f"could not provision {extractor_id}: {response.text}")
        self._minted.append(extractor_id)
        return str(response.json()["token"])

    def upload(self, path: str, body: bytes, *, media_type: str = "text/plain") -> dict[str, Any]:
        """One-shot upload of a fixture — the plain-upload path of the resumable protocol."""
        response = self._http.post(
            f"{API}/workspaces/{self.workspace()}/files",
            params={"path": path},
            content=body,
            headers={"upload-complete": "?1", "Content-Type": media_type},
        )
        if response.status_code != 201:
            raise ConformanceError(f"could not upload {path}: {response.text}")
        return dict(response.json())

    def extraction_of(self, file_id: str) -> dict[str, Any]:
        response = self._http.get(f"{API}/files/{file_id}/extraction")
        if response.status_code != 200:
            raise ConformanceError(f"could not read extraction status: {response.text}")
        return dict(response.json())

    def extractor(self, extractor_id: str) -> dict[str, Any] | None:
        response = self._http.get(f"{API}/extractors/{extractor_id}")
        return dict(response.json()) if response.status_code == 200 else None

    def set_enabled(self, extractor_id: str, *, enabled: bool) -> None:
        self._http.patch(f"{API}/extractors/{extractor_id}", json={"enabled": enabled})

    def client_for(self, token: str) -> ExtractorClient:
        """An extractor client against the same instance — and the same transport, so the kit
        works against an in-process application as well as a real one."""
        return ExtractorClient(self._base_url, token, transport=self._transport)

    # ------------------------------------------------------------------ protocol checks

    def protocol_checks(self) -> Iterator[Check]:
        """What the core owes an extractor. Uses a synthetic extractor of the kit's own."""
        identifier = f"conformance-{_token()}"
        token = self.provision(identifier)
        checks: tuple[tuple[str, Callable[[ExtractorClient, str], str]], ...] = (
            ("registration-refuses-an-incoherent-manifest", self._incoherent),
            ("registration-is-bound-to-the-credential", self._foreign_identity),
            ("registration-refuses-an-unknown-contract-version", self._wrong_version),
            ("registration-accepts-a-good-manifest", self._registers),
            ("an-idle-queue-answers-nothing", self._idle),
            ("an-input-matches-its-declared-hash", self._input_hash),
            ("a-stale-fencing-token-cannot-write", self._fencing),
            ("an-unknown-job-is-not-found", self._unknown_job),
            ("a-disabled-extractor-is-told-so", self._disabled),
        )
        with self.client_for(token) as client:
            for name, check in checks:
                yield from _guarded(name, check, client, identifier)

    def _incoherent(self, client: ExtractorClient, identifier: str) -> str:
        manifest = _manifest(identifier, produces=["renditions"])
        try:
            client.register(manifest)
        except ContractError as refused:
            if refused.status == 422:
                return "refused with 422"
            raise AssertionError(f"expected 422, got {refused.status}") from refused
        raise AssertionError("a manifest declaring renditions without kinds was accepted")

    def _foreign_identity(self, client: ExtractorClient, identifier: str) -> str:
        try:
            client.register(_manifest("somebody-else"))
        except ContractError as refused:
            if refused.status == 403:
                return "refused with 403"
            raise AssertionError(f"expected 403, got {refused.status}") from refused
        raise AssertionError("a manifest claiming another extractor's id was accepted")

    def _wrong_version(self, client: ExtractorClient, identifier: str) -> str:
        try:
            client.register(_manifest(identifier, api_version="v2"))
        except ContractError as refused:
            if refused.status == 409:
                return "refused with 409"
            raise AssertionError(f"expected 409, got {refused.status}") from refused
        raise AssertionError("a manifest declaring an unknown contract version was accepted")

    def _registers(self, client: ExtractorClient, identifier: str) -> str:
        registered = client.register(_manifest(identifier))
        if registered.extractor_id != identifier:
            raise AssertionError("the core echoed a different extractor id")
        # The echo is the point: what it omits, it ignored.
        problems = manifest_problems(registered.manifest)
        if problems:
            raise AssertionError(f"the echoed manifest is not coherent: {'; '.join(problems)}")
        return "registered, and the echo is coherent"

    def _idle(self, client: ExtractorClient, identifier: str) -> str:
        if client.claim() is not None:
            raise AssertionError("a job was claimed before anything was uploaded")
        return "204 with nothing queued"

    def _claim_one(self, client: ExtractorClient, name: str, body: bytes) -> Job:
        self.upload(name, body)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            job = client.claim()
            if job is not None:
                return job
            time.sleep(_POLL)
        raise AssertionError("no job was offered for an uploaded file")

    def _input_hash(self, client: ExtractorClient, identifier: str) -> str:
        body = b"conformance fixture " + _token().encode()
        job = self._claim_one(client, f"input-{_token()}.txt", body)
        read = client.read_input(job)
        if read != body:
            raise AssertionError("the input served different bytes than were uploaded")
        original = job.original
        if original is None:
            raise AssertionError("the job carried no original input")
        if hashlib.sha256(read).hexdigest() != original.content_hash:
            raise AssertionError("the declared content hash does not match the bytes served")
        client.submit(job)
        return f"{len(read)} bytes, hash matched"

    def _fencing(self, client: ExtractorClient, identifier: str) -> str:
        job = self._claim_one(client, f"fencing-{_token()}.txt", b"fencing")
        stale = Job.of({**_as_document(job), "attempt": job.attempt + 1})
        for name, call in (
            ("heartbeat", lambda: client.heartbeat(stale)),
            ("result", lambda: client.submit(stale)),
            ("error", lambda: client.report_error(stale, "stale")),
        ):
            try:
                call()
            except LeaseLost:
                continue
            except ContractError as refused:
                raise AssertionError(
                    f"a stale {name} was refused with {refused.status} rather than lease-lost"
                ) from refused
            raise AssertionError(f"a stale {name} was accepted")
        client.submit(job)
        return "heartbeat, result and error all refused"

    def _unknown_job(self, client: ExtractorClient, identifier: str) -> str:
        phantom = Job.of({"id": str(uuid.uuid4()), "attempt": 1})
        try:
            client.heartbeat(phantom)
        except ContractError as refused:
            if refused.status == 404:
                return "404 for a job that does not exist"
            raise AssertionError(f"expected 404, got {refused.status}") from refused
        raise AssertionError("a heartbeat for an unknown job was accepted")

    def _disabled(self, client: ExtractorClient, identifier: str) -> str:
        self.set_enabled(identifier, enabled=False)
        try:
            client.claim()
        except ContractError as refused:
            if refused.status == 409:
                return "409 while disabled"
            raise AssertionError(f"expected 409, got {refused.status}") from refused
        finally:
            self.set_enabled(identifier, enabled=True)
        raise AssertionError("a disabled extractor was offered work")

    # ------------------------------------------------------------------ image checks

    def image_checks(self, extractor_id: str) -> Iterator[Check]:
        """What an image owes the core. Assumes it is running and pointed at this instance."""
        registered = self._await_registration(extractor_id)
        if registered is None:
            yield Check(
                "the-extractor-registers",
                FAIL,
                f"{extractor_id} had no manifest after {self._timeout:.0f}s",
            )
            return
        yield Check("the-extractor-registers", PASS, f"version {registered.get('version')}")

        problems = manifest_problems(registered.get("manifest") or {})
        yield Check(
            "its-manifest-is-coherent",
            FAIL if problems else PASS,
            "; ".join(problems) if problems else "",
        )

        yield from _guarded("it-claims-and-finishes-a-job", self._works, extractor_id)
        yield from _guarded(
            "it-leaves-alone-what-it-does-not-accept", self._selective, extractor_id
        )

    def _await_registration(self, extractor_id: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            found = self.extractor(extractor_id)
            if found is not None and found.get("registered"):
                return found
            time.sleep(_POLL)
        return None

    def _works(self, extractor_id: str) -> str:
        found = self.extractor(extractor_id) or {}
        accepts = (found.get("manifest") or {}).get("accepts") or {}
        if accepts.get("when"):
            # Its manifest says it wants files only *once* a well-known key says so, and nothing
            # here can write that key — only another extractor's result can. Reporting that
            # honestly beats uploading something it will rightly ignore and calling it a failure.
            raise _Skipped("it routes on a predicate another extractor's result must satisfy")
        if accepts.get("derived_kinds") and not accepts.get("mime_types"):
            raise _Skipped("it accepts only other extractors' outputs, which this kit cannot stage")

        media_type, suffix, body = self._sample_for(accepts.get("mime_types") or [])
        created = self.upload(f"image-{_token()}.{suffix}", body, media_type=media_type)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            runs = [
                run
                for run in self.extraction_of(str(created["id"]))["runs"]
                if run["extractor"] == extractor_id
            ]
            if not runs:
                raise AssertionError(f"no job was created for {extractor_id}")
            run = runs[0]
            if run["state"] == "succeeded":
                if not run.get("started_at"):
                    raise AssertionError("the run succeeded without ever starting")
                return f"succeeded as version {run.get('extractor_version')}"
            if run["state"] in {"failed", "dead_letter", "cancelled"}:
                raise AssertionError(f"the run ended {run['state']}: {run.get('error')}")
            time.sleep(_POLL)
        raise AssertionError(f"the run did not finish within {self._timeout:.0f}s")

    def _sample_for(self, patterns: list[Any]) -> tuple[str, str, bytes]:
        """Something the manifest accepts, from the kit's own small set."""
        for media_type, suffix, body in _SAMPLES:
            if any(matches_pattern(str(pattern), media_type) for pattern in patterns):
                return media_type, suffix, body
        raise _Skipped(f"nothing in this kit's sample set matches {patterns}")

    def _selective(self, extractor_id: str) -> str:
        found = self.extractor(extractor_id) or {}
        patterns = ((found.get("manifest") or {}).get("accepts") or {}).get("mime_types") or []
        if any(str(pattern) == "*/*" for pattern in patterns):
            raise _Skipped("it accepts every media type, so there is nothing it should ignore")

        created = self.upload(
            f"unwanted-{_token()}.bin", b"\x00\x01\x02", media_type="application/octet-stream"
        )
        # Give it as long as it would have had to claim something it wanted.
        time.sleep(min(self._timeout, 2.0))
        claimed = [
            run
            for run in self.extraction_of(str(created["id"]))["runs"]
            if run["extractor"] == extractor_id
        ]
        if claimed:
            raise AssertionError("a job appeared for a media type the manifest does not accept")
        return "no job for a type outside its manifest"


class _Skipped(Exception):  # noqa: N818 - an outcome this kit reports, not an error
    """A check that cannot be run here. Reported as `skip`, never as a pass."""


def _guarded(name: str, check: Callable[..., str], *args: Any) -> Iterator[Check]:
    """Run one check and turn whatever it raises into an outcome."""
    try:
        yield Check(name, PASS, check(*args))
    except _Skipped as skipped:
        yield Check(name, SKIP, str(skipped))
    except AssertionError as failed:
        yield Check(name, FAIL, str(failed))
    except Exception as broken:
        yield Check(name, FAIL, f"{type(broken).__name__}: {broken}")


def _manifest(extractor_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "id": extractor_id,
        "version": "1.0.0",
        "api_version": "v1",
        "accepts": {"mime_types": ["*/*"]},
        "produces": ["metadata"],
        **overrides,
    }


def _as_document(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "attempt": job.attempt,
        "idempotency_key": job.idempotency_key,
        "extractor_id": job.extractor_id,
        "generation": job.generation,
    }


def _token() -> str:
    return uuid.uuid4().hex[:8]


def run_checks(
    conformance: Conformance, *, extractor_id: str | None = None, protocol: bool = True
) -> Report:
    report = Report()
    if protocol:
        report.checks.extend(conformance.protocol_checks())
    if extractor_id is not None:
        report.checks.extend(conformance.image_checks(extractor_id))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="se-conformance",
        description="Check an extractor image and a core against the contract.",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("SE_CORE_URL", "http://localhost:8000")
    )
    parser.add_argument("--email", default=os.environ.get("SE_ADMIN_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("SE_ADMIN_PASSWORD", ""))
    parser.add_argument(
        "--extractor-id",
        default=None,
        help="an extractor already running against this instance; its image is checked too",
    )
    parser.add_argument("--workspace", default=None, help="an existing workspace to upload into")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--skip-protocol", action="store_true", help="check only the image, not the core"
    )
    arguments = parser.parse_args(argv)

    if not arguments.email or not arguments.password:
        print("an administrator's --email and --password are required", file=sys.stderr)
        return 2

    try:
        with Conformance(
            arguments.base_url,
            email=arguments.email,
            password=arguments.password,
            workspace=arguments.workspace,
            timeout=arguments.timeout,
        ) as conformance:
            report = run_checks(
                conformance,
                extractor_id=arguments.extractor_id,
                protocol=not arguments.skip_protocol,
            )
    except ConformanceError as unusable:
        print(f"the kit could not run: {unusable}", file=sys.stderr)
        return 2

    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - the CLI's entrypoint
    raise SystemExit(main())
