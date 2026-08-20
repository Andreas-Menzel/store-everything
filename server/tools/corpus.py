"""Validate the ground-truth corpus and generate its attribution notice (ADR-0015).

Every fixture carries a manifest row: what it is, where it came from, under which licence,
and — the point of the whole exercise — the truth it asserts. A fixture nobody can explain
is a fixture nobody can trust, so an unlisted file is an error rather than a warning.

    python -m tools.corpus                     # validate
    python -m tools.corpus --refresh           # recompute hashes and sizes
    python -m tools.corpus --attribution PATH  # write the attribution notice
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.specdocs import REPO_ROOT, Finding

CORPUS_ROOT = REPO_ROOT / "corpus"
FIXTURES_ROOT = CORPUS_ROOT / "fixtures"
MANIFEST_PATH = CORPUS_ROOT / "manifest.json"
ATTRIBUTION_PATH = CORPUS_ROOT / "ATTRIBUTION.md"

GENERATED = "generated"
CURATED = "curated"


@dataclass(frozen=True)
class Fixture:
    path: str
    origin: str
    license: str
    asserts: str
    sha256: str
    bytes: int
    generator: str
    source: dict[str, str]


def _fixture(entry: dict[str, Any]) -> Fixture:
    return Fixture(
        path=str(entry.get("path", "")),
        origin=str(entry.get("origin", "")),
        license=str(entry.get("license", "")),
        asserts=str(entry.get("asserts", "")),
        sha256=str(entry.get("sha256", "")),
        bytes=int(entry.get("bytes", -1)),
        generator=str(entry.get("generator", "")),
        source={key: str(value) for key, value in (entry.get("source") or {}).items()},
    )


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[dict[str, int], list[Fixture]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    budget = {key: int(value) for key, value in document.get("budget", {}).items()}
    return budget, [_fixture(entry) for entry in document.get("fixtures", [])]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(manifest_path: Path = MANIFEST_PATH, root: Path = FIXTURES_ROOT) -> list[Finding]:
    budget, fixtures = load_manifest(manifest_path)
    findings: list[Finding] = []

    listed = {fixture.path for fixture in fixtures}
    on_disk = {str(path.relative_to(root)) for path in sorted(root.rglob("*")) if path.is_file()}

    findings += [
        Finding("error", f"corpus/fixtures/{name}", "is not listed in manifest.json")
        for name in sorted(on_disk - listed)
    ]
    findings += [
        Finding("error", "manifest.json", f"lists {name}, which does not exist")
        for name in sorted(listed - on_disk)
    ]

    total = 0
    for fixture in sorted(fixtures, key=lambda item: item.path):
        where = f"manifest.json[{fixture.path}]"

        if not fixture.license:
            findings.append(Finding("error", where, "declares no licence"))
        if not fixture.asserts:
            findings.append(Finding("error", where, "declares no ground truth; say what it proves"))
        if fixture.origin == GENERATED and not fixture.generator:
            findings.append(Finding("error", where, "is generated but names no generator"))
        if fixture.origin == CURATED:
            for field in ("url", "author", "retrieved"):
                if not fixture.source.get(field):
                    findings.append(
                        Finding("error", where, f"is curated but its source has no {field}")
                    )
        if fixture.origin not in {GENERATED, CURATED}:
            findings.append(Finding("error", where, f"has unknown origin {fixture.origin!r}"))

        located = root / fixture.path
        if not located.exists():
            continue

        actual_size = located.stat().st_size
        actual_digest = digest(located)
        total += actual_size

        if fixture.sha256 != actual_digest:
            findings.append(
                Finding("error", where, "sha256 does not match the file; run `make corpus`")
            )
        if fixture.bytes != actual_size:
            findings.append(
                Finding("error", where, "size does not match the file; run `make corpus`")
            )

        limit = budget.get("max_file_bytes", 0)
        if limit and actual_size > limit:
            findings.append(
                Finding(
                    "error", where, f"is {actual_size} bytes, over the {limit}-byte per-file cap"
                )
            )

    total_limit = budget.get("total_bytes", 0)
    if total_limit and total > total_limit:
        findings.append(
            Finding(
                "error",
                "corpus",
                f"is {total} bytes, over the {total_limit}-byte budget; move bulk fixtures "
                "out of the repository (ADR-0015)",
            )
        )

    return findings


def refresh(manifest_path: Path = MANIFEST_PATH) -> int:
    """Fill in the mechanical fields. Licence and ground truth stay human-written."""
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    updated = 0
    for entry in document.get("fixtures", []):
        path = FIXTURES_ROOT / str(entry.get("path", ""))
        if not path.exists():
            continue
        checksum, size = digest(path), path.stat().st_size
        if entry.get("sha256") != checksum or entry.get("bytes") != size:
            entry["sha256"], entry["bytes"] = checksum, size
            updated += 1
    manifest_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return updated


def render_attribution(fixtures: list[Fixture]) -> str:
    lines = [
        "# Corpus attribution",
        "",
        "Generated by `make corpus` from `manifest.json` — do not edit.",
        "",
        "Fixtures marked *generated* are produced by `generate.py` and carry this",
        "project's licence. Curated fixtures keep the licence of their source, which is",
        "reproduced here so the requirement travels with the files (ADR-0015).",
        "",
    ]

    for licence in sorted({fixture.license for fixture in fixtures}):
        lines += [f"## {licence}", ""]
        for fixture in sorted(fixtures, key=lambda item: item.path):
            if fixture.license != licence:
                continue
            if fixture.origin == CURATED:
                source = fixture.source
                lines.append(
                    f"- `{fixture.path}` — {source.get('author', 'unknown')}, "
                    f"<{source.get('url', '')}> (retrieved {source.get('retrieved', '')})"
                )
            else:
                lines.append(f"- `{fixture.path}` — generated by `{fixture.generator}`")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="recompute hashes and sizes")
    parser.add_argument("--attribution", type=Path, help="write the attribution notice here")
    args = parser.parse_args(argv)

    if args.refresh:
        print(f"refreshed {refresh()} manifest entr(ies)")

    findings = validate()
    for finding in findings:
        print(finding.render(), file=sys.stderr)

    if args.attribution is not None and not findings:
        _, fixtures = load_manifest()
        args.attribution.write_text(render_attribution(fixtures), encoding="utf-8")
        print(f"wrote {args.attribution}")

    _, fixtures = load_manifest()
    total = sum(fixture.bytes for fixture in fixtures)
    print(f"\n{len(fixtures)} fixtures, {total} bytes: {len(findings)} error(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
