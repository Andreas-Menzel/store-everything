"""The reference extractor: the smallest honest one, and the shape of every real one.

Three jobs at once (11 § test infrastructure):

1. **an executable example.** Everything a third-party author needs to copy is in this file, and
   it is short on purpose: a manifest, a handler, `run()`.
2. **a test double.** Deterministic and instant, so an end-to-end test can watch a file go from
   `pending` to `indexed` without waiting for OCR. `SE_REFERENCE_MODE` makes it fail, fail
   permanently, or stall, so the paths around a *broken* extractor are testable too.
3. **the image the conformance kit validates itself against.** If the kit fails here, the kit is
   wrong.

What it actually does by default is the one thing every extractor should: read its input and
check the bytes against the hash the core declared. That is a real integrity check, and getting
it wrong is how an extractor silently analyses the wrong file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import sys
import time
from types import FrameType
from typing import Any

from se_extractor.client import ExtractorClient
from se_extractor.loop import JobContext, PermanentFailure, Worker
from se_extractor.models import Job

_logger = logging.getLogger("se_extractor.reference")

EXTRACTOR_ID = "reference"
VERSION = "1.0.0"

#: The one asset kind this extractor produces — an excerpt of what it read, which is enough to
#: exercise two-phase staging and, for a test, to chain another extractor onto.
ASSET_KIND = "text-excerpt"
ASSET_NAME = "excerpt.txt"
EXCERPT_BYTES = 64

#: What this extractor tells the core it can do. `*/*` because a double has to be routed
#: whatever a test uploads; a real extractor names the types it can actually read.
MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    "accepts": {"mime_types": ["*/*"]},
    "produces": ["metadata", "text_segments", "derived_assets"],
    "derived_asset_kinds": [ASSET_KIND],
    "cost_class": "light",
    "gpu": "none",
    "network": "none",
}

#: How a test tells the double to misbehave.
MODES = ("verify", "succeed", "fail", "fail-permanently", "stall")


def build_manifest(mode: str) -> dict[str, Any]:
    """The manifest, with the mode in the version so a mode change is a version change.

    Not cosmetic: the extractor version is provenance, and two runs that behaved differently
    must not claim to be the same program (ADR-0004).
    """
    return {**MANIFEST, "version": f"{VERSION}+{mode}"}


def handle(job: Job, context: JobContext, *, mode: str, delay: float) -> dict[str, Any] | None:
    """Do the work — or the chosen imitation of it."""
    if mode == "fail":
        raise RuntimeError("the reference extractor was asked to fail")
    if mode == "fail-permanently":
        raise PermanentFailure("the reference extractor was asked to fail permanently")

    if mode == "stall":
        # Never finishes: the job's lease lapses and the core re-runs it, which is the path a
        # killed container takes.
        while not context.cancelled:
            time.sleep(0.2)
        return None

    if delay:
        # Split so cancellation is noticed promptly, the way a real extractor should behave.
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            context.raise_if_cancelled()
            time.sleep(min(0.2, max(deadline - time.monotonic(), 0)))

    if mode == "verify":
        return _analyse(job, context)
    return None


def _analyse(job: Job, context: JobContext) -> dict[str, Any] | None:
    """Read the input, check it, and describe it — the shape of every real extractor.

    The check first: the job declares the content hash of what it handed over, and verifying it
    is one line. Getting that wrong is how an extractor silently analyses the wrong file, and a
    mismatch is *permanent* — the same bytes will not hash differently on a retry.

    Then the description, which is deliberately a sample of every output shape rather than
    anything clever: typed facts of each storage class, one segment per line with a line anchor,
    and one staged asset. A test can therefore assert on all three from one job.
    """
    original = job.original
    if original is None:
        _logger.info("job %s has no input to read", job.id)
        return None

    data = _read(job, context)
    if len(data) != original.size:
        raise PermanentFailure(f"read {len(data)} bytes where the core declared {original.size}")
    if hashlib.sha256(data).hexdigest() != original.content_hash:
        raise PermanentFailure("the bytes read do not match the declared content hash")
    _logger.info("job %s verified %d bytes", job.id, len(data))

    # A chained job analyses somebody else's output; it has nothing new to add about the file.
    if original.kind != "original":
        return None

    text = data.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    excerpt = data[:EXCERPT_BYTES]

    return {
        "metadata": [
            {"key": "byte_count", "type": "integer", "value": len(data)},
            {"key": "line_count", "type": "integer", "value": len(lines)},
            {"key": "language", "type": "string", "value": "und", "confidence": 0.5},
            {"key": "is_probably_text", "type": "boolean", "value": _looks_like_text(data)},
            {"key": "checked_at", "type": "datetime", "value": "2026-08-23T00:00:00+00:00"},
            {"key": "sample_geo", "type": "geo", "value": {"lat": 48.137, "lon": 11.575}},
            {"key": "reference_report", "type": "json", "value": {"lines": len(lines)}},
        ],
        "text_segments": [
            {
                "text": line,
                "anchor": {"kind": "line", "start_line": number, "end_line": number},
                "language": "und",
            }
            for number, line in enumerate(lines, start=1)
        ],
        "derived_assets": [
            {
                "kind": ASSET_KIND,
                "name": ASSET_NAME,
                "content_hash": context.client.stage_asset(job, excerpt),
                "media_type": "text/plain",
                "params": {"bytes": len(excerpt)},
            }
        ],
    }


def _read(job: Job, context: JobContext) -> bytes:
    """The whole input, in chunks — a real extractor streams for the same reason."""
    chunks: list[bytes] = []
    with context.client.stream_input(job) as stream:
        for chunk in stream:
            context.raise_if_cancelled()
            chunks.append(chunk)
    return b"".join(chunks)


def _looks_like_text(data: bytes) -> bool:
    return b"\x00" not in data


def _environment() -> tuple[str, str, str, float, int]:
    base_url = os.environ.get("SE_CORE_URL", "http://api:8000")
    token = os.environ.get("SE_EXTRACTOR_TOKEN", "")
    mode = os.environ.get("SE_REFERENCE_MODE", "verify")
    delay = float(os.environ.get("SE_REFERENCE_DELAY_SECONDS", "0") or 0)
    claim_wait = int(os.environ.get("SE_CLAIM_WAIT_SECONDS", "30") or 30)
    return base_url, token, mode, delay, claim_wait


def main() -> int:
    """Run the reference extractor from the environment. The whole of an image's entrypoint."""
    logging.basicConfig(
        level=os.environ.get("SE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    base_url, token, mode, delay, claim_wait = _environment()
    if not token:
        print("SE_EXTRACTOR_TOKEN is not set", file=sys.stderr)
        return 2
    if mode not in MODES:
        print(f"SE_REFERENCE_MODE must be one of {', '.join(MODES)}", file=sys.stderr)
        return 2

    client = ExtractorClient(base_url, token)
    worker = Worker(
        client,
        build_manifest(mode),
        lambda job, context: handle(job, context, mode=mode, delay=delay),
        claim_wait=claim_wait,
        worker_name=os.environ.get("HOSTNAME"),
    )

    def stop(_signal: int, _frame: FrameType | None) -> None:
        # A container is stopped, not asked. Finishing the current job and leaving is enough:
        # anything unclaimed stays queued and anything claimed has a lease that lapses.
        _logger.info("stopping")
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        worker.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the image's entrypoint
    raise SystemExit(main())
