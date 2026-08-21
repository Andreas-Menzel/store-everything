"""The filesystem probe: does this directory support what the write protocol assumes?

Every guarantee in 12-reliability.md stands on three properties of the filesystem holding a
workspace: atomic same-directory rename, honest `fsync` on files *and* directories, and
listings that reflect what was just written. On a local POSIX filesystem all three hold. On
SMB and NFS mounts they hold or fail depending on mount options, server implementation and
occasionally luck — which is why ADR-0019 refuses to assume them and runs this instead.

**What a probe can and cannot prove.** It cannot prove atomicity: that would need a power cut
mid-rename, observed from outside. What it *can* do is exercise the operations the protocol
performs and catch the failures that actually occur in the field — mounts that reject a
rename onto an existing file, filesystems that raise `EINVAL` when a directory is fsync'd,
staging areas that turn out to be on a different device so the commit is a copy rather than a
rename. Those are the realistic causes of silent data loss here, and they are all detectable.

It also reports **facts** rather than verdicts where behaviour merely differs: case folding
and Unicode normalization change what "the same name" means. Neither is a failure — the name
policy is case-insensitive and NFC-normalizing anyway (ADR-0019) — but an operator debugging a
scan conflict deserves to know which one their filesystem does.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from store_everything import filestore

#: Where the probe does its work: a subdirectory it creates and removes, so a failed probe
#: leaves nothing an operator has to clean up by hand.
PROBE_DIRECTORY = ".se-fscheck"


@dataclass(frozen=True, slots=True)
class Property:
    """One required behaviour, and whether this filesystem has it."""

    name: str
    satisfied: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Verdict:
    """The probe's answer for one candidate root."""

    root: Path
    properties: tuple[Property, ...] = ()
    facts: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.error is None and all(item.satisfied for item in self.properties)

    @property
    def failures(self) -> tuple[Property, ...]:
        return tuple(item for item in self.properties if not item.satisfied)

    def as_record(self) -> dict[str, Any]:
        """The verdict as JSON, for the column that records what admitted a workspace root.

        Stored whole, including the facts: when a scan conflict turns up months later, "does
        this filesystem fold case?" is the first question, and the answer belongs next to the
        workspace rather than in a log nobody kept.
        """
        return {
            "root": str(self.root),
            "usable": self.usable,
            "properties": {
                item.name: {"satisfied": item.satisfied, "detail": item.detail}
                for item in self.properties
            },
            "facts": dict(self.facts),
            "error": self.error,
        }

    def explain(self) -> str:
        if self.error is not None:
            return f"{self.root}: {self.error}"
        if self.usable:
            return f"{self.root}: usable"
        named = ", ".join(
            f"{item.name} ({item.detail})" if item.detail else item.name for item in self.failures
        )
        return f"{self.root}: unusable — {named}"


def probe(root: Path) -> Verdict:
    """Exercise the write protocol's requirements against `root`.

    Read-only in effect: everything happens inside a probe directory that is removed again,
    including when a check fails part-way.
    """
    if not root.is_dir():
        return Verdict(root=root, error="not a directory")

    workspace = root / PROBE_DIRECTORY
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as denied:
        return Verdict(root=root, error=f"cannot create a probe directory: {denied.strerror}")

    try:
        properties, facts = _run(workspace)
        return Verdict(root=root, properties=tuple(properties), facts=facts)
    except OSError as failure:
        return Verdict(root=root, error=f"probe failed: {failure}")
    finally:
        _cleanup(workspace)


def _run(workspace: Path) -> tuple[list[Property], dict[str, str]]:
    properties: list[Property] = []
    facts: dict[str, str] = {}

    payload = b"store-everything filesystem probe"
    first = workspace / f"probe-{uuid4().hex}"
    second = workspace / f"probe-{uuid4().hex}"

    # 1. Write and fsync a file. A filesystem that cannot do this durably cannot hold data.
    try:
        with first.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        properties.append(Property("file-fsync", True))
    except OSError as failure:
        properties.append(Property("file-fsync", False, failure.strerror or str(failure)))
        return properties, facts

    # 2. fsync the directory. Network mounts commonly raise EINVAL here, and without it a
    #    renamed file can vanish after a crash even though the rename returned.
    try:
        filestore.fsync_directory(workspace)
        properties.append(Property("directory-fsync", True))
    except OSError as failure:
        properties.append(Property("directory-fsync", False, failure.strerror or str(failure)))

    # 3. Rename onto an existing file. This is the commit step of every write, and some SMB
    #    servers refuse it outright.
    second.write_bytes(b"to be replaced")
    try:
        os.replace(first, second)
        replaced = second.read_bytes() == payload
        properties.append(
            Property("rename-onto-existing", replaced, "" if replaced else "content did not change")
        )
    except OSError as failure:
        properties.append(Property("rename-onto-existing", False, failure.strerror or str(failure)))

    # 4. A listing must reflect what just happened, or a scan cannot be trusted.
    listed = {path.name for path in workspace.iterdir()}
    consistent = second.name in listed and first.name not in listed
    properties.append(
        Property("consistent-listing", consistent, "" if consistent else "listing is stale")
    )

    # 5. Staging must share a device with the destination, or the commit degrades from an
    #    atomic rename to a copy — the exact difference this protocol exists to avoid.
    staging = workspace / "staging"
    staging.mkdir(exist_ok=True)
    same_device = os.stat(staging).st_dev == os.stat(workspace).st_dev
    properties.append(
        Property("staging-same-device", same_device, "" if same_device else "different st_dev")
    )

    facts.update(_facts(workspace))
    return properties, facts


def _facts(workspace: Path) -> dict[str, str]:
    """Behaviour that differs without being wrong, but changes what "same name" means."""
    facts: dict[str, str] = {}

    mixed = workspace / f"Probe-Case-{uuid4().hex[:8]}"
    mixed.write_bytes(b"case")
    lowered = workspace / mixed.name.lower()
    facts["case_sensitivity"] = "folds case" if lowered.exists() else "case-sensitive"
    mixed.unlink()

    # macOS historically stored NFD; a filesystem that normalizes turns one written name into
    # a different one on read, which is why the name policy compares a normalized key.
    composed = workspace / ("caf" + "é" + uuid4().hex[:8])
    composed.write_bytes(b"nfc")
    decomposed = workspace / unicodedata.normalize("NFD", composed.name)
    facts["unicode"] = (
        "normalizes (NFC and NFD are one name)" if decomposed.exists() else "byte-preserving"
    )
    composed.unlink()

    return facts


def _cleanup(workspace: Path) -> None:
    for path in sorted(workspace.rglob("*"), reverse=True):
        try:
            path.unlink() if path.is_file() else path.rmdir()
        except OSError:
            # A probe that cannot tidy up is not a reason to refuse a filesystem; the leftover
            # is one empty directory with an obvious name.
            return
    try:
        workspace.rmdir()
    except OSError:
        return
