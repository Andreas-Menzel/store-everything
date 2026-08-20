"""Base model for every request and response body.

`extra="forbid"` implements 08-api-principles.md's rule that unknown fields are rejected
rather than silently ignored — a typo in a client payload is an error, not a no-op.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class BaseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


EmailAddress = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]
"""An email address, validated structurally rather than exhaustively.

Deliberately not `pydantic.EmailStr`: it pulls in a dependency whose licence would need
its own review (ADR-0016), and full RFC 5322 validation buys nothing here — the address is
an account identifier, and the only proof that it is real is that someone receives mail at
it. The bounds match the `app_user.email` check constraints, so a value that passes here
cannot fail at the database.
"""
