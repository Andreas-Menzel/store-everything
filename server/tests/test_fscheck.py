"""The filesystem probe: does it pass what it should and refuse what it must?

The probe is a gate on workspace creation (ADR-0019), so both directions matter. A probe that
passes everything is decoration; one that fails a perfectly good local filesystem would stop
the product working at all.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from store_everything import fscheck

REQUIRED = {
    "file-fsync",
    "directory-fsync",
    "rename-onto-existing",
    "consistent-listing",
    "staging-same-device",
}


def test_an_ordinary_directory_is_usable(tmp_path: Path) -> None:
    verdict = fscheck.probe(tmp_path)

    assert verdict.usable, verdict.explain()
    assert {item.name for item in verdict.properties} == REQUIRED


def test_the_probe_reports_what_it_learned_about_names(tmp_path: Path) -> None:
    """Not failures — behaviour that changes what "the same name" means (ADR-0019)."""
    verdict = fscheck.probe(tmp_path)

    assert set(verdict.facts) == {"case_sensitivity", "unicode"}
    assert verdict.facts["case_sensitivity"] in {"folds case", "case-sensitive"}


def test_the_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    """It runs against a directory a user owns, so it must not litter it."""
    before = set(tmp_path.iterdir())

    fscheck.probe(tmp_path)

    assert set(tmp_path.iterdir()) == before


def test_a_missing_directory_is_refused_with_a_reason(tmp_path: Path) -> None:
    verdict = fscheck.probe(tmp_path / "absent")

    assert not verdict.usable
    assert verdict.error == "not a directory"
    assert "not a directory" in verdict.explain()


def test_a_file_is_not_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "a-file"
    path.write_bytes(b"")

    assert not fscheck.probe(path).usable


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_directory_is_refused(tmp_path: Path) -> None:
    """The most common real failure: a mount the app user cannot write to."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        verdict = fscheck.probe(locked)

        assert not verdict.usable
        assert verdict.error is not None
        assert "probe directory" in verdict.error
    finally:
        locked.chmod(0o700)


def test_the_verdict_explains_which_property_failed() -> None:
    """An operator has to learn *what* is wrong, not only that something is."""
    verdict = fscheck.Verdict(
        root=Path("/mnt/nas"),
        properties=(
            fscheck.Property("file-fsync", True),
            fscheck.Property("directory-fsync", False, "Invalid argument"),
        ),
    )

    assert not verdict.usable
    assert verdict.failures == (verdict.properties[1],)
    assert "directory-fsync (Invalid argument)" in verdict.explain()
