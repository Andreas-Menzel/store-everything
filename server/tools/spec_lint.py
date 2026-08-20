"""Lint the specification documents against their own authoring rules.

The rules live in features/README.md § Writing FRs; this makes the machine-checkable
ones checked. Hard errors are structural — a broken id, a dangling cross-reference, an
index that disagrees with the files — because those silently break traceability. Vague
wording is a warning: it needs a human to judge the replacement
(11-engineering-standards.md § requirement traceability, soft gates).

    python -m tools.spec_lint             # report; non-zero only on errors
    python -m tools.spec_lint --strict    # warnings fail too
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from tools.specdocs import (
    FEATURE_INDEX,
    REPO_ROOT,
    REQUIREMENT_REFERENCE,
    VERIFICATION_METHODS,
    Feature,
    Finding,
    load_features,
    load_index,
    requirement_index,
)

REQUIRED_HEADERS = ("Status", "Priority", "Clients", "Depends on")

# features/README.md rule 3: words that cannot fail a test.
VAGUE_WORDS = (
    "gracefully",
    "properly",
    "appropriately",
    "reasonable",
    "reasonably",
    "robust",
    "user-friendly",
    "seamless",
    "as needed",
)
_VAGUE = re.compile(r"\b(" + "|".join(VAGUE_WORDS) + r")\b", re.IGNORECASE)

#: Documents that discuss the conventions rather than apply them.
RULE_DOCUMENTS = {"features/README.md", "features/TEMPLATE.md"}


def check_requirements(feature: Feature) -> list[Finding]:
    findings: list[Finding] = []
    seen: dict[int, int] = {}

    for requirement in feature.requirements:
        where = f"{feature.path.name}:{requirement.line}"

        if requirement.number in seen:
            findings.append(
                Finding(
                    "error",
                    where,
                    f"FR-{requirement.number} is declared twice "
                    f"(first at line {seen[requirement.number]}); ids are unique per feature",
                )
            )
        seen[requirement.number] = requirement.line

        if requirement.method not in VERIFICATION_METHODS:
            findings.append(
                Finding(
                    "error",
                    where,
                    f"unknown verification method {requirement.method!r}; "
                    f"expected one of {', '.join(sorted(VERIFICATION_METHODS))}",
                )
            )

        if not requirement.tombstoned and not requirement.text:
            findings.append(Finding("error", where, f"FR-{requirement.number} has no text"))

        vague = _VAGUE.search(requirement.text)
        if vague and not requirement.tombstoned:
            findings.append(
                Finding(
                    "warning",
                    where,
                    f"FR-{requirement.number} uses {vague.group(1)!r} — "
                    "state a number or link a definition instead",
                )
            )

    numbers = sorted(seen)
    if numbers:
        missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
        if missing:
            findings.append(
                Finding(
                    "error",
                    feature.path.name,
                    "FR ids are append-only, so removed ones stay as tombstones; "
                    f"missing: {', '.join(f'FR-{n}' for n in missing)}",
                )
            )

    return findings


def check_headers(feature: Feature) -> list[Finding]:
    return [
        Finding("error", feature.path.name, f"missing required header **{field}:**")
        for field in REQUIRED_HEADERS
        if field not in feature.headers
    ]


def _token(value: str) -> str:
    """The bare value, without any parenthetical or dash-introduced qualifier."""
    return re.split(r"[(\u2014]", value, maxsplit=1)[0].strip()


def check_index(features: list[Feature], index_path: Path = FEATURE_INDEX) -> list[Finding]:
    findings: list[Finding] = []
    index = {entry.id: entry for entry in load_index(index_path)}
    name = index_path.name

    for feature in features:
        entry = index.pop(feature.id, None)
        if entry is None:
            findings.append(Finding("error", str(name), f"{feature.id} is missing from the index"))
            continue
        for field, in_file, in_index in (
            ("clients", feature.clients, entry.clients),
            ("status", feature.status, entry.status),
            ("priority", feature.priority, entry.priority),
        ):
            # The file is the source of truth; the index mirrors it. A file may qualify
            # a value ("Deferred (design sketch — …)"); only the token has to agree.
            if _token(in_file) != _token(in_index):
                findings.append(
                    Finding(
                        "error",
                        str(name),
                        f"{feature.id} {field}: index says {in_index!r}, "
                        f"the feature file says {in_file!r}",
                    )
                )

    findings += [
        Finding("error", str(name), f"{feature_id} is indexed but has no feature file")
        for feature_id in index
    ]
    return findings


def check_references(features: list[Feature], root: Path = REPO_ROOT) -> list[Finding]:
    """Every `F-001/FR-2` in the documentation must point at a requirement that exists."""
    known = requirement_index(features)
    findings: list[Finding] = []

    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if any(part in {".venv", "node_modules", ".git"} for part in relative.parts):
            continue
        if str(relative) in RULE_DOCUMENTS:
            continue

        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for feature_id, fr_number in REQUIREMENT_REFERENCE.findall(line):
                reference = f"{feature_id}/FR-{fr_number}"
                requirement = known.get(reference)
                if requirement is None:
                    findings.append(
                        Finding("error", f"{relative}:{number}", f"{reference} does not exist")
                    )
                elif requirement.tombstoned:
                    findings.append(
                        Finding(
                            "warning",
                            f"{relative}:{number}",
                            f"{reference} is a tombstone; the reference cannot be verified",
                        )
                    )
    return findings


def lint(features: list[Feature] | None = None) -> list[Finding]:
    resolved = load_features() if features is None else features
    findings: list[Finding] = []
    for feature in resolved:
        findings += check_headers(feature)
        findings += check_requirements(feature)
    findings += check_index(resolved)
    findings += check_references(resolved)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    findings = lint()
    errors = [finding for finding in findings if finding.level == "error"]
    warnings = [finding for finding in findings if finding.level == "warning"]

    for finding in findings:
        print(finding.render(), file=sys.stderr if finding.level == "error" else sys.stdout)

    features = load_features()
    total = sum(len(feature.requirements) for feature in features)
    print(
        f"\n{len(features)} features, {total} requirements: "
        f"{len(errors)} error(s), {len(warnings)} warning(s)"
    )

    if errors:
        return 1
    return 1 if (args.strict and warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
