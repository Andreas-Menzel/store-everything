"""Kill the process mid-append, resume, and check that nothing was doubled or lost.

The upload path's crash window is narrow and consequential: bytes are `fsync`'d **before** the
offset that promises them is committed, so a crash in between leaves a staging file *longer*
than the offset the client will be told about. The rule that makes this safe is that a resume
truncates back to the committed offset first — and this file is what proves it, by killing a
real process at each fault point and then resuming from the offset the database would still
hold.

The reverse order would be the bug: an offset promising bytes that a power cut ate, with the
client resuming past a hole neither side can see.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from store_everything import filestore
from store_everything.faults import CRASH_EXIT_STATUS, FAULT_POINT_VARIABLE

pytestmark = [pytest.mark.fault_injection, pytest.mark.fr("F-001/FR-15")]

SERVER_ROOT = Path(__file__).resolve().parents[1]

FIRST = b"the first acknowledged chunk" * 4
SECOND = b"the chunk that was in flight" * 8

#: Where an append can be interrupted, in execution order. The second is the dangerous one:
#: the bytes are durable, and the offset that would have promised them is not.
APPEND_FAULT_POINTS = ("filestore.after-append-write", "filestore.after-append-fsync")

_SCRIPT = f"""
import sys
from pathlib import Path
from uuid import UUID
from store_everything import filestore, uploads

root, session, offset, which = Path(sys.argv[1]), UUID(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
staging = filestore.staging_path(root / "staging", session)

# Exactly what the append endpoint does, in the same order: discard anything a previous
# attempt left unacknowledged, then write and make durable.
uploads.discard_unacknowledged(staging, offset)
filestore.append_to_staging(staging, {FIRST!r} if which == "first" else {SECOND!r})
"""


def append(root: Path, *, session: str, offset: int, which: str, crash_at: str | None) -> int:
    environment = dict(os.environ)
    environment["SE_APP_ENV"] = "development"
    if crash_at is not None:
        environment[FAULT_POINT_VARIABLE] = crash_at
    else:
        environment.pop(FAULT_POINT_VARIABLE, None)

    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _SCRIPT, str(root), session, str(offset), which],
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    return completed.returncode


def staged(root: Path, session: str) -> bytes:
    path = filestore.staging_path(root / "staging", UUID(session))
    return path.read_bytes() if path.exists() else b""


@pytest.mark.parametrize("crash_at", APPEND_FAULT_POINTS)
def test_an_append_killed_mid_flight_is_re_received_exactly_once(
    tmp_path: Path, crash_at: str
) -> None:
    session = str(uuid4())

    # One acknowledged append: the database would now hold `len(FIRST)`.
    assert append(tmp_path, session=session, offset=0, which="first", crash_at=None) == 0
    assert staged(tmp_path, session) == FIRST

    # The next append dies after writing — possibly after fsyncing — so its offset never
    # committed. Whatever is on disk now, the client still believes the first offset.
    outcome = append(
        tmp_path, session=session, offset=len(FIRST), which="second", crash_at=crash_at
    )
    assert outcome == CRASH_EXIT_STATUS
    assert staged(tmp_path, session).startswith(FIRST), "acknowledged bytes were lost"

    # The resume: same offset, same chunk.
    assert append(tmp_path, session=session, offset=len(FIRST), which="second", crash_at=None) == 0

    # Exactly once — not FIRST + SECOND + SECOND, and not a hole in between.
    assert staged(tmp_path, session) == FIRST + SECOND


def test_a_partial_append_is_discarded_rather_than_kept(tmp_path: Path) -> None:
    """The property the truncation exists for, stated on its own.

    A crash can leave any number of bytes past the acknowledged offset — a short write, a
    full one, anything. None of them may survive into the assembled file, because the client
    is going to send them again from the offset it was last told.
    """
    session = str(uuid4())
    assert append(tmp_path, session=session, offset=0, which="first", crash_at=None) == 0

    # Simulate the worst shape of debris: a partial tail that a naive resume would keep.
    staging = filestore.staging_path(tmp_path / "staging", UUID(session))
    with staging.open("ab") as handle:
        handle.write(SECOND[:13])

    assert append(tmp_path, session=session, offset=len(FIRST), which="second", crash_at=None) == 0

    assert staged(tmp_path, session) == FIRST + SECOND


def test_an_uninterrupted_append_is_the_control_case(tmp_path: Path) -> None:
    """Without this the assertions above could be passing vacuously."""
    session = str(uuid4())

    assert append(tmp_path, session=session, offset=0, which="first", crash_at=None) == 0

    assert staged(tmp_path, session) == FIRST
