"""The one shared write layer. Every file this app writes goes through it.

PostgreSQL transactions do not cover the filesystem, and that gap is where "the row says the
file is there" and "the file is there" drift apart. 12-reliability.md § filesystem write
protocol closes it with four rules, implemented here and nowhere else — ad-hoc `open()` in
feature code is a review-blocker (ADR-0010), because a second implementation is a second set
of crash windows nobody has thought about.

1. **Stage on the destination filesystem**, at a path derived from the operation id. Same
   filesystem, so the final step is a rename rather than a copy; derived from the id, so a
   retry reuses the same staging file instead of leaking a new one.
2. **`fsync` the file, rename onto the deterministic destination, then `fsync` the parent
   directory.** The last step is the one people forget: after a crash a renamed file can
   exist with its directory entry lost, which is indistinguishable from never having been
   written — except that the row referencing it committed.
3. **Content-addressed destinations are idempotent for free**: same bytes, same path, so a
   re-run is a no-op instead of a conflict (see `blobs.py`).
4. **Cross-filesystem moves cannot be atomic**, so they run as a journaled sequence —
   copy, fsync, verify the hash, then unlink the source. A crash leaves both copies, which
   is harmless and recoverable, rather than a truncated destination.

**Ordering rule** ([02 § invariant 8](../../../specs/02-domain-model.md)): app-written bytes
outlive the rows that reference them. Bytes first, then the row; rows first, then the bytes.
An orphaned file is a janitor's problem, while a row pointing at absent bytes is a user's.

Every step that a crash could land between is a named fault point, so the crash-injection
tests kill a real process there rather than simulating the timing
(`tests/test_filestore_fault_injection.py`).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from store_everything.faults import fault_point

#: Read size for hashing and copying. Large enough to keep syscall overhead irrelevant,
#: small enough that a multi-gigabyte file does not sit in memory.
CHUNK_SIZE = 1024 * 1024

#: The digest algorithm for content addressing and integrity (02 § FileVersion).
DIGEST_ALGORITHM = "sha256"

#: Staging files carry this suffix so debris is recognisable at a glance — by the janitor,
#: and by an operator looking at a directory listing.
STAGING_SUFFIX = ".partial"


class ContainmentError(Exception):
    """A path resolved outside the root it must stay inside.

    Raised rather than returned: every caller either has a legitimate path or is being made
    to read or write somewhere it must not, and the second case must not have a fall-through.
    """


def digest_of(data: bytes) -> str:
    return hashlib.new(DIGEST_ALGORITHM, data).hexdigest()


def stage_copy(source: Path, staging: Path) -> str:
    """Copy a file into staging without disturbing it, and return its digest. **Blocking.**

    Hashed as it is copied rather than in a second pass: the digest then describes the bytes
    that were actually written, and a multi-gigabyte original is read once. The staging file is
    durable when this returns, so the caller's `commit_staged` is the only step left.

    Used by the version snapshot (`blobs.put_copy_of`), which is why this is a copy and not
    the cheaper `journaled_move`: the original has to stay where it is until the new content
    replaces it.
    """
    ensure_directory(staging.parent)
    digest = hashlib.new(DIGEST_ALGORITHM)

    fault_point("filestore.before-staging")
    with source.open("rb") as reading, staging.open("wb") as writing:
        while chunk := reading.read(CHUNK_SIZE):
            digest.update(chunk)
            writing.write(chunk)
        writing.flush()
        fault_point("filestore.after-staging-write")
        os.fsync(writing.fileno())
    return digest.hexdigest()


def digest_of_file(path: Path) -> str:
    """Stream the file through the hash — a version may be larger than memory."""
    digest = hashlib.new(DIGEST_ALGORITHM)
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    """Make a directory's own entries durable.

    Renaming a file into place is not durable until the directory entry is, so this is not
    an optional flourish: without it a crash can lose a file that `rename` already reported
    as complete.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(path: Path) -> None:
    """Create a directory and make its existence durable. Idempotent."""
    if path.is_dir():
        return
    path.mkdir(parents=True, exist_ok=True)
    # The parent's entry for this directory has to survive too, or a crash can lose the
    # directory that the file we are about to write lives in.
    fsync_directory(path.parent)


def resolve_within(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and refuse it unless it stays inside `root`.

    This is the check ADR-0019 requires on **every** open, deliberately redundant with the
    scanner's refusal to follow symlinks. Lexical containment is not containment: File
    Browser's CVE-2026-54094 was exactly a scope check that compared path strings while the
    open followed a link out of the scope, and its first fix still trusted a dangling link's
    parent directory. So this resolves symlinks (`strict=False`, because the target of a
    write does not exist yet) and compares the *resolved* paths.
    """
    resolved_root = root.resolve()
    resolved = (
        (resolved_root / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ContainmentError(f"path escapes its root: {candidate}")
    return resolved


def staging_path(staging_root: Path, operation_id: UUID, *, part: str | None = None) -> Path:
    """Where an operation stages its bytes. Deterministic, so a retry converges.

    The operation id in the name is what lets the janitor decide: it can look the operation
    up, and collect the file only once that operation is terminal (or unknown) and past the
    grace window.
    """
    suffix = f".{part}" if part else ""
    return staging_root / f"{operation_id}{suffix}{STAGING_SUFFIX}"


def is_staging_entry(path: Path) -> bool:
    return path.name.endswith(STAGING_SUFFIX)


def operation_of_staging_entry(path: Path) -> UUID | None:
    """The operation a staging file belongs to, or `None` if the name is not ours."""
    name = path.name.removesuffix(STAGING_SUFFIX).split(".", maxsplit=1)[0]
    try:
        return UUID(name)
    except ValueError:
        return None


@contextmanager
def staged_write(destination: Path, *, staging: Path) -> Generator[BinaryIO]:
    """Write bytes destined for `destination` into `staging`, then commit them atomically.

    The caller writes to the handle; leaving the block runs `fsync` → `rename` → directory
    `fsync`. Raising inside the block leaves the staging file in place — deliberately: the
    retry reuses it, and the janitor collects it if there is no retry.
    """
    ensure_directory(staging.parent)
    ensure_directory(destination.parent)

    fault_point("filestore.before-staging")
    with staging.open("wb") as handle:
        yield handle
        handle.flush()
        fault_point("filestore.after-staging-write")
        os.fsync(handle.fileno())

    commit_staged(staging, destination)


def commit_staged(staging: Path, destination: Path) -> None:
    """Move an already-durable staging file onto its destination, atomically.

    Split out from `staged_write` because a resumable upload stages across many requests and
    commits in a later one (ADR-0017): the bytes are durable long before anyone knows the
    final path is wanted.
    """
    fault_point("filestore.after-staging-fsync")
    # `os.replace` is atomic within a filesystem: readers see either the old file or the new
    # one, never a partial write. Across filesystems it raises, which is why the caller must
    # stage beside the destination — and why `journaled_move` exists for when it cannot.
    os.replace(staging, destination)
    fault_point("filestore.after-rename")
    fsync_directory(destination.parent)
    fault_point("filestore.after-directory-fsync")


def write_atomically(destination: Path, data: bytes, *, staging: Path) -> None:
    """The whole protocol for a small, in-memory payload."""
    with staged_write(destination, staging=staging) as handle:
        handle.write(data)


def open_staging_append(staging: Path) -> BinaryIO:
    """Open a staging file for appending, creating its directory. Caller closes it.

    Exists so one request can stream many chunks into one handle and pay for **one** `fsync`
    at the end: a resumable upload's append can carry tens of megabytes, and fsyncing per
    read would turn one durability barrier into hundreds. The handle is written from a worker
    thread (`filestore` is synchronous by design) one chunk at a time, never concurrently.
    """
    ensure_directory(staging.parent)
    return staging.open("ab")


def commit_appended(handle: BinaryIO) -> int:
    """Make everything written to `handle` durable, and report the file's new size.

    Durability before acknowledgement is the point: a resumable upload tells the client
    "I have your first N bytes" (ADR-0017), and that promise must survive a power cut, not
    just a process restart. So this runs *before* the offset is committed, never after.
    """
    handle.flush()
    fault_point("filestore.after-append-write")
    os.fsync(handle.fileno())
    size = handle.tell()
    fault_point("filestore.after-append-fsync")
    return size


def append_to_staging(staging: Path, chunk: bytes) -> int:
    """Append one chunk and make it durable. Returns the new size."""
    handle = open_staging_append(staging)
    try:
        handle.write(chunk)
        return commit_appended(handle)
    finally:
        handle.close()


def truncate_staging(staging: Path, offset: int) -> None:
    """Cut a staging file back to a known-good offset before resuming.

    A crash can leave bytes on disk that were never acknowledged — written, but the offset
    they imply never committed. Resuming from the *recorded* offset and discarding the rest
    is what keeps the client's view and ours identical.
    """
    with staging.open("r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    """What a journaled move did, for the operation record to reason about."""

    copied: bool
    source_removed: bool


def move_entry(source: Path, destination: Path) -> None:
    """Rename a file or a whole directory to another place on the same filesystem. **Blocking.**

    What a folder rename or move does on disk
    ([F-015/FR-4](../../../features/F-015-folders.md)): one `rename`, atomic however large the
    subtree is, because the kernel moves a directory entry rather than the bytes underneath it.
    Both parent directories are made durable afterwards, because two of them changed.

    Refuses an occupied destination rather than replacing it. The database's own collision check
    has already run, so reaching this means the filesystem holds something the index does not know
    about — a directory nobody registered — and overwriting a user's data to resolve that would be
    the worst possible answer (ADR-0019: report, never repair).

    Raises `OSError` with `EXDEV` when the two paths are on different filesystems, which is the
    caller's cue that this move needs bytes copied rather than an entry rewritten.
    """
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"{destination} is already there")

    fault_point("filestore.before-move")
    os.rename(source, destination)
    fault_point("filestore.after-move")
    fsync_directory(destination.parent)
    if source.parent != destination.parent:
        fsync_directory(source.parent)


def journaled_move(source: Path, destination: Path, *, staging: Path) -> MoveOutcome:
    """Move a file that may be on another filesystem, without a window of loss.

    `os.replace` fails across filesystems, and a plain copy-then-delete has a window where a
    crash leaves a truncated destination. So: copy to staging, fsync, verify the hash matches
    the source, commit atomically, and only then unlink the source. A crash anywhere leaves
    either the source alone or both copies — never a half-written destination, and never
    neither.

    Idempotent by inspection: if the destination already holds the right bytes, the move is
    finished and the source can go.
    """
    if destination.exists() and digest_of_file(destination) == digest_of_file(source):
        # A previous attempt got as far as the rename. Finish the last step.
        source.unlink(missing_ok=True)
        return MoveOutcome(copied=False, source_removed=True)

    expected = digest_of_file(source)
    ensure_directory(staging.parent)
    ensure_directory(destination.parent)

    fault_point("filestore.before-journaled-copy")
    with source.open("rb") as reader, staging.open("wb") as writer:
        shutil.copyfileobj(reader, writer, CHUNK_SIZE)
        writer.flush()
        os.fsync(writer.fileno())
    fault_point("filestore.after-journaled-copy")

    # Verify before committing: a copy that silently lost bytes must not become the only
    # remaining version of a user's file.
    if digest_of_file(staging) != expected:
        staging.unlink(missing_ok=True)
        raise OSError(f"copy of {source} does not match its source digest")
    fault_point("filestore.after-journaled-verify")

    commit_staged(staging, destination)
    fault_point("filestore.before-source-unlink")
    source.unlink(missing_ok=True)
    return MoveOutcome(copied=True, source_removed=True)


def remove(path: Path) -> bool:
    """Delete a file, tolerating its absence.

    `unlink` is naturally idempotent — ENOENT means the work is already done — which is what
    lets deferred deletions be retried without bookkeeping (12 § ordering rule).
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
