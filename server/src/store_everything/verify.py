"""`verify` — the fsck-style audit that checks the invariants nothing else can.

Most guarantees in this system are enforced where they are used: a constraint refuses a bad
row, a CAS refuses a stale write. But a few are *cross-cutting* — they relate rows to bytes,
or state to time — and nothing enforces them at the moment of use because there is no single
moment. Those are what this audits (12-reliability.md § verification):

- **Debris does not accumulate.** Staging files past the grace window whose operation is
  terminal should have been collected; finding them means the janitor is not running.
- **No operation is stuck.** A non-terminal row whose lease expired long ago means nothing is
  claiming it — a worker that is not running, or a kind with no handler.
- **Blobs match their names.** A blob's filename *is* its digest, so reading it back detects
  bit rot, which no write path can catch.

Read-only, and honest about what it did not look at: a finding is a fact with a path, and a
clean run says which checks were performed. It runs after every crash-injection test, and on
demand in production — an audit nobody runs is a comment.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import filestore, janitor, operations
from store_everything.blobs import BlobStore
from store_everything.config import Settings
from store_everything.tables import operation

#: A lease this far past expiry means nobody is claiming the work, not that a worker is slow.
STUCK_LEASE_FACTOR = 10


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing that is not as it should be. `subject` is a path or an id, never prose."""

    check: str
    subject: str
    detail: str

    def render(self) -> str:
        return f"{self.check}: {self.subject} — {self.detail}"


@dataclass(frozen=True, slots=True)
class Report:
    checks: tuple[str, ...]
    findings: tuple[Finding, ...]
    blobs_read: int

    @property
    def clean(self) -> bool:
        return not self.findings

    def render(self) -> str:
        header = f"verify: {len(self.checks)} checks, {self.blobs_read} blob(s) read"
        if self.clean:
            return f"{header} — clean"
        listed = "\n".join(f"  {finding.render()}" for finding in self.findings)
        return f"{header} — {len(self.findings)} finding(s)\n{listed}"


def _uncollected_staging(roots: tuple[Path, ...], *, grace: timedelta) -> list[Path]:
    """Staging entries older than the grace window. Blocking; run in a thread."""
    cutoff = time.time() - grace.total_seconds()
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if not entry.is_file() or not filestore.is_staging_entry(entry):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    found.append(entry)
            except FileNotFoundError:
                continue
    return found


def _corrupt_blobs(root: Path, *, limit: int) -> tuple[list[str], int]:
    """Blobs whose content no longer matches their name, and how many were read.

    Bounded because reading every blob means reading the whole store — fine for a nightly
    audit on a small instance, not for an on-demand check against 10 TB. Incremental coverage
    over repeated runs is the intended pattern (12 § verification: "incremental-capable").
    """
    store = BlobStore(root)
    corrupt: list[str] = []
    read = 0
    for digest in store.iter_digests():
        if read >= limit:
            break
        read += 1
        if not store.verify(digest):
            corrupt.append(digest)
    return corrupt, read


async def audit(
    connection: AsyncConnection, *, settings: Settings, blob_sample: int = 256
) -> Report:
    """Run every check and report what it found."""
    checks = ("uncollected-debris", "stuck-operations", "blob-integrity")
    findings: list[Finding] = []
    grace = timedelta(hours=settings.janitor_grace_hours)

    stale = await asyncio.to_thread(
        _uncollected_staging, janitor.staging_roots(settings), grace=grace
    )
    for path in stale:
        owner = filestore.operation_of_staging_entry(path)
        state = None if owner is None else await operations.get(connection, owner)
        if owner is None or state is None or state.is_terminal:
            findings.append(
                Finding(
                    "uncollected-debris",
                    str(path),
                    f"older than the {settings.janitor_grace_hours}h grace window "
                    "and its operation is finished — is the janitor running?",
                )
            )

    stuck_after = timedelta(seconds=settings.lease_seconds * STUCK_LEASE_FACTOR)
    stuck = (
        await connection.execute(
            select(operation.c.id, operation.c.kind, operation.c.lease_expires_at).where(
                operation.c.state == "running",
                operation.c.lease_expires_at
                < func.now() - text(f"interval '{stuck_after.total_seconds()} seconds'"),
            )
        )
    ).all()
    for row in stuck:
        findings.append(
            Finding(
                "stuck-operations",
                str(row[0]),
                f"{row[1]} has been running with an expired lease since {row[2]} — "
                "no worker is claiming it",
            )
        )

    corrupt, blobs_read = await asyncio.to_thread(
        _corrupt_blobs, settings.versions_root, limit=blob_sample
    )
    findings.extend(
        Finding("blob-integrity", digest, "content no longer matches its digest")
        for digest in corrupt
    )

    return Report(checks=checks, findings=tuple(findings), blobs_read=blobs_read)
