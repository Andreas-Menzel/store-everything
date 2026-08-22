"""pytest plugin: record which requirements each test claims to verify.

Tests declare coverage with `@pytest.mark.fr("F-002/FR-7")`. Non-default verification
methods are declared with a second marker — `benchmark`, `fault_injection`, `drill` — so
the matrix can tell a deterministic test from a benchmark run
(11-engineering-standards.md § verification methods).

Enabled for every run; it writes nothing unless `--fr-report PATH` is given.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

#: pytest marker name → the verification method it declares.
METHOD_MARKERS = {
    "benchmark": "benchmark",
    "fault_injection": "fault-injection",
    "drill": "drill",
}
DEFAULT_METHOD = "test"

#: Which suite this report speaks for. The matrix is built from one report per layer
#: (core, web, web-e2e) and names the layer beside each covering test.
LAYER = "core"

#: Worst-wins, so a failing teardown cannot be reported as a pass.
_OUTCOME_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


class RequirementRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tests: dict[str, dict[str, Any]] = {}

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            requirements = [
                str(argument) for marker in item.iter_markers("fr") for argument in marker.args
            ]
            if not requirements:
                continue
            methods = sorted(
                {
                    method
                    for marker_name, method in METHOD_MARKERS.items()
                    if item.get_closest_marker(marker_name) is not None
                }
            ) or [DEFAULT_METHOD]
            self.tests[item.nodeid] = {
                "requirements": requirements,
                "methods": methods,
                "outcome": "not run",
            }

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self.tests.get(report.nodeid)
        if entry is None:
            return
        outcome = (
            "error" if report.outcome == "failed" and report.when != "call" else report.outcome
        )
        current = entry["outcome"]
        if current == "not run" or _OUTCOME_RANK.get(outcome, 3) > _OUTCOME_RANK.get(current, -1):
            entry["outcome"] = outcome

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"layer": LAYER, "tests": self.tests}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--fr-report",
        action="store",
        default=None,
        help="write requirement-coverage JSON to this path",
    )


def pytest_configure(config: pytest.Config) -> None:
    destination = config.getoption("--fr-report")
    if destination:
        config.pluginmanager.register(RequirementRecorder(Path(str(destination))), "fr-recorder")
