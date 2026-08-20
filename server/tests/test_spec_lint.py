"""The spec lint must reject malformed documentation, not merely read it."""

from __future__ import annotations

from pathlib import Path

from tools.spec_lint import (
    check_headers,
    check_index,
    check_references,
    check_requirements,
    lint,
)
from tools.specdocs import Finding, parse_feature

HEADER = """# F-901 — Probe Feature

**Status:** Draft
**Priority:** P1
**Clients:** all
**Depends on:** —

## Functional requirements

"""


def write_feature(tmp_path: Path, requirements: str, header: str = HEADER) -> Path:
    path = tmp_path / "F-901-probe.md"
    path.write_text(header + requirements, encoding="utf-8")
    return path


def errors(findings: list[Finding]) -> list[str]:
    return [finding.message for finding in findings if finding.level == "error"]


def warnings(findings: list[Finding]) -> list[str]:
    return [finding.message for finding in findings if finding.level == "warning"]


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    feature = parse_feature(write_feature(tmp_path, "- **FR-1** One.\n- **FR-1** One again.\n"))

    assert any("declared twice" in message for message in errors(check_requirements(feature)))


def test_a_gap_in_the_numbering_is_rejected(tmp_path: Path) -> None:
    """Ids are append-only: a missing number means one was deleted rather than tombstoned."""
    feature = parse_feature(write_feature(tmp_path, "- **FR-1** One.\n- **FR-3** Three.\n"))

    assert any("append-only" in message for message in errors(check_requirements(feature)))


def test_a_tombstone_closes_the_gap(tmp_path: Path) -> None:
    feature = parse_feature(
        write_feature(
            tmp_path,
            "- **FR-1** One.\n- **FR-2** *(removed — see ADR-0099)*\n- **FR-3** Three.\n",
        )
    )

    assert errors(check_requirements(feature)) == []


def test_an_unknown_verification_method_is_rejected(tmp_path: Path) -> None:
    feature = parse_feature(write_feature(tmp_path, "- **FR-1** *(verify: vibes)* One.\n"))

    assert any("unknown verification method" in m for m in errors(check_requirements(feature)))


def test_an_empty_requirement_is_rejected(tmp_path: Path) -> None:
    feature = parse_feature(write_feature(tmp_path, "- **FR-1**\n"))

    assert any("has no text" in message for message in errors(check_requirements(feature)))


def test_vague_words_warn_but_do_not_fail(tmp_path: Path) -> None:
    """A human has to choose the number that replaces the word, so this cannot be an error."""
    feature = parse_feature(
        write_feature(tmp_path, "- **FR-1** The importer handles errors gracefully.\n")
    )
    findings = check_requirements(feature)

    assert errors(findings) == []
    assert any("gracefully" in message for message in warnings(findings))


def test_a_missing_header_is_rejected(tmp_path: Path) -> None:
    header = HEADER.replace("**Clients:** all\n", "")
    feature = parse_feature(write_feature(tmp_path, "- **FR-1** One.\n", header))

    assert any("**Clients:**" in message for message in errors(check_headers(feature)))


def test_an_index_that_disagrees_with_the_file_is_rejected(tmp_path: Path) -> None:
    feature = parse_feature(write_feature(tmp_path, "- **FR-1** One.\n"))
    index = tmp_path / "README.md"
    index.write_text(
        "| ID | Feature | Clients | Status | Priority |\n|---|---|---|---|---|\n"
        "| [F-901](F-901-probe.md) | Probe Feature | Android | Draft | P1 |\n",
        encoding="utf-8",
    )

    assert any("clients" in message for message in errors(check_index([feature], index)))


def test_a_qualified_status_still_matches_the_index(tmp_path: Path) -> None:
    """Feature files may explain a status; the index carries the bare token."""
    header = HEADER.replace("**Status:** Draft", "**Status:** Deferred (design sketch — later)")
    feature = parse_feature(write_feature(tmp_path, "- **FR-1** One.\n", header))
    index = tmp_path / "README.md"
    index.write_text(
        "| ID | Feature | Clients | Status | Priority |\n|---|---|---|---|---|\n"
        "| [F-901](F-901-probe.md) | Probe Feature | all | Deferred | P1 |\n",
        encoding="utf-8",
    )

    assert errors(check_index([feature], index)) == []


def test_a_dangling_cross_reference_is_rejected(tmp_path: Path) -> None:
    feature = parse_feature(write_feature(tmp_path, "- **FR-1** One.\n"))
    (tmp_path / "other.md").write_text("See [F-901/FR-9](x) for details.\n", encoding="utf-8")
    findings = check_references([feature], tmp_path)

    assert any("does not exist" in message for message in errors(findings))


def test_a_reference_to_a_tombstone_warns(tmp_path: Path) -> None:
    feature = parse_feature(
        write_feature(tmp_path, "- **FR-1** One.\n- **FR-2** *(removed — see ADR-0099)*\n")
    )
    (tmp_path / "other.md").write_text("Guarded by F-901/FR-2.\n", encoding="utf-8")
    findings = check_references([feature], tmp_path)

    assert errors(findings) == []
    assert any("tombstone" in message for message in warnings(findings))


def test_the_committed_documentation_passes() -> None:
    """The repository's own docs are the primary fixture."""
    assert errors(lint()) == []
