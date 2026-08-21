"""The one pagination envelope, and keyset cursors.

08-api-principles.md § pagination: cursor-based everywhere lists can grow, one envelope,
never an unbounded list. Cursors are **keyset-anchored** — the sort key plus the id as a
tiebreak — rather than offsets, so a page stays correct while rows are inserted and
deleted around it, and page seams do not silently skip or repeat items.

The cursor is opaque to clients on purpose: it encodes a position, and clients that parse
it would freeze the sort order we are free to change.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from store_everything.problems import FieldProblem, ProblemException

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class Page[T](BaseModel):
    """`{ "data": [...], "next_cursor": "..." | null }` — the same shape everywhere."""

    data: list[T]
    next_cursor: str | None = None


class InvalidCursor(ProblemException):
    def __init__(self) -> None:
        super().__init__(
            status=422,
            slug="validation",
            title="Validation failed",
            detail="1 request field(s) are invalid.",
            errors=[FieldProblem(detail="not a valid cursor", pointer="/query/cursor")],
        )


def encode_sequence_cursor(value: int) -> str:
    """A cursor over a monotonic integer id.

    Opaque like every other cursor (08 § pagination): a client that parsed it would freeze an
    ordering we are free to change.
    """
    return base64.urlsafe_b64encode(str(value).encode()).decode().rstrip("=")


def decode_sequence_cursor(cursor: str) -> int:
    padding = "=" * (-len(cursor) % 4)
    try:
        return int(base64.urlsafe_b64decode(cursor + padding).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as invalid:
        raise InvalidCursor() from invalid


def encode_cursor(created_at: datetime, identifier: UUID) -> str:
    raw = f"{created_at.isoformat()}|{identifier}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Decode a cursor, or raise the standard validation problem."""
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        timestamp, _, identifier = raw.partition("|")
        return datetime.fromisoformat(timestamp), UUID(identifier)
    except (binascii.Error, UnicodeDecodeError, ValueError) as invalid:
        raise InvalidCursor() from invalid
