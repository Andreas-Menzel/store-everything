"""The gates guard the code; these guard the gates."""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# 11-engineering-standards.md § coverage. The floor only ever moves up; lowering it is
# how a coverage gate quietly stops meaning anything.
COVERAGE_FLOOR = 85


def _pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_coverage_floor_never_drops() -> None:
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    coverage = tool["coverage"]
    assert isinstance(coverage, dict)
    report = coverage["report"]
    assert isinstance(report, dict)

    assert report["fail_under"] >= COVERAGE_FLOOR


def test_strict_typing_stays_on() -> None:
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    pyright = tool["pyright"]
    assert isinstance(pyright, dict)

    assert pyright["typeCheckingMode"] == "strict"
