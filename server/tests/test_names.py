"""The name policy — the rules that decide when two names are one name.

No database and no filesystem: these are properties of the rules themselves, and they are the
ones a subtle mistake would break quietly. A comparison key that is not stable, or that fails
to equate the NFC and NFD spellings of one name, produces exactly the phantom duplicates
ADR-0019 exists to prevent — and it produces them months later, on someone's Mac, over SMB.
"""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from store_everything import names

# ------------------------------------------------------------------ comparison key


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Foo.txt", "foo.txt"),
        ("REPORT", "report"),
        ("Straße", "STRASSE"),
        # The macOS-over-SMB case: one visible name, two byte sequences.
        (unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")),
    ],
)
def test_names_that_must_collide_share_a_key(first: str, second: str) -> None:
    assert names.comparison_key(first) == names.comparison_key(second)


@pytest.mark.parametrize(("first", "second"), [("report", "reports"), ("a", "b"), ("é", "e")])
def test_different_names_keep_different_keys(first: str, second: str) -> None:
    assert names.comparison_key(first) != names.comparison_key(second)


@given(st.text(max_size=60))
def test_the_key_is_stable(value: str) -> None:
    """`key(key(x)) == key(x)`.

    Load-bearing: the key is stored in a column and compared against keys derived elsewhere,
    so a key that folds differently on a second pass would make lookups miss rows that are
    already there.
    """
    once = names.comparison_key(value)

    assert names.comparison_key(once) == once


@given(st.text(max_size=60))
def test_normalization_never_changes_the_key(value: str) -> None:
    """Whichever spelling arrives, the key is the same — the whole point of having one."""
    composed = unicodedata.normalize("NFC", value)
    decomposed = unicodedata.normalize("NFD", value)

    assert names.comparison_key(composed) == names.comparison_key(decomposed)


def test_the_key_is_normalized() -> None:
    """Stored keys are NFC, so a byte comparison in SQL means what the policy means."""
    key = names.comparison_key(unicodedata.normalize("NFD", "Café"))

    assert key == unicodedata.normalize("NFC", key)


# ------------------------------------------------------------------------- API names


def test_an_api_name_is_normalized_to_nfc() -> None:
    decomposed = unicodedata.normalize("NFD", "café")

    stored = names.normalize_api_name(decomposed)

    assert stored == unicodedata.normalize("NFC", "café")
    assert stored != decomposed


def test_normalizing_preserves_case() -> None:
    """Stored as given: normalization is not folding, and a name is the user's to choose."""
    assert names.normalize_api_name("MyPhotos") == "MyPhotos"


# ------------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "name", ["photos", "Tax 2026", "a name with spaces", ".hidden", "café", "x" * 255]
)
def test_ordinary_names_are_accepted(name: str) -> None:
    names.validate_name(name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", "empty"),
        (".", "names"),
        ("..", "names"),
        ("a/b", "'/'"),
        ("tab\there", "control"),
        ("nul\0byte", "control"),
        ("x" * 256, "255 bytes"),
        # 200 two-byte characters: inside the 255-*character* limit a naive check would
        # allow, outside the 255-*byte* one the filesystem actually has.
        ("é" * 200, "255 bytes"),
    ],
)
def test_refused_names_name_their_rule(name: str, expected: str) -> None:
    with pytest.raises(names.InvalidNameError) as refused:
        names.validate_name(name)

    assert expected in refused.value.reason
    # The rule, never the value: the reason travels into an API response.
    assert name not in refused.value.reason or len(name) <= 2


def test_the_control_directory_is_reserved_at_a_workspace_root() -> None:
    with pytest.raises(names.InvalidNameError) as refused:
        names.validate_name(names.CONTROL_DIRECTORY, at_root=True)

    assert "reserved" in refused.value.reason


def test_the_reservation_holds_however_it_is_spelled() -> None:
    """A reserved name compared raw would be trivially bypassed by changing its case."""
    with pytest.raises(names.InvalidNameError):
        names.validate_name(".Workspace", at_root=True)


def test_the_control_directory_is_an_ordinary_name_further_down() -> None:
    """Nothing of ours lives below the root, so the name is the user's there."""
    names.validate_name(names.CONTROL_DIRECTORY)


# ---------------------------------------------------------------------------- paths


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("beach.jpg", ("beach.jpg",)),
        ("Photos/2026/beach.jpg", ("Photos", "2026", "beach.jpg")),
        ("a b/c d.txt", ("a b", "c d.txt")),
    ],
)
def test_a_path_splits_into_validated_segments(path: str, expected: tuple[str, ...]) -> None:
    assert names.split_path(path) == expected


def test_a_path_is_normalized_on_the_way_in() -> None:
    decomposed = unicodedata.normalize("NFD", "café/menu.txt")

    assert names.split_path(decomposed) == (unicodedata.normalize("NFC", "café"), "menu.txt")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/absolute", "relative"),
        ("a//b", "empty segment"),
        ("trailing/", "empty segment"),
        ("", "empty"),
        ("../escape", "names"),
        ("a/../b", "names"),
        ("a/./b", "names"),
        (".workspace/anything", "reserved"),
        ("x" * (names.MAX_PATH_BYTES + 1), "4096 bytes"),
    ],
)
def test_a_path_that_is_not_one_is_refused(path: str, expected: str) -> None:
    with pytest.raises(names.InvalidNameError) as refused:
        names.split_path(path)

    assert expected in refused.value.reason


def test_the_control_directory_is_only_reserved_at_the_root() -> None:
    """Deeper down the name is the user's: nothing of ours lives there (ADR-0018)."""
    assert names.split_path("sub/.workspace/notes.txt") == ("sub", ".workspace", "notes.txt")
