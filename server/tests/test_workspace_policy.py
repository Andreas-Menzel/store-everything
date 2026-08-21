"""The placement policy: which directory may become a workspace root, and where ours go.

No database and no app: `resolve_adopted_root` is the function that stands between a request
field and the filesystem, so its refusals are worth testing one at a time rather than only
through the endpoint that calls it. Every branch that is not "allowed" must raise — a
fall-through here is a path-traversal bug.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from store_everything import names, workspaces
from store_everything.config import Settings
from store_everything.filestore import ContainmentError
from tests.conftest import make_settings


def settings_for(tmp_path: Path, *allowed: Path) -> Settings:
    return make_settings(
        data_root=tmp_path / "managed",
        app_data_root=tmp_path / "app-data",
        adoption_roots=tuple(allowed),
    )


# ------------------------------------------------------------------------ adoption


def test_the_allow_listed_directory_itself_may_be_adopted(tmp_path: Path) -> None:
    """An operator who lists exactly the tree they mean should not have to list its parent."""
    allowed = tmp_path / "nas"
    allowed.mkdir()

    assert workspaces.resolve_adopted_root(settings_for(tmp_path, allowed), str(allowed)) == allowed


def test_a_directory_below_an_allow_listed_root_may_be_adopted(tmp_path: Path) -> None:
    allowed = tmp_path / "nas"
    (allowed / "photos" / "2026").mkdir(parents=True)

    resolved = workspaces.resolve_adopted_root(
        settings_for(tmp_path, allowed), str(allowed / "photos" / "2026")
    )

    assert resolved == allowed / "photos" / "2026"


def test_a_relative_path_is_refused(tmp_path: Path) -> None:
    """Resolved against the process's working directory, it would mean something nobody chose."""
    allowed = tmp_path / "nas"
    allowed.mkdir()

    with pytest.raises(workspaces.AdoptionRefusedError, match="absolute"):
        workspaces.resolve_adopted_root(settings_for(tmp_path, allowed), "nas/photos")


def test_an_over_long_path_is_refused(tmp_path: Path) -> None:
    """The limit is ours so it fails predictably, rather than at the filesystem's whim."""
    allowed = tmp_path / "nas"
    allowed.mkdir()
    absurd = "/" + "x" * (names.MAX_PATH_BYTES + 1)

    with pytest.raises(workspaces.AdoptionRefusedError, match="bytes"):
        workspaces.resolve_adopted_root(settings_for(tmp_path, allowed), absurd)


def test_a_file_is_not_a_workspace_root(tmp_path: Path) -> None:
    allowed = tmp_path / "nas"
    allowed.mkdir()
    (allowed / "notes.txt").write_bytes(b"not a directory")

    with pytest.raises(workspaces.AdoptionRefusedError, match="not a directory"):
        workspaces.resolve_adopted_root(settings_for(tmp_path, allowed), str(allowed / "notes.txt"))


def test_a_path_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    """Adoption indexes what is there; a typo must not create an empty workspace."""
    allowed = tmp_path / "nas"
    allowed.mkdir()

    with pytest.raises(workspaces.AdoptionRefusedError, match="not a directory"):
        workspaces.resolve_adopted_root(settings_for(tmp_path, allowed), str(allowed / "absent"))


def test_a_sibling_with_a_shared_prefix_is_not_inside(tmp_path: Path) -> None:
    """`/a/bc` is not inside `/a/b` — which a prefix comparison on strings would get wrong."""
    allowed = tmp_path / "nas"
    allowed.mkdir()
    sibling = tmp_path / "nastier"
    sibling.mkdir()

    with pytest.raises(workspaces.AdoptionRefusedError, match="SE_ADOPTION_ROOTS"):
        workspaces.resolve_adopted_root(settings_for(tmp_path, allowed), str(sibling))


# ------------------------------------------------------------------ managed placement


def test_a_managed_root_follows_the_documented_layout(tmp_path: Path) -> None:
    """03 § storage layout: the workspace directory carries its name, the owner's its id."""
    settings = settings_for(tmp_path)
    owner = uuid4()

    root = workspaces.managed_root(settings, owner_id=owner, name="Photos")

    assert root == settings.data_root / "users" / str(owner) / "workspaces" / "Photos" / "data"


def test_a_managed_root_stays_inside_the_data_root(tmp_path: Path) -> None:
    """The second line of defence, exercised directly.

    A name like this cannot arrive through the API — `names.validate_name` refuses `/`
    outright — which is exactly why the containment check is worth testing here: it is the
    guard that would still hold if a future caller forgot the first one.
    """
    settings = settings_for(tmp_path)

    with pytest.raises(ContainmentError):
        workspaces.managed_root(settings, owner_id=uuid4(), name="../../../../../../escape")
