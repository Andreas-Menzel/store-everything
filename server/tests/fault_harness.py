"""The crash-injection harness: run an operation, kill it, restart, assert convergence.

12-reliability.md's binding property is that *after any prefix of any operation, plus a
restart, the system converges to the same terminal state* — no debris past its grace
window, no duplicated effects. Proving that needs a real process death, so the operation
runs in a subprocess that `os._exit`s at the armed fault point.

The demo operation below is the filesystem write protocol in miniature — stage, fsync,
rename, fsync the directory. The real shared write layer arrives in phase 1 and inherits
both this protocol and this harness.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from store_everything.faults import CRASH_EXIT_STATUS, FAULT_POINT_VARIABLE, fault_point

SERVER_ROOT = Path(__file__).resolve().parents[1]

#: Every point at which the demo operation can be interrupted, in execution order.
DEMO_FAULT_POINTS = (
    "demo.before-staging",
    "demo.after-staging-write",
    "demo.after-staging-fsync",
    "demo.after-rename",
)

STAGING_SUFFIX = ".staging"


def staging_path(destination: Path) -> Path:
    """Deterministic, so a retry reuses the same staging file instead of leaking a new one."""
    return destination.with_name(destination.name + STAGING_SUFFIX)


def write_atomically(destination: Path, payload: bytes) -> None:
    """Stage on the destination filesystem, fsync, rename, fsync the directory.

    Deterministic paths make retries converge: the same call after a crash reaches the
    same final state rather than leaving a second copy behind.
    """
    staging = staging_path(destination)

    fault_point("demo.before-staging")

    # Opened by descriptor rather than Path.write_bytes: fsync needs the file descriptor.
    with open(staging, "wb") as handle:
        handle.write(payload)
        handle.flush()
        fault_point("demo.after-staging-write")
        os.fsync(handle.fileno())

    fault_point("demo.after-staging-fsync")

    os.replace(staging, destination)

    fault_point("demo.after-rename")

    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def collect_debris(directory: Path) -> list[Path]:
    """Staging files left behind. The janitor's job in production; here, evidence."""
    return sorted(path for path in directory.iterdir() if path.name.endswith(STAGING_SUFFIX))


@dataclass(frozen=True)
class Run:
    exit_status: int
    crashed: bool


def run_operation(destination: Path, payload: bytes, crash_at: str | None = None) -> Run:
    """Run the demo operation in a fresh process, optionally killing it at `crash_at`."""
    environment = dict(os.environ)
    environment["SE_APP_ENV"] = "development"
    if crash_at is not None:
        environment[FAULT_POINT_VARIABLE] = crash_at
    else:
        environment.pop(FAULT_POINT_VARIABLE, None)

    completed = subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-c",
            "import sys;"
            "from pathlib import Path;"
            "from tests.fault_harness import write_atomically;"
            "write_atomically(Path(sys.argv[1]), sys.argv[2].encode())",
            str(destination),
            payload.decode(),
        ],
        cwd=SERVER_ROOT,
        env=environment,
        capture_output=True,
        check=False,
    )
    return Run(completed.returncode, completed.returncode == CRASH_EXIT_STATUS)
