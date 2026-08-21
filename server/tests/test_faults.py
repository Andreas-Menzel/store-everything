"""The fault hook itself: inert unless armed, and impossible to arm in production.

The hook is production code — crash-injection cannot be simulated from outside a process, so
`fault_point()` sits in the real write path (12-reliability.md § verification). That makes two
properties load-bearing: it must cost nothing when disarmed, and it must be impossible to arm
by accident on a real instance, where it would kill the service.

What the fault points *do* is asserted where they are: `test_filestore_fault_injection.py`
for the write protocol, `test_operation_fault_injection.py` for state transitions. Each
parametrised crash there also proves the point it names is reachable — a fault point nothing
reaches is a comment pretending to be a test.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from store_everything.faults import (
    CRASH_EXIT_STATUS,
    FAULT_POINT_VARIABLE,
    UnsafeFaultInjectionError,
    armed,
    fault_point,
)


def test_fault_points_are_inert_when_nothing_is_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FAULT_POINT_VARIABLE, raising=False)

    fault_point("filestore.after-rename")  # returns, which is the whole assertion

    assert armed() is None


def test_an_armed_point_does_not_affect_other_points(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SE_APP_ENV", "development")
    monkeypatch.setenv(FAULT_POINT_VARIABLE, "filestore.after-rename")

    fault_point("filestore.before-staging")  # a different point: still inert

    assert armed() == "filestore.after-rename"


def test_arming_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a stray environment variable is a remote kill switch."""
    monkeypatch.setenv("SE_APP_ENV", "production")
    monkeypatch.setenv(FAULT_POINT_VARIABLE, "filestore.after-rename")

    with pytest.raises(UnsafeFaultInjectionError):
        fault_point("filestore.after-rename")


def test_a_production_process_refuses_to_start_armed() -> None:
    """The same rule from the outside: a real process, a real environment."""
    environment = dict(os.environ)
    environment["SE_APP_ENV"] = "production"
    environment[FAULT_POINT_VARIABLE] = "filestore.after-rename"

    program = (
        "from store_everything.faults import fault_point; fault_point('filestore.after-rename')"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", program],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode not in (0, CRASH_EXIT_STATUS)
    assert "fault injection" in completed.stderr
