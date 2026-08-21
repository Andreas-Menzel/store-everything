"""The write protocol and the content-addressed store.

These are the guarantees every later feature inherits without restating them: a write is
either fully visible or absent, a retry converges instead of duplicating, and no path ever
escapes the root it was resolved against.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from store_everything import filestore
from store_everything.blobs import BlobStore
from store_everything.filestore import ContainmentError

PAYLOAD = b"the quick brown fox" * 100


# ------------------------------------------------------------------ atomic writes


def test_a_staged_write_lands_atomically(tmp_path: Path) -> None:
    destination = tmp_path / "tree" / "document.txt"
    staging = tmp_path / "staging" / "op.partial"

    filestore.write_atomically(destination, PAYLOAD, staging=staging)

    assert destination.read_bytes() == PAYLOAD
    # Nothing is left behind: the staging file *became* the destination.
    assert not staging.exists()


def test_a_write_replaces_existing_content_without_a_gap(tmp_path: Path) -> None:
    """A reader either sees the old bytes or the new ones — never a truncated file."""
    destination = tmp_path / "document.txt"
    destination.write_bytes(b"the previous content")

    filestore.write_atomically(destination, PAYLOAD, staging=tmp_path / "s" / "op.partial")

    assert destination.read_bytes() == PAYLOAD


def test_a_failed_write_leaves_the_destination_untouched(tmp_path: Path) -> None:
    destination = tmp_path / "document.txt"
    destination.write_bytes(b"still here")
    staging = tmp_path / "s" / "op.partial"

    with (
        pytest.raises(RuntimeError),
        filestore.staged_write(destination, staging=staging) as handle,
    ):
        handle.write(b"never committed")
        raise RuntimeError("the operation failed")

    assert destination.read_bytes() == b"still here"
    # The staging file survives on purpose: a retry reuses it, and the janitor collects it
    # if there is no retry.
    assert staging.exists()


def test_the_staging_path_is_the_same_for_every_attempt(tmp_path: Path) -> None:
    """Deterministic paths are what make a retry converge instead of leaking a file."""
    operation = uuid4()

    first = filestore.staging_path(tmp_path, operation)
    second = filestore.staging_path(tmp_path, operation)

    assert first == second
    assert filestore.is_staging_entry(first)
    assert filestore.operation_of_staging_entry(first) == operation


def test_staging_entries_name_their_operation(tmp_path: Path) -> None:
    operation = uuid4()
    with_part = filestore.staging_path(tmp_path, operation, part="chunk-3")

    assert filestore.operation_of_staging_entry(with_part) == operation
    # Something we did not write is recognisable as such.
    assert filestore.operation_of_staging_entry(tmp_path / "stray.partial") is None


# ------------------------------------------------------------------ append and resume


def test_appending_reports_the_durable_size(tmp_path: Path) -> None:
    """A resumable upload acknowledges offsets, so the size must be the durable one."""
    staging = tmp_path / "upload.partial"

    first = filestore.append_to_staging(staging, b"12345")
    second = filestore.append_to_staging(staging, b"67890")

    assert (first, second) == (5, 10)
    assert staging.read_bytes() == b"1234567890"


def test_truncating_discards_unacknowledged_bytes(tmp_path: Path) -> None:
    """A crash can leave bytes on disk whose offset never committed; resume drops them."""
    staging = tmp_path / "upload.partial"
    filestore.append_to_staging(staging, b"1234567890")

    filestore.truncate_staging(staging, 5)

    assert staging.read_bytes() == b"12345"


# ------------------------------------------------------------------ journaled moves


def test_a_journaled_move_verifies_before_it_commits(tmp_path: Path) -> None:
    source = tmp_path / "source" / "original"
    source.parent.mkdir()
    source.write_bytes(PAYLOAD)
    destination = tmp_path / "elsewhere" / "moved"

    outcome = filestore.journaled_move(source, destination, staging=tmp_path / "s" / "op.partial")

    assert outcome.copied and outcome.source_removed
    assert destination.read_bytes() == PAYLOAD
    assert not source.exists()


def test_a_journaled_move_finishes_a_half_done_predecessor(tmp_path: Path) -> None:
    """A crash after the rename leaves both copies; the retry only has to drop the source."""
    source = tmp_path / "source"
    source.write_bytes(PAYLOAD)
    destination = tmp_path / "destination"
    destination.write_bytes(PAYLOAD)

    outcome = filestore.journaled_move(source, destination, staging=tmp_path / "op.partial")

    assert not outcome.copied
    assert outcome.source_removed
    assert destination.read_bytes() == PAYLOAD
    assert not source.exists()


# ------------------------------------------------------------------ containment


def test_a_relative_path_inside_the_root_resolves(tmp_path: Path) -> None:
    assert (
        filestore.resolve_within(tmp_path, Path("a/b/c.txt")) == (tmp_path / "a/b/c.txt").resolve()
    )


@pytest.mark.parametrize("escape", ["../outside.txt", "a/../../outside.txt", "/etc/passwd"])
def test_a_path_that_escapes_the_root_is_refused(tmp_path: Path, escape: str) -> None:
    with pytest.raises(ContainmentError):
        filestore.resolve_within(tmp_path, Path(escape))


def test_a_symlink_out_of_the_root_is_refused(tmp_path: Path) -> None:
    """The File Browser CVE in one test: the path looks contained, the target is not."""
    outside = tmp_path.parent / f"outside-{uuid4().hex}"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(outside)

    with pytest.raises(ContainmentError):
        filestore.resolve_within(root, Path("escape/secret.txt"))


def test_a_dangling_symlink_out_of_the_root_is_also_refused(tmp_path: Path) -> None:
    """The incomplete first fix for that CVE: a dangling link has no target to stat."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path.parent / f"absent-{uuid4().hex}")

    with pytest.raises(ContainmentError):
        filestore.resolve_within(root, Path("escape"))


def test_a_symlink_inside_the_root_is_allowed(tmp_path: Path) -> None:
    """Containment is the rule, not "no symlinks anywhere"."""
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "link").symlink_to(root / "real")

    assert filestore.resolve_within(root, Path("link/file.txt")).is_relative_to(root.resolve())


# ------------------------------------------------------------------ digests


def test_the_file_digest_matches_the_bytes_digest(tmp_path: Path) -> None:
    path = tmp_path / "content"
    path.write_bytes(PAYLOAD)

    assert filestore.digest_of_file(path) == filestore.digest_of(PAYLOAD)


def test_digests_are_sha256(tmp_path: Path) -> None:
    """Pinned deliberately: the mobile clients compute the same digest for `hash-check`."""
    import hashlib

    assert filestore.digest_of(b"abc") == hashlib.sha256(b"abc").hexdigest()


# ------------------------------------------------------------------ the blob store


def test_storing_the_same_bytes_twice_is_a_no_op(tmp_path: Path) -> None:
    """Content addressing is what makes a retried write harmless."""
    store = BlobStore(tmp_path)
    operation = uuid4()

    first = store.put_bytes(PAYLOAD, operation_id=operation)
    second = store.put_bytes(PAYLOAD, operation_id=operation)

    assert first == second
    assert list(store.iter_digests()) == [first]


def test_a_blob_lives_at_a_sharded_path(tmp_path: Path) -> None:
    """A flat directory of a million files is slow to open and useless to read."""
    store = BlobStore(tmp_path)
    digest = store.put_bytes(PAYLOAD, operation_id=uuid4())

    path = store.path_for(digest)

    assert path.relative_to(tmp_path).parts == (digest[:2], digest[2:4], digest)
    assert store.contains(digest)


def test_a_file_moved_into_the_store_keeps_its_content(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "versions")
    source = tmp_path / "original"
    source.write_bytes(PAYLOAD)

    digest = store.put_file(source, operation_id=uuid4())

    assert store.open(digest).read_bytes() == PAYLOAD
    # The source is gone: this is a move, for content that is meant to stop existing where it
    # is — trash safeguarding, not a version snapshot.
    assert not source.exists()


def test_a_snapshot_leaves_the_original_exactly_where_it_was(tmp_path: Path) -> None:
    """The version snapshot copies, and that ordering is the whole point (F-007/FR-9).

    A move would empty the file's path until the new content is renamed in, and a scan
    interleaving with an upload would read that absent name as a deletion — it would trash the
    file mid-overwrite. So the original stays readable at every instant, and the extra read is
    the price.
    """
    store = BlobStore(tmp_path / "versions")
    source = tmp_path / "draft.txt"
    source.write_bytes(PAYLOAD)

    digest = store.put_copy_of(source, operation_id=uuid4())

    assert source.read_bytes() == PAYLOAD, "the snapshot moved the file it was meant to copy"
    assert store.open(digest).read_bytes() == PAYLOAD
    assert digest == filestore.digest_of(PAYLOAD), "the digest describes the bytes it copied"
    # Nothing left behind in staging: the commit was a rename, not a second copy.
    assert not any(store.staging_root.iterdir())


def test_snapshotting_content_the_store_already_has_is_a_no_op(tmp_path: Path) -> None:
    """Two files with the same content share one blob, and the second snapshot writes nothing
    new — content addressing doing the refcounting the database then reasons about."""
    store = BlobStore(tmp_path / "versions")
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_bytes(PAYLOAD)
    second.write_bytes(PAYLOAD)

    assert store.put_copy_of(first, operation_id=uuid4()) == store.put_copy_of(
        second, operation_id=uuid4()
    )

    assert first.exists() and second.exists()
    assert len(list(store.iter_digests())) == 1
    assert not any(store.staging_root.iterdir())


def test_storing_a_file_whose_content_is_already_there_drops_the_source(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "versions")
    store.put_bytes(PAYLOAD, operation_id=uuid4())
    duplicate = tmp_path / "duplicate"
    duplicate.write_bytes(PAYLOAD)

    store.put_file(duplicate, operation_id=uuid4())

    assert not duplicate.exists()
    assert len(list(store.iter_digests())) == 1


def test_verify_detects_bit_rot(tmp_path: Path) -> None:
    """A blob's name is its digest, which is the only reason rot is detectable at all."""
    store = BlobStore(tmp_path)
    digest = store.put_bytes(PAYLOAD, operation_id=uuid4())
    assert store.verify(digest)

    path = store.path_for(digest)
    path.write_bytes(b"corrupted on disk")

    assert not store.verify(digest)


def test_a_missing_blob_is_reported_rather_than_guessed(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.open("0" * 64)


def test_a_digest_that_is_not_a_digest_is_refused(tmp_path: Path) -> None:
    """The digest becomes a path, so it is validated before it is joined onto one."""
    store = BlobStore(tmp_path)

    for candidate in ("../../etc/passwd", "short", "z" * 64):
        with pytest.raises(ValueError, match="digest"):
            store.path_for(candidate)


def test_staging_files_are_not_mistaken_for_blobs(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    store.staging_root.mkdir(parents=True)
    (store.staging_root / f"{uuid4()}.partial").write_bytes(b"in flight")
    digest = store.put_bytes(PAYLOAD, operation_id=uuid4())

    assert list(store.iter_digests()) == [digest]


def test_a_removed_blob_is_gone_and_removing_it_again_is_harmless(tmp_path: Path) -> None:
    """`unlink` being idempotent is what lets deferred deletions retry without bookkeeping."""
    store = BlobStore(tmp_path)
    digest = store.put_bytes(PAYLOAD, operation_id=uuid4())

    assert store.remove(digest)
    assert not store.remove(digest)
    assert not store.contains(digest)


def test_the_directory_fsync_helper_works_on_a_real_directory(tmp_path: Path) -> None:
    """Not decoration: without it a renamed file can vanish after a crash."""
    filestore.fsync_directory(tmp_path)

    assert os.path.isdir(tmp_path)
