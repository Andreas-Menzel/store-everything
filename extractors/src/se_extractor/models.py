"""What the core hands an extractor, as plain Python.

Read **tolerantly**: every reader here takes the keys it knows and ignores the rest, because a
core newer than this SDK may add fields and an extractor must keep working across that
(05 § compatibility rules). The opposite — refusing an unknown field — would turn every
additive change to the contract into a fleet-wide outage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _text(document: dict[str, Any], key: str, default: str = "") -> str:
    value = document.get(key, default)
    return value if isinstance(value, str) else default


def _number(document: dict[str, Any], key: str, default: int = 0) -> int:
    value = document.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


@dataclass(frozen=True, slots=True)
class JobInput:
    """One thing to read. `url` is relative to the instance's base URL."""

    index: int
    kind: str
    url: str
    media_type: str
    size: int
    content_hash: str
    digest_algorithm: str = "sha256"

    @classmethod
    def of(cls, document: dict[str, Any]) -> JobInput:
        return cls(
            index=_number(document, "index"),
            kind=_text(document, "kind", "original"),
            url=_text(document, "url"),
            media_type=_text(document, "media_type", "application/octet-stream"),
            size=_number(document, "size"),
            content_hash=_text(document, "content_hash"),
            digest_algorithm=_text(document, "digest_algorithm", "sha256"),
        )


@dataclass(frozen=True, slots=True)
class FileVersion:
    """The immutable snapshot this job is about."""

    id: str
    content_hash: str
    size: int
    media_type: str
    media_class: str
    is_current: bool
    digest_algorithm: str = "sha256"

    @classmethod
    def of(cls, document: dict[str, Any]) -> FileVersion:
        return cls(
            id=_text(document, "id"),
            content_hash=_text(document, "content_hash"),
            size=_number(document, "size"),
            media_type=_text(document, "media_type", "application/octet-stream"),
            media_class=_text(document, "media_class", "other"),
            is_current=bool(document.get("is_current", True)),
            digest_algorithm=_text(document, "digest_algorithm", "sha256"),
        )


@dataclass(frozen=True, slots=True)
class Job:
    """One claimed job.

    `attempt` is the fencing token: it goes back with every write, and the core refuses a write
    carrying a stale one. Keep it with the job and never invent it.
    """

    id: str
    attempt: int
    idempotency_key: str
    extractor_id: str
    generation: int
    params: dict[str, Any] = field(default_factory=dict)
    lease_expires_at: str = ""
    heartbeat_interval_seconds: int = 60
    cancel_requested: bool = False
    file_version: FileVersion | None = None
    inputs: tuple[JobInput, ...] = ()

    @classmethod
    def of(cls, document: dict[str, Any]) -> Job:
        version = document.get("file_version")
        inputs = document.get("inputs")
        return cls(
            id=_text(document, "id"),
            attempt=_number(document, "attempt"),
            idempotency_key=_text(document, "idempotency_key"),
            extractor_id=_text(document, "extractor_id"),
            generation=_number(document, "generation", 1),
            params=dict(document.get("params") or {}),
            lease_expires_at=_text(document, "lease_expires_at"),
            heartbeat_interval_seconds=_number(document, "heartbeat_interval_seconds", 60),
            cancel_requested=bool(document.get("cancel_requested", False)),
            file_version=FileVersion.of(version) if isinstance(version, dict) else None,
            inputs=tuple(
                JobInput.of(entry)
                for entry in (inputs if isinstance(inputs, list) else [])
                if isinstance(entry, dict)
            ),
        )

    @property
    def original(self) -> JobInput | None:
        """The file's own bytes, which is what most extractors want."""
        return next((entry for entry in self.inputs if entry.kind == "original"), None)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    lease_expires_at: str
    cancel: bool

    @classmethod
    def of(cls, document: dict[str, Any]) -> Heartbeat:
        return cls(
            lease_expires_at=_text(document, "lease_expires_at"),
            cancel=bool(document.get("cancel", False)),
        )


@dataclass(frozen=True, slots=True)
class Registration:
    extractor_id: str
    changed: bool
    enabled: bool
    manifest: dict[str, Any]
    """What the core **understood**. Compare it with what was sent: a field missing here is a
    field this core ignored, which is how a typo in a manifest becomes visible."""

    @classmethod
    def of(cls, document: dict[str, Any]) -> Registration:
        manifest = document.get("manifest")
        return cls(
            extractor_id=_text(document, "extractor_id"),
            changed=bool(document.get("changed", False)),
            enabled=bool(document.get("enabled", True)),
            manifest=dict(manifest) if isinstance(manifest, dict) else {},
        )
