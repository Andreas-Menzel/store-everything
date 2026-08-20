"""The corpus must be trustworthy: complete, accounted for, and actually usable."""

from __future__ import annotations

import json
import unicodedata
import zipfile
from pathlib import Path

import pytest
from tools.corpus import (
    FIXTURES_ROOT,
    load_manifest,
    render_attribution,
    validate,
)
from tools.specdocs import Finding

ATTRIBUTION = FIXTURES_ROOT.parent / "ATTRIBUTION.md"


def errors(findings: list[Finding]) -> list[str]:
    return [finding.message for finding in findings if finding.level == "error"]


def write_manifest(tmp_path: Path, fixtures: list[dict[str, object]]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"budget": {"total_bytes": 1000, "max_file_bytes": 100}, "fixtures": fixtures}),
        encoding="utf-8",
    )
    return path


def entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": "probe.txt",
        "origin": "generated",
        "generator": "corpus/generate.py",
        "license": "AGPL-3.0-only",
        "asserts": "Contains the word probe.",
        "sha256": "",
        "bytes": 0,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- the committed corpus


def test_the_committed_corpus_is_valid() -> None:
    assert errors(validate()) == []


def test_the_attribution_notice_is_in_sync() -> None:
    """It ships beside the fixtures, so a stale copy is a licensing problem."""
    _, fixtures = load_manifest()

    assert ATTRIBUTION.read_text(encoding="utf-8") == render_attribution(fixtures)


def test_every_fixture_states_what_it_proves() -> None:
    _, fixtures = load_manifest()

    assert all(fixture.asserts.strip() for fixture in fixtures)


# ------------------------------------------------------------------ the gate can fail


def test_an_unlisted_file_is_rejected(tmp_path: Path) -> None:
    """A fixture nobody documented is a fixture nobody can trust."""
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "stray.txt").write_text("hello", encoding="utf-8")

    findings = validate(write_manifest(tmp_path, []), root)

    assert any("not listed" in message for message in errors(findings))


def test_a_missing_file_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()

    findings = validate(write_manifest(tmp_path, [entry()]), root)

    assert any("does not exist" in message for message in errors(findings))


def test_a_stale_hash_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "probe.txt").write_text("probe", encoding="utf-8")

    manifest = write_manifest(tmp_path, [entry(sha256="0" * 64, bytes=5)])

    assert any("sha256 does not match" in message for message in errors(validate(manifest, root)))


def test_a_fixture_over_the_size_cap_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "probe.txt").write_bytes(b"x" * 500)

    manifest = write_manifest(tmp_path, [entry(bytes=500)])
    findings = validate(manifest, root)

    assert any("per-file cap" in message for message in errors(findings))


def test_a_fixture_without_a_licence_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "probe.txt").write_text("probe", encoding="utf-8")

    findings = validate(write_manifest(tmp_path, [entry(license="")]), root)

    assert any("declares no licence" in message for message in errors(findings))


def test_a_curated_fixture_without_provenance_is_rejected(tmp_path: Path) -> None:
    """Redistributing someone else's work requires knowing whose, and from where."""
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "probe.txt").write_text("probe", encoding="utf-8")

    findings = validate(
        write_manifest(tmp_path, [entry(origin="curated", source={"url": "https://example"})]),
        root,
    )
    messages = errors(findings)

    assert any("no author" in message for message in messages)
    assert any("no retrieved" in message for message in messages)


# ------------------------------------------------------------- the fixtures are usable


def test_the_text_fixture_holds_its_asserted_truth() -> None:
    lines = (FIXTURES_ROOT / "text/known-phrases.txt").read_text(encoding="utf-8").splitlines()

    assert lines[2] == "The quick brown fox jumps over the lazy dog."
    assert sum(line.count("xylophone marmalade") for line in lines) == 1


def test_the_zip_slip_fixture_really_escapes() -> None:
    with zipfile.ZipFile(FIXTURES_ROOT / "adversarial/zip-slip.zip") as archive:
        names = archive.namelist()

    escaping = [name for name in names if ".." in Path(name).parts]

    assert escaping, "the fixture must contain entries that traverse upwards"


def test_the_zero_byte_fixture_is_empty() -> None:
    assert (FIXTURES_ROOT / "adversarial/zero-byte.bin").stat().st_size == 0


def test_hostile_names_are_described_rather_than_committed() -> None:
    """Committing both members would break checkout on case-insensitive volumes."""
    described = json.loads(
        (FIXTURES_ROOT / "adversarial/hostile-names.json").read_text(encoding="utf-8")
    )

    assert described["case_collisions"], "case collisions must be described"
    for case in described["case_collisions"]:
        assert len({name.lower() for name in case["names"]}) == 1
        assert len(set(case["names"])) == 2

    for case in described["unicode_normalisation"]:
        spellings = {unicodedata.normalize("NFC", name) for name in case["names"]}
        assert len(spellings) == 1, "the pair must be one name in two normalisations"


def test_hostile_names_can_be_materialised_where_the_filesystem_allows(tmp_path: Path) -> None:
    """The runtime half of the pattern: create the pair, or skip honestly."""
    described = json.loads(
        (FIXTURES_ROOT / "adversarial/hostile-names.json").read_text(encoding="utf-8")
    )
    first, second = described["case_collisions"][0]["names"]

    (tmp_path / first).write_text("one", encoding="utf-8")
    (tmp_path / second).write_text("two", encoding="utf-8")

    if len(list(tmp_path.iterdir())) == 1:
        pytest.skip("this filesystem folds case; the collision cannot be represented here")

    assert (tmp_path / first).read_text(encoding="utf-8") == "one"
