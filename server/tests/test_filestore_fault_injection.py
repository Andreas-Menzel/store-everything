"""Kill the process at every step of the write protocol, and check what survived.

12-reliability.md's binding property, applied to the filesystem: after any prefix of a write
plus a restart, the destination holds either the old content or the new one — never a
truncated file, never a file whose directory entry was lost — and a retry converges.

Phase 0 proved this against a demonstration copy of the protocol; this proves it against
the real `filestore`, which is the only version that matters — the demo has been retired
rather than left to drift from the code it stood in for.

The crash is a real `os._exit(137)` in a subprocess: no unwinding, no `finally`, no
buffered writes flushed — exactly what a power cut does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from store_everything.faults import CRASH_EXIT_STATUS, FAULT_POINT_VARIABLE

pytestmark = pytest.mark.fault_injection

SERVER_ROOT = Path(__file__).resolve().parents[1]

OLD = b"the content that was already there"
NEW = b"the content being written" * 50

#: Every point a write can be interrupted at, in execution order.
WRITE_FAULT_POINTS = (
    "filestore.before-staging",
    "filestore.after-staging-write",
    "filestore.after-staging-fsync",
    "filestore.after-rename",
    "filestore.after-directory-fsync",
)

#: The journaled path, used when the destination is on another filesystem.
MOVE_FAULT_POINTS = (
    "filestore.before-journaled-copy",
    "filestore.after-journaled-copy",
    "filestore.after-journaled-verify",
    "filestore.before-source-unlink",
)

#: The payload travels as a literal so the subprocess needs no fixture of its own.
_SCRIPT = f"""
import sys
from pathlib import Path
from uuid import UUID
from store_everything import filestore

action, root, operation = sys.argv[1], Path(sys.argv[2]), UUID(sys.argv[3])
destination = root / "tree" / "document.bin"
staging = filestore.staging_path(root / "staging", operation)

if action == "write":
    filestore.write_atomically(destination, {NEW!r}, staging=staging)
else:
    source = root / "source" / "original.bin"
    filestore.journaled_move(source, destination, staging=staging)
"""


def run(action: str, root: Path, *, operation: str, crash_at: str | None) -> int:
    environment = dict(os.environ)
    environment["SE_APP_ENV"] = "development"
    if crash_at is not None:
        environment[FAULT_POINT_VARIABLE] = crash_at
    else:
        environment.pop(FAULT_POINT_VARIABLE, None)

    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _SCRIPT, action, str(root), operation],
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    return completed.returncode


def staging_entries(root: Path) -> list[Path]:
    staging = root / "staging"
    return sorted(staging.iterdir()) if staging.is_dir() else []


@pytest.mark.parametrize("crash_at", WRITE_FAULT_POINTS)
def test_a_write_killed_anywhere_leaves_readable_content(tmp_path: Path, crash_at: str) -> None:
    destination = tmp_path / "tree" / "document.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(OLD)
    operation = str(uuid4())

    assert run("write", tmp_path, operation=operation, crash_at=crash_at) == CRASH_EXIT_STATUS

    # The invariant: one of the two complete contents, never a mixture.
    assert destination.read_bytes() in (OLD, NEW), f"torn write after a crash at {crash_at}"

    # The retry converges on the same staging path and the same destination.
    assert run("write", tmp_path, operation=operation, crash_at=None) == 0
    assert destination.read_bytes() == NEW
    assert staging_entries(tmp_path) == [], "the retry left debris behind"


def test_a_write_with_no_previous_content_is_absent_or_complete(tmp_path: Path) -> None:
    """The other starting state: nothing to fall back to, so absence is the safe answer."""
    destination = tmp_path / "tree" / "document.bin"
    operation = str(uuid4())

    for crash_at in WRITE_FAULT_POINTS:
        assert run("write", tmp_path, operation=operation, crash_at=crash_at) == CRASH_EXIT_STATUS
        assert not destination.exists() or destination.read_bytes() == NEW

    assert run("write", tmp_path, operation=operation, crash_at=None) == 0
    assert destination.read_bytes() == NEW


@pytest.mark.parametrize("crash_at", MOVE_FAULT_POINTS)
def test_a_journaled_move_killed_anywhere_never_loses_the_content(
    tmp_path: Path, crash_at: str
) -> None:
    """The cross-filesystem path: both copies is fine, a truncated one is not, none is fatal."""
    source = tmp_path / "source" / "original.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(NEW)
    destination = tmp_path / "tree" / "document.bin"
    operation = str(uuid4())

    assert run("move", tmp_path, operation=operation, crash_at=crash_at) == CRASH_EXIT_STATUS

    survivors = [path for path in (source, destination) if path.exists()]
    assert survivors, f"the content vanished after a crash at {crash_at}"
    for path in survivors:
        # Whatever survived is whole. A half-copied destination would be the real bug.
        assert path.read_bytes() == NEW, f"{path.name} is truncated after {crash_at}"

    assert run("move", tmp_path, operation=operation, crash_at=None) == 0
    assert destination.read_bytes() == NEW
    assert not source.exists()
    assert staging_entries(tmp_path) == []


def test_an_uninterrupted_write_is_the_control_case(tmp_path: Path) -> None:
    """Without this, the assertions above could be passing vacuously."""
    destination = tmp_path / "tree" / "document.bin"

    assert run("write", tmp_path, operation=str(uuid4()), crash_at=None) == 0

    assert destination.read_bytes() == NEW
    assert staging_entries(tmp_path) == []
