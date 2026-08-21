"""The control directory: exactly one thing appears in a user's tree, and nothing else changes.

The promise this file guards is the uncomfortable one in ADR-0018. Staging has to live inside
the workspace root so that committing a write is a rename rather than a copy — which means the
app writes into a directory the user owns and browses over SMB. The bargain is that it writes
**one** directory, never touches anything else, and leaves no debris behind.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from store_everything import names, workspacefs

WORKSPACE_ID = uuid4()
OPERATION_ID = uuid4()
CREATED_AT = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)


def plant(root: Path, *, placement: str = "adopted") -> None:
    workspacefs.materialize(
        root,
        workspace_id=WORKSPACE_ID,
        placement=placement,
        created_at=CREATED_AT,
        operation_id=OPERATION_ID,
    )


def fingerprint(root: Path) -> dict[str, str]:
    """Every file under `root`, by relative path and content digest."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_it_creates_the_control_directory_and_its_staging_area(tmp_path: Path) -> None:
    plant(tmp_path)

    assert workspacefs.control_directory(tmp_path).is_dir()
    assert workspacefs.staging_directory(tmp_path).is_dir()
    assert workspacefs.marker_path(tmp_path).is_file()


def test_the_marker_identifies_the_workspace(tmp_path: Path) -> None:
    """This is the whole reason the marker exists: a tree that turns up after a restore."""
    plant(tmp_path, placement="managed")

    marker = workspacefs.read_marker(tmp_path)

    assert marker is not None
    assert marker.workspace_id == WORKSPACE_ID
    assert marker.placement == "managed"
    assert marker.created_at == CREATED_AT.isoformat()


def test_the_marker_is_readable_by_a_human(tmp_path: Path) -> None:
    """Someone will find this file while browsing their NAS; it should explain itself."""
    plant(tmp_path)

    text = workspacefs.marker_path(tmp_path).read_text(encoding="utf-8")

    assert "store-everything" in text
    assert "authoritative" in text
    assert text.endswith("\n")


def test_a_tree_with_no_marker_reports_none(tmp_path: Path) -> None:
    assert workspacefs.read_marker(tmp_path) is None


def test_a_corrupt_marker_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    """ "Unreadable" and "never seen" must not look the same: one is a bug to investigate."""
    plant(tmp_path)
    workspacefs.marker_path(tmp_path).write_text("{ not json", encoding="utf-8")

    with pytest.raises(workspacefs.MarkerError):
        workspacefs.read_marker(tmp_path)


def test_planting_twice_changes_nothing(tmp_path: Path) -> None:
    """Provisioning is a leased operation that a crash can replay."""
    plant(tmp_path)
    before = fingerprint(tmp_path)

    plant(tmp_path)

    assert fingerprint(tmp_path) == before


def test_it_leaves_no_staging_debris(tmp_path: Path) -> None:
    """The marker is staged and renamed; a leftover `.partial` would be a janitor's problem."""
    plant(tmp_path)

    staged = list(workspacefs.staging_directory(tmp_path).iterdir())

    assert staged == []


def test_nothing_else_in_the_tree_is_touched(tmp_path: Path) -> None:
    """The adopted-tree promise, asserted byte for byte (F-001/AC-9's rigor, one level down)."""
    (tmp_path / "holiday").mkdir()
    (tmp_path / "holiday" / "beach.jpg").write_bytes(b"pretend this is a photo")
    (tmp_path / "notes.txt").write_bytes(b"do not touch")
    before = fingerprint(tmp_path)
    entries_before = {path.name for path in tmp_path.iterdir()}

    plant(tmp_path)

    after = fingerprint(tmp_path)
    assert {path: digest for path, digest in after.items() if not path.startswith(".")} == before
    # Exactly one new entry, and it is the one we are allowed to add.
    assert {path.name for path in tmp_path.iterdir()} - entries_before == {names.CONTROL_DIRECTORY}


def test_a_missing_root_is_refused_rather_than_created(tmp_path: Path) -> None:
    """For an adopted placement a missing root means the mount is gone. Creating one would
    turn that into "the workspace is empty", which a later scan would reconcile by deleting
    every file it knows about."""
    absent = tmp_path / "not-mounted"

    with pytest.raises(NotADirectoryError):
        plant(absent)

    assert not absent.exists()
