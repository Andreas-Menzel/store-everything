"""What a scan makes of one directory's entries — the rules, on every platform.

These are ADR-0019's four decisions about a source tree, asserted where they can be asserted
*anywhere*. The collision rule in particular cannot be exercised against a real directory on a
case-folding filesystem like APFS: the filesystem merges the two names before the scanner ever
sees them. Feeding the classifier its entries directly is what makes the rule testable on a
developer's Mac and on CI's ext4 alike — `test_scanning.py` covers the real-directory path
where the filesystem allows it.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pytest

from store_everything import names, scanning


@dataclass(frozen=True, slots=True)
class Fake:
    """One directory entry, exactly as much of it as the classifier looks at."""

    name: str
    kind: str = "file"
    size: int = 10
    mtime: float = 1_800_000_000.0

    def is_symlink(self) -> bool:
        return self.kind == "symlink"

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self.kind == "dir"

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self.kind == "file"

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        return os.stat_result((0o100644, 0, 0, 1, 0, 0, self.size, 0, int(self.mtime), 0))


def listing(*entries: Fake, at: str = "") -> scanning.Listing:
    """Classify these entries as if they had come from `os.scandir`, already sorted."""
    return scanning.classify(at, sorted(entries, key=lambda entry: entry.name))


def test_files_and_directories_are_separated() -> None:
    result = listing(Fake("notes.txt"), Fake("Photos", kind="dir"))

    assert result.directories == ("Photos",)
    assert [entry.name for entry in result.files] == ["notes.txt"]
    assert result.findings == ()


def test_a_files_size_and_timestamp_come_from_the_filesystem() -> None:
    """Both are what the next stat-scan compares against, so they are read, never assumed."""
    result = listing(Fake("clip.mp4", size=4096, mtime=1_700_000_000.0))

    assert result.files[0].size == 4096
    assert result.files[0].modified_at.year == 2023


@pytest.mark.fr("F-001/FR-12")
def test_a_symlink_is_skipped_whatever_it_points_at() -> None:
    """Never dereferenced — not to see whether it is a file, not to see whether it exists."""
    result = listing(Fake("escape", kind="symlink"), Fake("real.txt"))

    assert result.directories == ()
    assert [entry.name for entry in result.files] == ["real.txt"]
    assert [(finding.kind, finding.path) for finding in result.findings] == [("skipped", "escape")]
    assert "never followed" in result.findings[0].detail


@pytest.mark.fr("F-001/FR-11")
def test_two_names_differing_only_in_case_are_a_conflict() -> None:
    """The first in traversal order registers; the second is reported with both spellings."""
    result = listing(Fake("Report.pdf"), Fake("report.pdf"))

    assert [entry.name for entry in result.files] == ["Report.pdf"]
    conflict = result.findings[0]
    assert (conflict.kind, conflict.path) == ("conflict", "report.pdf")
    assert "'Report.pdf'" in conflict.detail


@pytest.mark.fr("F-001/FR-11")
def test_the_nfc_and_nfd_spellings_of_one_name_are_a_conflict() -> None:
    """The macOS-over-SMB case: one visible name, two byte sequences, and no way for the app
    to tell the user which file it registered unless it says so."""
    composed = unicodedata.normalize("NFC", "café.txt")
    decomposed = unicodedata.normalize("NFD", "café.txt")
    assert composed != decomposed

    result = listing(Fake(composed), Fake(decomposed))

    assert len(result.files) == 1
    assert [finding.kind for finding in result.findings] == ["conflict"]


def test_a_directory_can_collide_with_a_file() -> None:
    """The key is the whole entry's name, so which *kind* of thing claimed it is irrelevant."""
    result = listing(Fake("Photos", kind="dir"), Fake("photos"))

    assert result.directories == ("Photos",)
    assert result.files == ()
    assert [finding.kind for finding in result.findings] == ["conflict"]


def test_the_control_directory_is_invisible_at_the_root() -> None:
    """F-001/FR-13: ours, so not even reported — it is not a fact about the user's tree."""
    result = listing(Fake(names.CONTROL_DIRECTORY, kind="dir"), Fake("notes.txt"), at="")

    assert result.directories == ()
    assert result.findings == ()
    assert [entry.name for entry in result.files] == ["notes.txt"]


def test_the_control_directory_is_an_ordinary_name_deeper_down() -> None:
    """Nothing of ours lives below the root, so the name is the user's there (ADR-0018)."""
    result = listing(Fake(names.CONTROL_DIRECTORY, kind="dir"), at="Photos")

    assert result.directories == (names.CONTROL_DIRECTORY,)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("x" * 256, "255 bytes"), ("é" * 200, "255 bytes"), ("tab\there", "control character")],
)
def test_a_name_the_policy_refuses_is_reported(name: str, expected: str) -> None:
    """Failing here beats failing at the filesystem's whim three layers down."""
    result = listing(Fake(name))

    assert result.files == ()
    assert result.findings[0].kind == "skipped"
    assert expected in result.findings[0].detail


def test_something_that_is_neither_a_file_nor_a_directory_is_skipped() -> None:
    result = listing(Fake("pipe", kind="fifo"))

    assert result.files == ()
    assert "not a regular file" in result.findings[0].detail


def test_paths_are_relative_to_the_workspace_root() -> None:
    """A finding names the path the user would look at, not a name out of context."""
    result = listing(Fake("escape", kind="symlink"), at="Photos/2026")

    assert result.findings[0].path == "Photos/2026/escape"


# ---------------------------------------------------------------- against a real directory


@pytest.mark.fr("F-001/FR-16")
def test_a_missing_directory_is_missing_rather_than_empty(tmp_path: Path) -> None:
    """The distinction the next chunk's reconciliation stands on: *gone* is a fact about the
    tree, and it is not the same fact as "there was nothing in it"."""
    result = scanning.inspect(tmp_path, "not-there")

    assert result.missing is True
    assert result.unreadable is False


@pytest.mark.fr("F-001/FR-16")
def test_an_unreadable_directory_is_not_treated_as_empty(tmp_path: Path) -> None:
    """A directory we cannot read tells us **nothing** about its contents. Reconciling it as
    if everything in it had been deleted would be the worst bug in the scanner."""
    closed = tmp_path / "closed"
    closed.mkdir()
    (closed / "secret.txt").write_bytes(b"still there")
    os.chmod(closed, 0o000)
    try:
        if os.access(closed, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("this process can read a mode-000 directory, so it cannot be tested")
        result = scanning.inspect(tmp_path, "closed")
    finally:
        os.chmod(closed, 0o700)

    assert result.unreadable is True
    assert result.missing is False
    assert result.files == ()
    assert "cannot be read" in result.findings[0].detail


@pytest.mark.fr("F-001/FR-12", "F-001/FR-16")
def test_a_path_that_resolves_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    """Containment is re-checked even for a path that came from our own frontier: a directory
    that became a symlink between two scans is exactly what a lexical check misses."""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    result = scanning.inspect(root, "escape")

    assert result.unreadable is True
    assert "outside the workspace" in result.findings[0].detail


def test_a_real_directory_is_listed_in_sorted_order(tmp_path: Path) -> None:
    """Determinism is not decoration: the conflict rule is stated in terms of this order."""
    for name in ("c.txt", "a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"x")

    result = scanning.inspect(tmp_path, "")

    assert [entry.name for entry in result.files] == ["a.txt", "b.txt", "c.txt"]
