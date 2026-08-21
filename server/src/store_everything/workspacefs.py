"""The on-disk shape of a workspace root: the one control directory we plant in it.

A workspace root is the user's own tree (ADR-0003), and the app adds exactly one thing to
it: `.workspace/` (ADR-0018). Two reasons, and no others are allowed to accumulate here:

- `staging/` — write staging must share a filesystem with its destination, or committing a
  write degrades from an atomic rename to a copy (12-reliability.md § filesystem write
  protocol). For an adopted NAS tree that means staging cannot live on the app volume; it
  has to live in the tree.
- `marker` — so a directory found on a restored backup, or moved by an operator, can be
  recognised as the workspace it is. **The database stays authoritative**: nothing reads
  configuration from here, because two sources of truth is one too many.

Everything is written through `filestore`, and every step is idempotent: this runs inside a
leased operation that may be retried after a crash, so "already there" is the normal case
rather than an error.

`.workspace` is a reserved name at the workspace root (`names.RESERVED_ROOT_NAMES`), skipped
by every scan and invisible in every API response
([F-001/FR-13](../../../features/F-001-upload-and-import.md)).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from store_everything import filestore
from store_everything.names import CONTROL_DIRECTORY

MARKER_NAME = "marker"
STAGING_DIRECTORY = "staging"

#: Bumped only if the marker's layout changes incompatibly. A reader that finds a higher
#: number knows it is looking at a tree written by a newer version of the app.
MARKER_FORMAT = 1

#: Written into the marker for whoever finds the file while browsing their own NAS.
_MARKER_NOTE = (
    "Written by Store Everything so this directory stays identifiable after a move or a "
    "restore. The database is authoritative; editing this file changes nothing."
)


class MarkerError(Exception):
    """The marker exists but cannot be read as one."""


@dataclass(frozen=True, slots=True)
class Marker:
    """What the marker asserts about the tree it sits in."""

    workspace_id: UUID
    placement: str
    created_at: str


def control_directory(root: Path) -> Path:
    return root / CONTROL_DIRECTORY


def staging_directory(root: Path) -> Path:
    """Where uploads and app-mediated writes stage their bytes — same filesystem as the
    destination, which is the whole reason it is here rather than on the app volume."""
    return control_directory(root) / STAGING_DIRECTORY


def marker_path(root: Path) -> Path:
    return control_directory(root) / MARKER_NAME


def materialize(
    root: Path,
    *,
    workspace_id: UUID,
    placement: str,
    created_at: datetime,
    operation_id: UUID,
) -> None:
    """Plant the control directory in an existing root and write its marker. Idempotent.

    The root itself is *not* created here: for an adopted placement its absence means the
    mount is gone, and creating an empty directory in its place would look like an empty
    workspace instead of a missing one.
    """
    if not root.is_dir():
        raise NotADirectoryError(f"workspace root is not a directory: {root}")

    staging = staging_directory(root)
    # Creates `.workspace/` on the way, and fsyncs each parent so a crash cannot lose the
    # directory the marker is about to be written into.
    filestore.ensure_directory(staging)

    payload = {
        "format": MARKER_FORMAT,
        "app": "store-everything",
        "workspace": str(workspace_id),
        "placement": placement,
        "created_at": created_at.isoformat(),
        "note": _MARKER_NOTE,
    }
    filestore.write_atomically(
        marker_path(root),
        json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n",
        # Staged beside the destination and named after the operation, so a crash mid-write
        # leaves debris the janitor can attribute and collect.
        staging=filestore.staging_path(staging, operation_id, part=MARKER_NAME),
    )


def read_marker(root: Path) -> Marker | None:
    """The marker in `root`, or `None` if there is none. Raises if there is an unreadable one.

    The distinction matters for re-identification: a tree with no marker is a tree we have
    never seen, while a tree with a corrupt one is a problem to report rather than to
    silently treat as new.
    """
    path = marker_path(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as unreadable:
        raise MarkerError(f"cannot read {path}: {unreadable}") from unreadable

    try:
        document = json.loads(raw)
        return Marker(
            workspace_id=UUID(document["workspace"]),
            placement=str(document["placement"]),
            created_at=str(document["created_at"]),
        )
    except (ValueError, TypeError, KeyError) as malformed:
        raise MarkerError(f"{path} is not a valid workspace marker") from malformed
