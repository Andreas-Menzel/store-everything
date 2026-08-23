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

#: What this extractor tells the core it can do. `*/*` because a double has to be routed
#: whatever a test uploads; a real extractor names the types it can actually read.
MANIFEST: dict[str, Any] = {
    "id": EXTRACTOR_ID,
    "version": VERSION,
    "api_version": "v1",
    "accepts": {"mime_types": ["*/*"]},
    # Declared, not yet stored: this core version accepts an envelope with no outputs, and the
    # committed contract says so (05 § result envelope).
    "produces": ["metadata"],
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
        _verify(job, context)
    return None


def _verify(job: Job, context: JobContext) -> None:
    """Read the input and check it against the hash the core declared.

    A mismatch is permanent: the same bytes will not hash differently on a retry, and a job that
    keeps retrying hides the fact that something is wrong with the storage.
    """
    original = job.original
    if original is None or job.file_version is None:
        _logger.info("job %s has no original to read", job.id)
        return

    digest = hashlib.sha256()
    read = 0
    with context.client.stream_input(job) as chunks:
        for chunk in chunks:
            context.raise_if_cancelled()
            digest.update(chunk)
            read += len(chunk)

    if read != original.size:
        raise PermanentFailure(f"read {read} bytes where the core declared {original.size}")
    if digest.hexdigest() != original.content_hash:
        raise PermanentFailure("the bytes read do not match the declared content hash")
    _logger.info("job %s verified %d bytes", job.id, read)


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
