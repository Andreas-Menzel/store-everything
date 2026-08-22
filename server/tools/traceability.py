"""Generate the requirement traceability matrix and enforce its gates.

Forward (requirement → tests) proves coverage; backward (test → requirement) catches
markers pointing at ids that no longer exist. `Implemented` is therefore *computed* here,
never claimed in a feature file (11-engineering-standards.md § requirement traceability).

The matrix is a CI artefact and is deliberately **not** committed: a generated file in
the repository is a staleness bug waiting to happen.

    python -m tools.traceability --report core.json --report web.json --output matrix.md

One report per test layer, merged here: the core suite is not the only place a requirement
can be verified, and a client-side FR that only a browser can check must be able to reach
the same gate (Q59).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tools.specdocs import (
    DEFAULT_METHOD,
    Feature,
    Finding,
    Invariant,
    load_features,
    load_invariants,
)

UNCOVERED = "—"

#: Requirement text is written for its feature file, so its relative links resolve from
#: `features/`. The matrix lives elsewhere and is read as a table — keep the words, drop
#: the link syntax rather than emitting links that point nowhere.
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def plain(text: str) -> str:
    return " ".join(_MARKDOWN_LINK.sub(r"\1", text).split())


def status_token(status: str) -> str:
    """`Deferred (design sketch — …)` reads as `Deferred` in a table cell."""
    return re.split(r"[(\u2014]", status, maxsplit=1)[0].strip()


@dataclass(frozen=True)
class Coverage:
    nodeid: str
    methods: tuple[str, ...]
    outcome: str
    #: Which suite proved it — `core`, `web`, `web-e2e`. Two layers can cover one
    #: requirement, and which one did is part of reading the row.
    layer: str = "core"


@dataclass(frozen=True)
class Row:
    id: str
    requirement: str
    owner: str
    method: str
    covering: tuple[Coverage, ...]
    note: str

    @property
    def verified(self) -> bool:
        """Covered by at least one *passing* test using the declared method."""
        return any(
            coverage.outcome == "passed" and self.method in coverage.methods
            for coverage in self.covering
        )

    @property
    def result(self) -> str:
        if not self.covering:
            return "not covered"
        outcomes = {coverage.outcome for coverage in self.covering}
        for outcome in ("failed", "error", "not run", "skipped"):
            if outcome in outcomes:
                return outcome
        return "passed"


def load_coverage(paths: Sequence[Path]) -> dict[str, list[Coverage]]:
    """Invert each runner's test → requirements report into requirement → tests.

    The reports are merged, not chosen between: a requirement may be covered from the core
    suite and the browser suite at once, and both belong in its row.
    """
    coverage: dict[str, list[Coverage]] = defaultdict(list)
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        layer = str(document.get("layer", "core"))
        for nodeid, entry in document.get("tests", {}).items():
            record = Coverage(
                nodeid,
                tuple(entry.get("methods", [])),
                str(entry.get("outcome", "")),
                layer,
            )
            for requirement_id in entry.get("requirements", []):
                coverage[str(requirement_id)].append(record)
    return dict(coverage)


def describe_sources(paths: Sequence[Path]) -> list[tuple[str, int]]:
    """Which layer each report speaks for, and how many marked tests it carried.

    Printed with the matrix because a *filtered* run writes a perfectly valid report
    containing one test, and the matrix built from it would report everything else in that
    layer as uncovered. A missing report is caught by the CLI; a thin one is caught here, by
    a reader who can see that `web-e2e` contributed 1 test where it usually contributes 17.
    """
    sources: list[tuple[str, int]] = []
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        sources.append((str(document.get("layer", "core")), len(document.get("tests", {}))))
    return sources


def build_rows(
    features: list[Feature],
    invariants: list[Invariant],
    coverage: dict[str, list[Coverage]],
) -> list[Row]:
    rows: list[Row] = []

    for feature in features:
        for requirement in feature.requirements:
            rows.append(
                Row(
                    id=requirement.id,
                    requirement=plain(requirement.text) or "(tombstone)",
                    owner=f"{feature.id} ({status_token(feature.status)})",
                    method=requirement.method,
                    covering=tuple(coverage.get(requirement.id, ())),
                    note="tombstone" if requirement.tombstoned else "",
                )
            )

    for invariant in invariants:
        rows.append(
            Row(
                id=invariant.id,
                requirement=plain(invariant.text),
                owner="02-domain-model",
                method=DEFAULT_METHOD,
                covering=tuple(coverage.get(invariant.id, ())),
                note="",
            )
        )

    return rows


def render_markdown(rows: list[Row], sources: Sequence[tuple[str, int]] = ()) -> str:
    verified = sum(1 for row in rows if row.verified)
    live = [row for row in rows if not row.note]

    lines = [
        "# Requirement traceability matrix",
        "",
        "Generated by `make matrix`; never committed. `Implemented` is computed from this,",
        "never claimed in a feature file.",
        "",
        f"**{verified} of {len(live)}** live requirements are verified by their declared method.",
        "",
    ]

    if sources:
        marked = ", ".join(f"{layer} ({count} marked tests)" for layer, count in sources)
        lines += [f"Built from: {marked}.", ""]

    lines += [
        "| Id | Requirement | Owner | Method | Covering tests | Result | Note |",
        "|---|---|---|---|---|---|---|",
    ]

    for row in rows:
        tests = (
            "<br>".join(f"{cover.nodeid} ({cover.layer})" for cover in row.covering) or UNCOVERED
        )
        requirement = row.requirement.replace("|", "\\|")
        if len(requirement) > 160:
            requirement = requirement[:159].rstrip() + "…"
        lines.append(
            f"| `{row.id}` | {requirement} | {row.owner} | {row.method} | "
            f"{tests} | {row.result} | {row.note} |"
        )

    return "\n".join(lines) + "\n"


def gate(
    features: list[Feature],
    rows: list[Row],
    coverage: dict[str, list[Coverage]],
) -> list[Finding]:
    """Hard gates fail the pipeline; soft gates report and let it pass."""
    findings: list[Finding] = []
    by_id = {row.id: row for row in rows}
    tombstoned = {row.id for row in rows if row.note == "tombstone"}

    # Backward: a marker pointing at nothing means a test guards a requirement that no
    # longer exists — or never did.
    for requirement_id, records in sorted(coverage.items()):
        for record in records:
            if requirement_id not in by_id:
                findings.append(
                    Finding("error", record.nodeid, f"marks {requirement_id}, which does not exist")
                )
            elif requirement_id in tombstoned:
                findings.append(
                    Finding("error", record.nodeid, f"marks {requirement_id}, which is a tombstone")
                )

    # Forward: a feature may only claim `Implemented` when every live requirement is
    # verified by its declared method.
    for feature in features:
        if not feature.is_implemented:
            continue
        for requirement in feature.requirements:
            if requirement.tombstoned:
                continue
            row = by_id.get(requirement.id)
            if row is None or not row.verified:
                findings.append(
                    Finding(
                        "error",
                        feature.path.name,
                        f"claims Implemented but {requirement.id} has no passing "
                        f"{requirement.method} verification",
                    )
                )

    # Soft: a declared method whose suite does not exist yet.
    wired = {
        method for records in coverage.values() for record in records for method in record.methods
    }
    for method in sorted({row.method for row in rows if not row.note} - wired - {DEFAULT_METHOD}):
        waiting = [row.id for row in rows if row.method == method and not row.note]
        findings.append(
            Finding(
                "warning",
                "matrix",
                f"{len(waiting)} requirement(s) declare '{method}', whose suite is not wired "
                f"up yet (e.g. {', '.join(waiting[:3])})",
            )
        )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        action="append",
        required=True,
        metavar="PATH",
        help="a runner's requirement report; repeat once per test layer",
    )
    parser.add_argument("--output", type=Path, help="write the matrix here")
    args = parser.parse_args(argv)

    # A layer whose report is absent would silently read as "nothing there verifies this",
    # which is the one wrong answer the matrix must never give.
    missing = [path for path in args.report if not path.exists()]
    if missing:
        for path in missing:
            print(f"{path} does not exist — run that layer's suite first", file=sys.stderr)
        return 1

    features = load_features()
    invariants = load_invariants()
    coverage = load_coverage(args.report)

    rows = build_rows(features, invariants, coverage)
    findings = gate(features, rows, coverage)

    if args.output is not None:
        args.output.write_text(
            render_markdown(rows, describe_sources(args.report)), encoding="utf-8"
        )
        print(f"wrote {args.output}")

    for finding in findings:
        print(finding.render(), file=sys.stderr if finding.level == "error" else sys.stdout)

    errors = [finding for finding in findings if finding.level == "error"]
    live = [row for row in rows if not row.note]
    verified = sum(1 for row in live if row.verified)
    print(f"\n{verified}/{len(live)} live requirements verified; {len(errors)} gate failure(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
