"""The licence gate must actually fail — a check that cannot go red is decoration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.check_licenses import (
    Dependency,
    PolicyError,
    load_policy,
    render_notice,
    violations,
)

PERMISSIVE = Dependency("python", "fastapi", "0.120.0", "MIT")
COPYLEFT = Dependency("python", "some-gpl-lib", "1.0.0", "GPL-3.0-only")
UNREADABLE = Dependency("python", "colorama", "0.4.6", "UNKNOWN")


def write_policy(tmp_path: Path, document: dict[str, object]) -> Path:
    path = tmp_path / "license-allowlist.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_incompatible_licence_is_reported(tmp_path: Path) -> None:
    policy = write_policy(tmp_path, {"allowed": ["MIT"]})

    assert violations([PERMISSIVE, COPYLEFT], policy) == [COPYLEFT]


def test_allowed_licence_passes(tmp_path: Path) -> None:
    policy = write_policy(tmp_path, {"allowed": ["MIT"]})

    assert violations([PERMISSIVE], policy) == []


def test_unreadable_metadata_fails_until_a_human_declares_it(tmp_path: Path) -> None:
    """A dependency we cannot read must not slip through as "probably fine"."""
    policy = write_policy(tmp_path, {"allowed": ["MIT", "BSD-3-Clause"]})

    assert violations([UNREADABLE], policy) == [UNREADABLE]


def test_declared_licence_resolves_an_unreadable_dependency(tmp_path: Path) -> None:
    policy = write_policy(
        tmp_path,
        {
            "allowed": ["BSD-3-Clause"],
            "packageExceptions": {
                "python:colorama": {"license": "BSD-3-Clause", "reason": "windows-only"}
            },
        },
    )

    assert violations([UNREADABLE], policy) == []


def test_an_exception_without_a_licence_is_rejected(tmp_path: Path) -> None:
    """Silencing a package without naming its licence would defeat the gate."""
    policy = write_policy(
        tmp_path,
        {"allowed": ["MIT"], "packageExceptions": {"python:colorama": {"reason": "trust me"}}},
    )

    with pytest.raises(PolicyError, match="must declare a licence"):
        load_policy(policy)


def test_an_empty_policy_is_rejected(tmp_path: Path) -> None:
    policy = write_policy(tmp_path, {"allowed": []})

    with pytest.raises(PolicyError, match="allows nothing"):
        load_policy(policy)


def test_a_missing_policy_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="missing licence policy"):
        load_policy(tmp_path / "absent.json")


def test_the_notice_lists_every_ecosystem() -> None:
    notice = render_notice([PERMISSIVE, Dependency("javascript", "vue", "3.5.41", "MIT")])

    assert "## python" in notice
    assert "## javascript" in notice
    assert "| fastapi | 0.120.0 | MIT |" in notice
    assert "| vue | 3.5.41 | MIT |" in notice


def test_the_real_policy_is_loadable() -> None:
    """The committed policy is itself checked: a typo there would disable the gate."""
    allowed, declared = load_policy()

    assert "MIT" in allowed
    assert all(license_name for license_name in declared.values())
