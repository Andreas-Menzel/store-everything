"""The unified event log (ADR-0007): one append-only record of every state change.

The mechanism is a **transactional outbox**: an event is inserted on the *same connection,
inside the same transaction* as the change it describes. There is no separate audit call to
forget and no window where the two disagree — a rolled-back change takes its event with it
(F-011/FR-4), and a committed one always leaves exactly one.

Three consumers, three fidelities (ADR-0007): the audit API (full fidelity, phase 4), the
`/events` cursor feed, and the WebSocket fan-out (coalesced — phase 5). Coalescing lives
only in that last layer; nothing here ever collapses two actions into one.

Writing an event is therefore not optional bookkeeping. Every mutation from the very first
one is logged, which is why this module exists before any feature does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything.log import request_id_var
from store_everything.tables import event

type ActorType = Literal["user", "extractor", "system"]

# ------------------------------------------------------------------ action vocabulary
# Actions are `resource.past_tense`. They are part of the audit contract and the future
# `/events` feed, so they are named here once rather than spelled out at call sites.

USER_CREATED = "user.created"
USER_UPDATED = "user.updated"
USER_PASSWORD_CHANGED = "user.password_changed"  # noqa: S105 - an action name, not a secret

LOGIN_SUCCEEDED = "auth.login_succeeded"
LOGIN_FAILED = "auth.login_failed"
LOGGED_OUT = "auth.logged_out"
RATE_LIMITED = "auth.rate_limited"

SESSION_REVOKED = "session.revoked"

OPERATION_DEAD_LETTERED = "operation.dead_lettered"

TOKEN_CREATED = "token.created"  # noqa: S105 - an action name, not a secret
TOKEN_REVOKED = "token.revoked"  # noqa: S105 - an action name, not a secret

WORKSPACE_CREATED = "workspace.created"
#: The root exists, the control directory is planted and the root folder is registered —
#: the moment a workspace becomes usable, and a separate fact from being requested.
WORKSPACE_PROVISIONED = "workspace.provisioned"

FOLDER_CREATED = "folder.created"

FILE_CREATED = "file.created"

RESOURCE_USER = "user"
RESOURCE_SESSION = "session"
RESOURCE_OPERATION = "operation"
RESOURCE_TOKEN = "token"  # noqa: S105 - a resource name, not a secret
RESOURCE_WORKSPACE = "workspace"
RESOURCE_FOLDER = "folder"
RESOURCE_FILE = "file"

#: Detail keys that would put a credential into the permanent record. The log is the one
#: table nothing ever deletes, so a secret written here is a secret kept forever.
_FORBIDDEN_DETAIL_SUBSTRINGS = ("password", "token", "secret", "credential")


@dataclass(frozen=True, slots=True)
class Actor:
    """Who acted. `user_id` is required for a `user` actor and absent otherwise."""

    type: ActorType
    user_id: UUID | None = None

    @classmethod
    def user(cls, user_id: UUID) -> Actor:
        return cls("user", user_id)

    @classmethod
    def system(cls) -> Actor:
        """Startup work, schedules, janitor — anything with no human behind it."""
        return cls("system")


class UnsafeEventDetailsError(ValueError):
    """A detail key looks like a credential. Raised loudly: a leak here is permanent."""


def _validate(details: dict[str, Any]) -> None:
    for key in details:
        lowered = key.lower()
        for forbidden in _FORBIDDEN_DETAIL_SUBSTRINGS:
            if forbidden in lowered:
                raise UnsafeEventDetailsError(
                    f"event detail key {key!r} may carry a credential; "
                    "record an identifier or a name instead"
                )


async def record(
    connection: AsyncConnection,
    *,
    action: str,
    resource_type: str,
    actor: Actor,
    resource_id: UUID | None = None,
    details: dict[str, Any] | None = None,
    client_ip: str | None = None,
) -> None:
    """Append one event on `connection`, to be committed with the change it describes.

    Deliberately takes the caller's connection rather than opening its own: a separate
    connection would be a separate transaction, which is exactly the outbox bug this
    design exists to prevent.
    """
    payload = dict(details or {})
    _validate(payload)

    await connection.execute(
        insert(event).values(
            actor_type=actor.type,
            actor_user_id=actor.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=payload,
            # The bridge between an event and the log line of the request that caused it
            # (10-deployment-and-operations.md § logging).
            request_id=request_id_var.get(),
            client_ip=client_ip,
        )
    )
