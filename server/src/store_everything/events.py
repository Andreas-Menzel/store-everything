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
#: Two events rather than one for the same operation (F-015/FR-11): a reader looking for "who
#: reorganised my library" wants moves, and a reader chasing a broken link wants renames.
FOLDER_RENAMED = "folder.renamed"
FOLDER_MOVED = "folder.moved"
#: A directory vanished and its content turned up in several places, or two directories' content
#: turned up in one — so the folder that went away could not be *the* folder that appeared, and a
#: new identity was created instead (F-015/FR-7). The record a review surface would read (Q24).
FOLDER_IDENTITY_AMBIGUOUS = "folder.identity_ambiguous"
#: A folder this run had just registered turned out to be one the app already had: its parent's
#: identity transferred, and inside a directory known to be the same directory a child of the
#: same name is the same child (F-015/FR-7). The row the traversal created is discarded and the
#: older identity keeps its grants and tags. Recorded separately from a transfer because the
#: evidence is different — position and name rather than content — and because it settles cases
#: the content rule alone refuses, including an empty directory, which has no content at all.
FOLDER_IDENTITY_MERGED = "folder.identity_merged"

FILE_CREATED = "file.created"
#: A new current version, from an upload onto an existing path or from content that changed on
#: the storage. Carries whether the version it superseded kept its bytes (F-007/FR-9), because
#: that is the difference between history and a gap in it.
FILE_VERSION_CREATED = "file.version_created"
#: The same content at a different path. Written by re-scan's move heuristic now (02 § file)
#: and by an in-app move later (F-015/FR-4), which is why it names both paths rather than a
#: direction.
FILE_MOVED = "file.moved"
FILE_TRASHED = "file.trashed"
#: Out of the trash and back at its path. Phase 1 writes this only for content that reappeared
#: on the storage (F-014/FR-10); restoring on request arrives with the trash surface.
FILE_RESTORED = "file.restored"

#: One per completed scan, carrying its tallies — so "why did this file appear?" and "did the
#: hourly pass run?" are both answerable from the log alone.
WORKSPACE_SCANNED = "workspace.scanned"

#: An admin allowed an extractor id to exist and minted its first credential (ADR-0020).
EXTRACTOR_PROVISIONED = "extractor.provisioned"
#: A manifest arrived and **changed something**. Deliberately not written when a restarting
#: container re-declares what it already declared: containers restart, and the one table
#: nothing deletes must not fill up with "still the same". Carries the previous version and
#: model version, which is the eligibility data reprocessing needs (F-009/FR-2).
EXTRACTOR_REGISTERED = "extractor.registered"
#: Two actions rather than one `extractor.updated`, because the audit question is "who turned
#: OCR off, and when" — a reader should not have to open the details to see which way it went.
EXTRACTOR_ENABLED = "extractor.enabled"
EXTRACTOR_DISABLED = "extractor.disabled"
EXTRACTOR_TOKEN_CREATED = "extractor.token_created"  # noqa: S105 - an action name, not a secret
EXTRACTOR_TOKEN_REVOKED = "extractor.token_revoked"  # noqa: S105 - an action name, not a secret

#: Taxonomy administration (F-003/FR-10). Five actions rather than one `tag.updated`, because
#: the audit question is "who moved `receipts` under `finance`" and "who made `bill` mean
#: `invoice`" — a reader should not have to open the details to tell a rename from a re-parent.
TAG_CREATED = "tag.created"
TAG_RENAMED = "tag.renamed"
TAG_ALIAS_ADDED = "tag.alias_added"
TAG_ALIAS_REMOVED = "tag.alias_removed"
TAG_PARENT_ADDED = "tag.parent_added"
TAG_PARENT_REMOVED = "tag.parent_removed"
TAG_MERGED = "tag.merged"
#: The typo-grade erasure ADR-0006 reserves for a tag nothing carries. A tag the vocabulary
#: refuses is `rejected` instead — soft-removed, its name kept as a suppression record.
TAG_DELETED = "tag.deleted"

#: Tag edits on a file. Per edit, not per generation: an extractor's own output is recorded by
#: its run (`extraction_run`), while these are what a *person* did — the shared-state changes
#: F-003/FR-9 promises the audit trail carries, on a resource other people can also edit.
FILE_TAGGED = "file.tagged"
FILE_UNTAGGED = "file.untagged"

FOLDER_TAGGED = "folder.tagged"
FOLDER_UNTAGGED = "folder.untagged"

RESOURCE_USER = "user"
RESOURCE_SESSION = "session"
RESOURCE_OPERATION = "operation"
RESOURCE_TOKEN = "token"  # noqa: S105 - a resource name, not a secret
RESOURCE_WORKSPACE = "workspace"
RESOURCE_FOLDER = "folder"
RESOURCE_FILE = "file"
#: An extractor is keyed by its id, which is text, while `resource_id` is a UUID — so extractor
#: events carry the id in `details` and set `resource_id` only where there is a UUID to set (a
#: token's). Details are the audit trail's self-contained record anyway (F-011/FR-9).
RESOURCE_EXTRACTOR = "extractor"
#: The vocabulary itself, as opposed to a tag *on* something: a taxonomy edit is an event about
#: the tag, while tagging a file is an event about the file.
RESOURCE_TAG = "tag"

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

    @classmethod
    def extractor(cls) -> Actor:
        """An extractor container acting on its own behalf — registering, or submitting results.

        Which extractor it was lives in the event's details, because `actor_user_id` is a user
        id and an extractor is not a user (ADR-0007).
        """
        return cls("extractor")


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
