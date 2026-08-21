"""The content-addressed store: `versions/` and hashed derived assets.

Content addressing is what makes rule 3 of the write protocol true — *same bytes, same path*
— and that single property buys three things the rest of the system leans on:

- **Idempotence for free.** A retried write lands on the same path with the same content, so
  it is a no-op rather than a conflict. No operation needs to ask "did I already do this?".
- **Sharing without bookkeeping at the byte level.** Two files whose current version has the
  same content point at one blob; deletion is refcounted in the database
  ([F-007/FR-7](../../../features/F-007-versioning.md)), never inferred from the filesystem.
- **Integrity that can be checked.** A blob's name *is* its expected digest, so bit rot is
  detectable by reading it — which is what `verify` does (12 § verification).

Superseded versions live here rather than in the user's tree, because a user browsing their
files over SMB must not see stale duplicates of their own documents
([03](../../../specs/03-storage-and-portability.md)). That is also why this area is *not*
regenerable and is mandatory backup scope: the bytes exist nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from store_everything import filestore

#: Two levels of two hex characters: 65 536 leaf directories, so a million blobs average ~15
#: entries per directory. Flat layouts get slow at that size on most filesystems, and `ls`
#: becomes useless to an operator long before that.
_SHARD_DEPTH = 2
_SHARD_WIDTH = 2

#: Where a blob store keeps its own staging files. Inside the store's root on purpose: the
#: commit has to be a rename, which means the same filesystem.
STAGING_DIRECTORY = "staging"


@dataclass(frozen=True, slots=True)
class BlobStore:
    """A directory of content-addressed files, addressed by SHA-256 digest."""

    root: Path

    @property
    def staging_root(self) -> Path:
        return self.root / STAGING_DIRECTORY

    def path_for(self, digest: str) -> Path:
        """The one path a given content may occupy. Deterministic, so retries converge."""
        _validate(digest)
        parts = [
            digest[index * _SHARD_WIDTH : (index + 1) * _SHARD_WIDTH]
            for index in range(_SHARD_DEPTH)
        ]
        return self.root.joinpath(*parts, digest)

    def contains(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put_bytes(self, data: bytes, *, operation_id: UUID) -> str:
        """Store bytes and return their digest. A second call with the same bytes is a no-op."""
        digest = filestore.digest_of(data)
        destination = self.path_for(digest)
        if destination.is_file():
            return digest

        filestore.write_atomically(
            destination,
            data,
            staging=filestore.staging_path(self.staging_root, operation_id, part=digest),
        )
        return digest

    def put_file(self, source: Path, *, operation_id: UUID, digest: str | None = None) -> str:
        """Move a file into the store, verifying its content on the way.

        Used for the version snapshot: the previous content of a file being overwritten is
        moved here before the new bytes land, so there is no window in which neither exists.
        """
        resolved = digest or filestore.digest_of_file(source)
        destination = self.path_for(resolved)
        if destination.is_file():
            # The bytes are already stored — by an earlier attempt, or by another file with
            # the same content. Either way this source is now redundant.
            filestore.remove(source)
            return resolved

        filestore.journaled_move(
            source,
            destination,
            staging=filestore.staging_path(self.staging_root, operation_id, part=resolved),
        )
        return resolved

    def open(self, digest: str) -> Path:
        """The path to read a blob from, or raise if it is missing."""
        path = self.path_for(digest)
        if not path.is_file():
            raise FileNotFoundError(f"blob {digest} is not in {self.root}")
        return path

    def remove(self, digest: str) -> bool:
        """Unlink one blob. Only ever called after every referencing row is gone."""
        return filestore.remove(self.path_for(digest))

    def iter_digests(self) -> Iterator[str]:
        """Every blob currently stored, for the janitor and for `verify`."""
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or filestore.is_staging_entry(path):
                continue
            if path.parent.name == STAGING_DIRECTORY or STAGING_DIRECTORY in path.parts:
                continue
            if _looks_like_digest(path.name):
                yield path.name

    def verify(self, digest: str) -> bool:
        """Read a blob back and check it still matches its name — the bit-rot check."""
        path = self.path_for(digest)
        return path.is_file() and filestore.digest_of_file(path) == digest


def _looks_like_digest(name: str) -> bool:
    return len(name) == 64 and all(character in "0123456789abcdef" for character in name)


def _validate(digest: str) -> None:
    """A digest becomes a path, so it is validated before it is joined onto one."""
    if not _looks_like_digest(digest):
        raise ValueError(f"not a {filestore.DIGEST_ALGORITHM} digest: {digest!r}")
