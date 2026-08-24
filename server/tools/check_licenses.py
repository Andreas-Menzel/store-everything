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
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
#: Every Python package this repository *distributes*. The extractor SDK is one: the reference
#: extractor image ships it and its dependency, so it is in scope for exactly the reason the
#: core is (ADR-0016). Development tooling in either package is not.
PYTHON_ROOTS = (REPO_ROOT / "server", REPO_ROOT / "extractors")
POLICY_PATH = REPO_ROOT / "license-allowlist.json"

#: Where a published image installs operating-system packages. Those are distributed too — a
#: Debian package baked into an image we push is an artifact we ship — and no lockfile mentions
#: them, so the Dockerfiles are the inventory (ADR-0016).
DOCKERFILES = ("server/Dockerfile", "extractors/Dockerfile", "extractors/Dockerfile.ocr")

#: `apt-get install` with its flags, up to the end of the command. Flags are dropped, package
#: names are kept; a name pinned as `pkg=1.2` keeps only the name.
_APT_INSTALL = re.compile(r"apt-get\s+install\b(?P<arguments>[^&|;]*)", re.DOTALL)

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


#: Dumps the licence fields of everything installed, as JSON. Run *inside* each package's own
#: environment: a dependency of the extractor image is not installed in the core's virtualenv,
#: and asking the wrong environment answers `UNKNOWN` for a package that states its licence
#: perfectly well. One script, three fields, no policy — the verdict stays in this file.
_METADATA_DUMP = """
import json
import re
import re
from importlib.metadata import distributions

found = {}
for distribution in distributions():
    md = distribution.metadata
    # PEP 503 normalisation, because a wheel calls itself `pydantic_core` while a lockfile
    # calls it `pydantic-core`, and looking the wrong one up answers "unknown licence".
    name = re.sub(r"[-_.]+", "-", (md["Name"] or "")).lower()
    if not name:
        continue
    found[name] = {
        "expression": md.get("License-Expression") or "",
        "classifiers": [c for c in md.get_all("Classifier") or [] if c.startswith("License ::")],
        "declared": md.get("License") or "",
    }
print(json.dumps(found))
"""


def _normalized(name: str) -> str:
    """PEP 503's project-name normalisation, so both sides of the lookup spell it the same."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed_licences(root: Path) -> dict[str, dict[str, Any]]:
    """The licence fields of every package installed for one project."""
    return json.loads(_run(["uv", "run", "--no-sync", "python", "-c", _METADATA_DUMP], cwd=root))


def _statement(fields: dict[str, Any] | None) -> str:
    """Best available licence statement, preferring the machine-readable forms."""
    if fields is None:
        return UNKNOWN

    expression = str(fields.get("expression") or "").strip()
    if expression:
        return expression

    classifiers = sorted(
        {str(classifier).split("::")[-1].strip() for classifier in fields.get("classifiers") or []}
    )
    if classifiers:
        return "; ".join(classifiers)

    declared = str(fields.get("declared") or "").strip()
    if declared:
        # Some projects paste the whole licence text into this field.
        return declared.splitlines()[0][:100]
    return UNKNOWN


def python_dependencies() -> list[Dependency]:
    """Runtime dependencies of every distributed Python package, as the lockfiles resolve them."""
    dependencies: list[Dependency] = []
    for root in PYTHON_ROOTS:
        installed = _installed_licences(root)
        exported = _run(
            [
                "uv",
                "export",
                "--no-dev",
                # Extras included: an optional dependency an official image installs is one this
                # repository distributes, whatever the manifest calls it. `preview-gen`'s imaging
                # stack is an extra precisely so third-party SDK users do not inherit it, and it
                # would be exactly the wrong thing to hide from the licence gate (ADR-0016).
                "--all-extras",
                "--no-emit-project",
                "--no-hashes",
                "--no-annotate",
                "--format",
                "requirements-txt",
            ],
            cwd=root,
        )

        for line in exported.splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#") or "==" not in entry:
                continue
            name, _, remainder = entry.partition("==")
            name = name.split("[", 1)[0].strip()
            # `uv export` appends the environment marker: `tzdata==2026.3 ; sys_platform == ...`
            version = remainder.split(";", 1)[0].strip()
            licence = _statement(installed.get(_normalized(name)))
            dependencies.append(Dependency("python", name, version, licence))
    return sorted(set(dependencies))


def system_packages(dockerfiles: tuple[str, ...] = DOCKERFILES) -> list[Dependency]:
    """Every OS package a published image installs, read out of the Dockerfiles that install it.

    There is no metadata to read here — an apt package's licence lives in the distribution, not in
    anything we can query offline — so the policy carries the statement and this only finds the
    names. That is the useful half: a package added to an image without a licence decision is
    exactly what the gate is for, and it is the failure that would otherwise ship silently.
    """
    found: list[Dependency] = []
    for relative in dockerfiles:
        path = REPO_ROOT / relative
        if path.exists():
            found += [
                Dependency("system", name, "", UNKNOWN)
                for name in apt_packages(path.read_text(encoding="utf-8"))
            ]
    return sorted(set(found))


def apt_packages(dockerfile: str) -> list[str]:
    """The package names an `apt-get install` in this Dockerfile names.

    A real one is a line continuation carrying flags, so the continuations are joined first and
    the flags dropped; `pkg=1.2` keeps the name, because the name is what a licence is declared
    against.
    """
    names: list[str] = []
    joined = dockerfile.replace("\\\n", " ")
    for match in _APT_INSTALL.finditer(joined):
        for word in match.group("arguments").split():
            if word.startswith(("-", "#")):
                continue
            name = word.split("=", 1)[0].strip()
            if name and name not in names:
                names.append(name)
    return names


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
    # System packages are declared the same way, under their own section so a reader can see at a
    # glance what the images install beyond the wheels.
    for key, value in document.get("systemPackages", {}).items():
        license_name = str(value.get("license", "")).strip()
        if not license_name:
            raise PolicyError(f"system package {key!r} must declare a licence")
        declared[f"system:{key}"] = license_name

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
    for ecosystem in ("python", "javascript", "system"):
        in_ecosystem = [d for d in dependencies if d.ecosystem == ecosystem]
        if not in_ecosystem:
            continue
        lines += [f"## {ecosystem}", "", "| Package | Version | Licence |", "|---|---|---|"]
        lines += [f"| {d.name} | {d.version or '—'} | {d.license} |" for d in in_ecosystem]
        lines += [""]
    return "\n".join(lines)


def collect(pnpm: str = "pnpm", policy_path: Path = POLICY_PATH) -> list[Dependency]:
    """Every distributed dependency, with human-declared licences applied."""
    _, declared = load_policy(policy_path)
    found = python_dependencies() + javascript_dependencies(pnpm) + system_packages()
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
