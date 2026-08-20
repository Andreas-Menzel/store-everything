"""Crash injection: kill the operation at every point, restart, assert convergence.

The property under test is 12-reliability.md's binding one — after any prefix of the
operation plus a restart, the system reaches the same terminal state, with no debris and
no duplicated effects. Phase 0 proves it on the write protocol in miniature; phase 1
points the same harness at the real shared write layer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from store_everything.faults import (
    FAULT_POINT_VARIABLE,
    UnsafeFaultInjectionError,
    armed,
    fault_point,
)
from tests.fault_harness import (
    DEMO_FAULT_POINTS,
    SERVER_ROOT,
    collect_debris,
    run_operation,
    staging_path,
)

PAYLOAD = b"the bytes that must survive"


def test_fault_points_are_inert_when_nothing_is_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FAULT_POINT_VARIABLE, raising=False)

    fault_point("demo.before-staging")  # must simply return

    assert armed() is None


def test_arming_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray environment variable must never be able to kill a real instance."""
    monkeypatch.setenv(FAULT_POINT_VARIABLE, "demo.after-rename")
    monkeypatch.setenv("SE_APP_ENV", "production")

    with pytest.raises(UnsafeFaultInjectionError, match="production"):
        fault_point("demo.after-rename")


def test_the_operation_succeeds_when_it_is_not_interrupted(tmp_path: Path) -> None:
    destination = tmp_path / "payload.bin"

    result = run_operation(destination, PAYLOAD)

    assert result.exit_status == 0
    assert destination.read_bytes() == PAYLOAD
    assert collect_debris(tmp_path) == []


@pytest.mark.fault_injection
@pytest.mark.parametrize("crash_at", DEMO_FAULT_POINTS)
def test_a_crash_at_any_point_converges_on_retry(tmp_path: Path, crash_at: str) -> None:
    destination = tmp_path / "payload.bin"

    crashed = run_operation(destination, PAYLOAD, crash_at=crash_at)
    assert crashed.crashed, f"the process should have died at {crash_at}"

    # Whatever the crash left behind, the retry reaches the terminal state.
    retried = run_operation(destination, PAYLOAD)

    assert retried.exit_status == 0
    assert destination.read_bytes() == PAYLOAD
    assert collect_debris(tmp_path) == [], "a retry must not leave staging debris"


@pytest.mark.fault_injection
@pytest.mark.parametrize("crash_at", DEMO_FAULT_POINTS)
def test_a_crash_never_leaves_a_partial_file_at_the_destination(
    tmp_path: Path, crash_at: str
) -> None:
    """The destination is either absent or complete — never half-written.

    This is what the staged write buys: readers of the final path cannot observe a
    truncated file, whatever moment the process dies at.
    """
    destination = tmp_path / "payload.bin"

    run_operation(destination, PAYLOAD, crash_at=crash_at)

    if destination.exists():
        assert destination.read_bytes() == PAYLOAD


@pytest.mark.fault_injection
def test_a_crash_before_the_rename_leaves_no_destination(tmp_path: Path) -> None:
    """Bytes first, row second: nothing references content that was never renamed in."""
    destination = tmp_path / "payload.bin"

    run_operation(destination, PAYLOAD, crash_at="demo.after-staging-fsync")

    assert not destination.exists()
    assert collect_debris(tmp_path) == [staging_path(destination)]


@pytest.mark.fault_injection
def test_a_crash_after_the_rename_has_already_succeeded(tmp_path: Path) -> None:
    """The directory fsync is durability, not visibility: the content is already there."""
    destination = tmp_path / "payload.bin"

    run_operation(destination, PAYLOAD, crash_at="demo.after-rename")

    assert destination.read_bytes() == PAYLOAD
    assert collect_debris(tmp_path) == []


def test_every_declared_fault_point_is_reachable(tmp_path: Path) -> None:
    """A point nothing can stop at would give false confidence in the coverage above."""
    for index, point in enumerate(DEMO_FAULT_POINTS):
        destination = tmp_path / f"payload-{index}.bin"
        assert run_operation(destination, PAYLOAD, crash_at=point).crashed, point


def test_an_unknown_fault_point_does_not_stop_anything(tmp_path: Path) -> None:
    destination = tmp_path / "payload.bin"

    result = run_operation(destination, PAYLOAD, crash_at="demo.no-such-point")

    assert result.exit_status == 0
    assert destination.read_bytes() == PAYLOAD


def test_the_harness_refuses_to_arm_a_production_process(tmp_path: Path) -> None:
    """The safety rail holds through a real process, not only in-process."""
    environment = dict(os.environ)
    environment["SE_APP_ENV"] = "production"
    environment[FAULT_POINT_VARIABLE] = "demo.before-staging"

    completed = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-c",
            "import sys;"
            "from pathlib import Path;"
            "from tests.fault_harness import write_atomically;"
            "write_atomically(Path(sys.argv[1]), b'x')",
            str(tmp_path / "payload.bin"),
        ],
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert b"UnsafeFaultInjectionError" in completed.stderr
