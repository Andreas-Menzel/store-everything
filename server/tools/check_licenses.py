"""Third-party licence gate and notice generator (ADR-0016).

The repository is public and AGPL-3.0 licensed, so every dependency we *distribute*
must carry a licence we can distribute under. Development-only tooling is out of scope:
it is never shipped.

    python -m tools.check_licenses              # fail on anything outside the policy
    python -m tools.check_licenses --notice P   # write the third-party notice to P
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
POLICY_PATH = REPO_ROOT / "license-allowlist.json"

UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, order=True)
class Dependency:
    ecosystem: str
    name: str
    version: str
    license: str

    @property
    def key(self) -> str:
        return f"{self.ecosystem}:{self.name}"


class PolicyError(RuntimeError):
    """The policy file itself is unusable."""


def _run(command: list[str], cwd: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed:\n{result.stderr.strip()}")
    return result.stdout


def _python_license(distribution: str) -> str:
    """Best available licence statement, preferring the machine-readable forms."""
    try:
        md = metadata(distribution)
    except PackageNotFoundError:
        return UNKNOWN

    expression = md.get("License-Expression")
    if expression:
        return expression.strip()

    classifiers = sorted(
        {
            classifier.split("::")[-1].strip()
            for classifier in md.get_all("Classifier") or []
            if classifier.startswith("License ::")
        }
    )
    if classifiers:
        return "; ".join(classifiers)

    declared = (md.get("License") or "").strip()
    if declared:
        # Some projects paste the whole licence text into this field.
        return declared.splitlines()[0][:100]
    return UNKNOWN


def python_dependencies() -> list[Dependency]:
    """Runtime dependencies of the core service, as the lockfile resolves them."""
    exported = _run(
        [
            "uv",
            "export",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--no-annotate",
            "--format",
            "requirements-txt",
        ],
        cwd=SERVER_ROOT,
    )

    dependencies: list[Dependency] = []
    for line in exported.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "==" not in entry:
            continue
        name, _, remainder = entry.partition("==")
        name = name.split("[", 1)[0].strip()
        # `uv export` appends the environment marker: `tzdata==2026.3 ; sys_platform == ...`
        version = remainder.split(";", 1)[0].strip()
        dependencies.append(Dependency("python", name, version, _python_license(name)))
    return sorted(set(dependencies))


def javascript_dependencies(pnpm: str = "pnpm") -> list[Dependency]:
    """Runtime dependencies of the web client."""
    raw = _run([pnpm, "licenses", "list", "--prod", "--json"], cwd=REPO_ROOT)
    if not raw.strip():
        return []

    grouped: dict[str, list[dict[str, Any]]] = json.loads(raw)
    dependencies: list[Dependency] = []
    for license_name, packages in grouped.items():
        for package in packages:
            for version in package.get("versions", []):
                dependencies.append(
                    Dependency(
                        "javascript",
                        str(package["name"]),
                        str(version),
                        (license_name or UNKNOWN).strip(),
                    )
                )
    return sorted(set(dependencies))


def load_policy(path: Path = POLICY_PATH) -> tuple[set[str], dict[str, str]]:
    """Return the allowed licence expressions and the per-package declared licences.

    A package exception exists for dependencies whose metadata cannot be read on every
    host — platform-conditional ones are not installed elsewhere — so the licence is
    recorded by a human instead of guessed. The verdict is then the same everywhere.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PolicyError(f"missing licence policy at {path}") from error

    allowed = {str(entry).strip() for entry in document.get("allowed", [])}
    declared: dict[str, str] = {}
    for key, value in document.get("packageExceptions", {}).items():
        license_name = str(value.get("license", "")).strip()
        if not license_name:
            raise PolicyError(f"package exception {key!r} must declare a licence")
        declared[str(key)] = license_name

    if not allowed:
        raise PolicyError(f"{path} allows nothing; that cannot be intended")
    return allowed, declared


def resolve(dependency: Dependency, declared: dict[str, str]) -> Dependency:
    """Apply the human-declared licence where metadata could not supply one."""
    override = declared.get(dependency.key)
    if override is None:
        return dependency
    return Dependency(dependency.ecosystem, dependency.name, dependency.version, override)


def violations(dependencies: list[Dependency], policy_path: Path = POLICY_PATH) -> list[Dependency]:
    allowed, declared = load_policy(policy_path)
    return [
        resolved
        for resolved in (resolve(dependency, declared) for dependency in dependencies)
        if resolved.license not in allowed
    ]


def render_notice(dependencies: list[Dependency]) -> str:
    lines = [
        "# Third-party licences",
        "",
        "Generated by `make notice` — do not edit. Lists the dependencies distributed",
        "with Store Everything, which is itself licensed under AGPL-3.0-only.",
        "",
    ]
    for ecosystem in ("python", "javascript"):
        in_ecosystem = [d for d in dependencies if d.ecosystem == ecosystem]
        if not in_ecosystem:
            continue
        lines += [f"## {ecosystem}", "", "| Package | Version | Licence |", "|---|---|---|"]
        lines += [f"| {d.name} | {d.version} | {d.license} |" for d in in_ecosystem]
        lines += [""]
    return "\n".join(lines)


def collect(pnpm: str = "pnpm", policy_path: Path = POLICY_PATH) -> list[Dependency]:
    """Every distributed dependency, with human-declared licences applied."""
    _, declared = load_policy(policy_path)
    found = python_dependencies() + javascript_dependencies(pnpm)
    return [resolve(dependency, declared) for dependency in found]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notice", type=Path, help="write the third-party notice here")
    parser.add_argument("--pnpm", default="pnpm", help="pnpm executable to use")
    parser.add_argument(
        "--policy",
        type=Path,
        default=POLICY_PATH,
        help="licence policy to enforce (used by the gate self-test)",
    )
    args = parser.parse_args(argv)

    dependencies = collect(args.pnpm, args.policy)

    if args.notice is not None:
        args.notice.write_text(render_notice(dependencies), encoding="utf-8")
        print(f"wrote {args.notice} ({len(dependencies)} dependencies)")
        return 0

    offending = violations(dependencies, args.policy)
    if offending:
        print("Dependencies outside the licence policy:", file=sys.stderr)
        for dependency in offending:
            print(
                f"  {dependency.ecosystem:<10} {dependency.name}@{dependency.version}"
                f"  →  {dependency.license}",
                file=sys.stderr,
            )
        print(
            f"\nAdd the licence to {POLICY_PATH.name} if it is compatible with "
            "AGPL-3.0 distribution, or replace the dependency.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(dependencies)} distributed dependencies, all within the licence policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
