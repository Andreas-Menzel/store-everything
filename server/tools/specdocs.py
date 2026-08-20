"""A machine-readable view of the specification documents.

Feature files and the domain invariants are prose written for humans, but their
*structure* is normative: FR ids are the traceability link into the test suite
(features/README.md § Writing FRs). This module is the one parser both the spec lint
and the traceability matrix read them through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = REPO_ROOT / "features"
FEATURE_INDEX = FEATURES_DIR / "README.md"
INVARIANTS_DOC = REPO_ROOT / "specs" / "02-domain-model.md"

# 11-engineering-standards.md § verification methods.
DEFAULT_METHOD = "test"
VERIFICATION_METHODS = frozenset({DEFAULT_METHOD, "benchmark", "fault-injection", "drill"})

FEATURE_FILE = re.compile(r"^F-(\d{3})-[a-z0-9-]+\.md$")
_REQUIREMENT = re.compile(r"^-\s+\*\*FR-(\d+)\*\*\s*(.*)$")
_TOMBSTONE = re.compile(r"^\*\((?:removed|withdrawn)\b[^)]*\)\*")
_METHOD = re.compile(r"\*\(verify:\s*([a-z-]+)\)\*")
_HEADER = re.compile(r"^\*\*(?P<field>[A-Za-z ]+):\*\*\s*(?P<value>.*)$")
_TITLE = re.compile(r"^#\s+(?P<id>F-\d{3})\s+—\s+(?P<title>.+)$")
_INDEX_ROW = re.compile(
    r"^\|\s*\[(?P<id>F-\d{3})\]\([^)]*\)\s*\|(?P<title>[^|]*)\|"
    r"(?P<clients>[^|]*)\|(?P<status>[^|]*)\|(?P<priority>[^|]*)\|"
)
_INVARIANT = re.compile(r"^(\d+)\.\s+(.*)$")

#: Any `F-001/FR-2` reference, wherever it appears in the documentation.
REQUIREMENT_REFERENCE = re.compile(r"\b(F-\d{3})/FR-(\d+)\b")


@dataclass(frozen=True)
class Finding:
    """One problem found by a documentation tool. Shared so both report identically."""

    level: str  # "error" | "warning"
    where: str
    message: str

    def render(self) -> str:
        colour = "\033[31m" if self.level == "error" else "\033[33m"
        return f"  {colour}{self.level}\033[0m  {self.where}: {self.message}"


@dataclass(frozen=True)
class Requirement:
    feature_id: str
    number: int
    text: str
    method: str
    tombstoned: bool
    line: int

    @property
    def id(self) -> str:
        return f"{self.feature_id}/FR-{self.number}"


@dataclass(frozen=True)
class Feature:
    id: str
    path: Path
    title: str
    headers: dict[str, str]
    requirements: tuple[Requirement, ...]

    @property
    def status(self) -> str:
        return self.headers.get("Status", "")

    @property
    def clients(self) -> str:
        return self.headers.get("Clients", "")

    @property
    def priority(self) -> str:
        return self.headers.get("Priority", "")

    @property
    def is_implemented(self) -> bool:
        """`Implemented` is computed by the matrix, never claimed — but a file may claim
        it, and that claim is exactly what the hard gate checks."""
        return self.status.strip().startswith("Implemented")


@dataclass(frozen=True)
class Invariant:
    number: int
    text: str

    @property
    def id(self) -> str:
        return f"02/INV-{self.number}"


@dataclass(frozen=True)
class IndexEntry:
    id: str
    title: str
    clients: str
    status: str
    priority: str


def _parse_requirement(feature_id: str, number: int, remainder: str, line: int) -> Requirement:
    tombstoned = bool(_TOMBSTONE.match(remainder.strip()))
    method_match = _METHOD.search(remainder)
    method = method_match.group(1) if method_match else DEFAULT_METHOD
    text = _METHOD.sub("", remainder).strip()
    return Requirement(feature_id, number, text, method, tombstoned, line)


def parse_feature(path: Path) -> Feature:
    feature_id = path.name[:5]
    title = ""
    headers: dict[str, str] = {}
    requirements: list[Requirement] = []

    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.rstrip()

        title_match = _TITLE.match(line)
        if title_match:
            title = title_match.group("title").strip()
            continue

        header_match = _HEADER.match(line)
        if header_match:
            headers[header_match.group("field").strip()] = header_match.group("value").strip()
            continue

        # An FR bullet is that feature's requirement wherever it appears: F-010 keeps its
        # committed ids under "v1 obligations" rather than "Functional requirements", and
        # other documents cite them. The id is the contract, not the heading above it.
        requirement_match = _REQUIREMENT.match(line)
        if requirement_match:
            requirements.append(
                _parse_requirement(
                    feature_id,
                    int(requirement_match.group(1)),
                    requirement_match.group(2),
                    number,
                )
            )

    return Feature(feature_id, path, title, headers, tuple(requirements))


def load_features(directory: Path = FEATURES_DIR) -> list[Feature]:
    paths = sorted(path for path in directory.glob("F-*.md") if FEATURE_FILE.match(path.name))
    return [parse_feature(path) for path in paths]


def load_index(path: Path = FEATURE_INDEX) -> list[IndexEntry]:
    entries: list[IndexEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _INDEX_ROW.match(line.strip())
        if match:
            entries.append(
                IndexEntry(
                    match.group("id"),
                    match.group("title").strip(),
                    match.group("clients").strip(),
                    match.group("status").strip(),
                    match.group("priority").strip(),
                )
            )
    return entries


def load_invariants(path: Path = INVARIANTS_DOC) -> list[Invariant]:
    invariants: list[Invariant] = []
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line.strip().lower() == "## invariants"
            continue
        if not in_section:
            continue
        match = _INVARIANT.match(line.strip())
        if match:
            invariants.append(Invariant(int(match.group(1)), match.group(2).strip()))
    return invariants


def requirement_index(features: list[Feature]) -> dict[str, Requirement]:
    return {
        requirement.id: requirement for feature in features for requirement in feature.requirements
    }
