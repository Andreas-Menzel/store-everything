"""Base model for every request and response body.

`extra="forbid"` implements 08-api-principles.md's rule that unknown fields are rejected
rather than silently ignored — a typo in a client payload is an error, not a no-op.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
