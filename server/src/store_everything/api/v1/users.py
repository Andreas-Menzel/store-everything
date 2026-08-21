"""Account administration.

Accounts are created by admins — there is no self-registration
(07-identity-permissions-sharing.md § users). Note what this router deliberately does
*not* offer: reading or reaching into another account's data. Instance administration is
not data access, and that separation is the spec's most repeated rule.

There is no delete: a user owns workspaces, files and history, so removing one is a data
lifecycle question that belongs with deletion and trash (phase 4). `is_active = false`
stops a person logging in and is reversible, which is what an operator actually needs
today.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field, model_validator

from store_everything import identity, passwords
from store_everything.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    decode_cursor,
    encode_cursor,
)
from store_everything.api.v1.auth import UserSummary
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.problems import FieldProblem, ProblemException
from store_everything.schemas import BaseSchema, EmailAddress
from store_everything.security import AdminCredential

router = APIRouter(prefix="/users", tags=["users"])


class UserCreateRequest(BaseSchema):
    email: EmailAddress
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=passwords.MIN_LENGTH, max_length=passwords.MAX_LENGTH)
    role: Literal["admin", "member"] = "member"


class UserUpdateRequest(BaseSchema):
    """Every field is optional; omitting one leaves it alone."""

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Literal["admin", "member"] | None = None
    is_active: bool | None = None
    password: str | None = Field(
        default=None, min_length=passwords.MIN_LENGTH, max_length=passwords.MAX_LENGTH
    )

    @model_validator(mode="after")
    def _at_least_one_change(self) -> UserUpdateRequest:
        if all(value is None for value in self.__dict__.values()):
            raise ValueError("provide at least one field to change")
        return self


@router.get("", summary="List accounts", response_model=Page[UserSummary])
async def list_users(
    _admin: AdminCredential,
    connection: DatabaseConnection,
    limit: Annotated[int, Query(gt=0, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[UserSummary]:
    after = decode_cursor(cursor) if cursor else None
    # One row beyond the page: the cheapest honest way to know whether a next page exists.
    found = await identity.list_users(connection, limit=limit + 1, after=after)

    page = found[:limit]
    next_cursor = (
        encode_cursor(page[-1].created_at, page[-1].id) if len(found) > limit and page else None
    )
    return Page(data=[UserSummary.of(user) for user in page], next_cursor=next_cursor)


@router.post(
    "",
    summary="Create an account",
    status_code=201,
    response_model=UserSummary,
    responses={409: {"description": "That email address is already registered"}},
)
async def create_user(
    payload: UserCreateRequest, admin: AdminCredential, connection: DatabaseConnection
) -> UserSummary:
    existing = await identity.find_user_by_email(connection, payload.email)
    if existing is not None:
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="An account with that email address already exists.",
            errors=[FieldProblem(detail="already registered", pointer="/body/email")],
        )

    user = await identity.create_user(
        connection,
        email=payload.email,
        display_name=payload.display_name,
        password=payload.password,
        role=payload.role,
        actor=Actor.user(admin.user.id),
    )
    return UserSummary.of(user)


@router.get(
    "/{user_id}",
    summary="Read one account",
    response_model=UserSummary,
    responses={404: {"description": "No such account"}},
)
async def read_user(
    user_id: UUID, _admin: AdminCredential, connection: DatabaseConnection
) -> UserSummary:
    user = await identity.get_user(connection, user_id)
    if user is None:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return UserSummary.of(user)


@router.patch(
    "/{user_id}",
    summary="Change an account",
    response_model=UserSummary,
    responses={
        404: {"description": "No such account"},
        409: {"description": "The change would leave the instance without an admin"},
    },
)
async def update_user(
    user_id: UUID,
    payload: UserUpdateRequest,
    admin: AdminCredential,
    connection: DatabaseConnection,
) -> UserSummary:
    target = await identity.get_user(connection, user_id)
    if target is None:
        raise ProblemException(status=404, slug="not-found", title="Not found")

    # Locking yourself out is a mistake no confirmation dialog can undo, because the
    # endpoint that would fix it is the one you just lost access to.
    demoting = target.is_admin and (payload.role == "member" or payload.is_active is False)
    if demoting and await _is_last_active_admin(connection, target.id):
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="This is the only active administrator; promote another one first.",
        )

    updated = await identity.update_user(
        connection,
        user_id=user_id,
        actor=Actor.user(admin.user.id),
        display_name=payload.display_name,
        role=payload.role,
        is_active=payload.is_active,
        password=payload.password,
    )
    if updated is None:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return UserSummary.of(updated)


async def _is_last_active_admin(connection: DatabaseConnection, user_id: UUID) -> bool:
    admins = await identity.active_admin_ids(connection)
    return admins == {user_id}
