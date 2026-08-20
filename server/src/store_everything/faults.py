"""Fault points: the hook crash-injection tests fire through.

12-reliability.md § verification requires killing the process at injected points around
every filesystem mutation and state transition, restarting, and asserting convergence.
That needs the production code path itself to be interruptible at named places — a test
cannot inject a `kill -9` between an `fsync` and a `rename` from the outside.

`fault_point()` is a dictionary lookup and a comparison when disarmed, and the module is
inert unless `SE_FAULT_POINT` is set. Arming is refused outright in production.
"""

from __future__ import annotations

import os

#: Environment variable naming the point at which the process should die.
FAULT_POINT_VARIABLE = "SE_FAULT_POINT"

#: `kill -9`'s exit status, so a killed run is recognisable in test output.
CRASH_EXIT_STATUS = 137


class UnsafeFaultInjectionError(RuntimeError):
    """Raised when a production process is asked to arm a fault point."""


def _armed_point() -> str | None:
    requested = os.environ.get(FAULT_POINT_VARIABLE)
    if not requested:
        return None
    if os.environ.get("SE_APP_ENV", "production").strip() == "production":
        raise UnsafeFaultInjectionError(
            f"{FAULT_POINT_VARIABLE} is set in a production process; "
            "fault injection is a test facility and would kill this instance"
        )
    return requested


def fault_point(name: str) -> None:
    """Die here — without unwinding — if this point is the armed one.

    `os._exit` is deliberate: it skips `finally` blocks, buffered-output flushes and
    atexit hooks, which is what an actual `kill -9` does. Anything that survives is a
    property of what was already on disk, which is exactly what the tests assert.
    """
    if _armed_point() == name:
        os._exit(CRASH_EXIT_STATUS)


def armed() -> str | None:
    """The currently armed fault point, if any. For diagnostics and tests."""
    return _armed_point()
