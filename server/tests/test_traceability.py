"""The matrix computes `Implemented`; these tests prove it can also refuse it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.specdocs import Feature, Finding, Invariant, Requirement
from tools.traceability import Coverage, build_rows, gate, load_coverage, render_markdown

SERVER_ROOT = Path(__file__).resolve().parents[1]


def feature(status: str, *requirements: Requirement) -> Feature:
    return Feature(
        id="F-901",
        path=Path("F-901-probe.md"),
        title="Probe",
        headers={"Status": status, "Priority": "P1", "Clients": "all", "Depends on": "—"},
        requirements=requirements,
    )


def requirement(number: int, method: str = "test", tombstoned: bool = False) -> Requirement:
    return Requirement("F-901", number, f"Requirement {number}", method, tombstoned, number)


def passing(*methods: str) -> Coverage:
    return Coverage("tests/test_probe.py::test_one", methods or ("test",), "passed")


def errors(findings: list[Finding]) -> list[str]:
    return [finding.message for finding in findings if finding.level == "error"]


def warnings(findings: list[Finding]) -> list[str]:
    return [finding.message for finding in findings if finding.level == "warning"]


def test_a_marker_for_an_unknown_requirement_fails_the_gate() -> None:
    """Backward tracing: the test would otherwise guard nothing at all."""
    one = feature("Draft", requirement(1))
    coverage = {"F-901/FR-99": [passing()]}
    rows = build_rows([one], [], coverage)

    assert any("does not exist" in message for message in errors(gate([one], rows, coverage)))


def test_a_marker_for_a_tombstone_fails_the_gate() -> None:
    one = feature("Draft", requirement(1, tombstoned=True))
    coverage = {"F-901/FR-1": [passing()]}
    rows = build_rows([one], [], coverage)

    assert any("tombstone" in message for message in errors(gate([one], rows, coverage)))


def test_claiming_implemented_without_coverage_fails_the_gate() -> None:
    one = feature("Implemented", requirement(1))
    rows = build_rows([one], [], {})

    assert any("no passing test verification" in m for m in errors(gate([one], rows, {})))


def test_claiming_implemented_with_a_failing_test_fails_the_gate() -> None:
    one = feature("Implemented", requirement(1))
    coverage = {"F-901/FR-1": [Coverage("tests/test_probe.py::test_one", ("test",), "failed")]}
    rows = build_rows([one], [], coverage)

    assert errors(gate([one], rows, coverage))


def test_coverage_by_the_wrong_method_does_not_count() -> None:
    """An FR verified by benchmark is not satisfied by an ordinary test."""
    one = feature("Implemented", requirement(1, method="benchmark"))
    coverage = {"F-901/FR-1": [passing("test")]}
    rows = build_rows([one], [], coverage)

    assert errors(gate([one], rows, coverage))
    assert rows[0].verified is False


def test_implemented_with_passing_coverage_of_the_declared_method_passes() -> None:
    one = feature("Implemented", requirement(1, method="benchmark"), requirement(2))
    coverage = {"F-901/FR-1": [passing("benchmark")], "F-901/FR-2": [passing("test")]}
    rows = build_rows([one], [], coverage)

    assert errors(gate([one], rows, coverage)) == []
    assert all(row.verified for row in rows)


def test_a_tombstone_does_not_block_implemented() -> None:
    one = feature("Implemented", requirement(1), requirement(2, tombstoned=True))
    coverage = {"F-901/FR-1": [passing()]}
    rows = build_rows([one], [], coverage)

    assert errors(gate([one], rows, coverage)) == []


def test_an_unwired_suite_warns_rather_than_fails() -> None:
    one = feature("Draft", requirement(1, method="fault-injection"))
    rows = build_rows([one], [], {})
    findings = gate([one], rows, {})

    assert errors(findings) == []
    assert any("not wired up yet" in message for message in warnings(findings))


def test_invariants_take_part_in_the_matrix() -> None:
    rows = build_rows([], [Invariant(8, "App-written bytes outlive their rows.")], {})

    assert [row.id for row in rows] == ["02/INV-8"]


def test_the_matrix_reports_what_is_covered() -> None:
    one = feature("Draft", requirement(1), requirement(2))
    coverage = {"F-901/FR-1": [passing()]}
    rendered = render_markdown(build_rows([one], [], coverage))

    assert "**1 of 2**" in rendered
    assert "tests/test_probe.py::test_one" in rendered
    assert "not covered" in rendered


def test_coverage_is_inverted_from_the_plugin_report(tmp_path: Path) -> None:
    report = tmp_path / "fr.json"
    report.write_text(
        json.dumps(
            {
                "tests": {
                    "tests/test_a.py::test_x": {
                        "requirements": ["F-901/FR-1", "F-901/FR-2"],
                        "methods": ["test"],
                        "outcome": "passed",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    coverage = load_coverage(report)

    assert sorted(coverage) == ["F-901/FR-1", "F-901/FR-2"]
    assert coverage["F-901/FR-1"][0].outcome == "passed"


def test_the_plugin_records_markers_from_a_real_run(tmp_path: Path) -> None:
    """End to end: the marker a feature test will carry reaches the report."""
    suite = tmp_path / "test_marked.py"
    suite.write_text(
        "import pytest\n\n"
        "@pytest.mark.fr('F-901/FR-1')\n"
        "def test_marked() -> None:\n    assert True\n\n"
        "def test_unmarked() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    report = tmp_path / "fr.json"

    subprocess.run(  # noqa: S603 - fixed argv
        # -c/--rootdir pin the project config: without it pytest roots itself in the
        # temporary directory, finds no configuration, and never loads the plugin.
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-q",
            "-c",
            "pyproject.toml",
            "--rootdir",
            ".",
            f"--fr-report={report}",
        ],
        cwd=SERVER_ROOT,
        check=True,
        capture_output=True,
    )

    recorded = json.loads(report.read_text(encoding="utf-8"))["tests"]

    assert len(recorded) == 1, "only the marked test should appear"
    entry = next(iter(recorded.values()))
    assert entry["requirements"] == ["F-901/FR-1"]
    assert entry["methods"] == ["test"]
    assert entry["outcome"] == "passed"
