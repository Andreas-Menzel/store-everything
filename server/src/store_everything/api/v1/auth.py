"""Authentication endpoints: log in, log out, list and revoke credentials.

`POST /auth/login` is the one endpoint here that cannot require authentication — it is how
a caller *gets* a credential — so it lives on its own router and is counted in the
documented public surface (08-api-principles.md § endpoint map).

Session tokens leave the server exactly once, in a `Set-Cookie` header the browser's
JavaScript cannot read; personal access tokens leave exactly once, in the response body of
the request that created them. Neither is ever retrievable afterwards, because only their
digests are stored (07-identity-permissions-sharing.md § tokens & credentials).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from store_everything import events, identity, passwords, ratelimit
from store_everything.config import Settings
from store_everything.db import DatabaseConnection
from store_everything.events import Actor
from store_everything.identity import AccessToken, AccountDisabledError, Session, User
from store_everything.problems import FieldProblem, ProblemException
from store_everything.ratelimit import PROXIED_DETAIL
from store_everything.schemas import BaseSchema, EmailAddress
from store_everything.security import (
    AccountDisabledProblem,
    AuthenticationRequired,
    CurrentCredential,
    client_ip,
    client_ip_identifies_caller,
    enforce_request_ceiling,
    enforce_same_origin,
    settings_of,
)

MAX_TOKEN_LIFETIME_DAYS = 3650

#: A user-agent string is a label in a session list, not data we analyse. Truncated because
#: the header is attacker-controlled and unbounded.
_USER_AGENT_LIMIT = 300


def _user_agent(request: Request) -> str | None:
    header = request.headers.get("user-agent")
    return header[:_USER_AGENT_LIMIT] if header else None


def _cookie_attributes(settings: Settings) -> dict[str, Any]:
    """The session cookie's attributes, in one place.

    Setting and clearing must agree on every attribute — a browser matches on name, path
    and domain, so a `delete_cookie` that differs leaves the old cookie in place and the
    client keeps presenting a revoked session on every request.
    """
    return {
        "httponly": True,
        "secure": settings.session_cookie_secure,
        "samesite": "lax",
        "path": "/",
    }


public_router = APIRouter(prefix="/auth", tags=["auth"])
"""Unauthenticated by design — login only, and rate-limited."""

router = APIRouter(prefix="/auth", tags=["auth"])


# ------------------------------------------------------------------------- schemas


class UserSummary(BaseSchema):
    id: UUID
    email: str
    display_name: str
    role: Literal["admin", "member"]
    is_active: bool
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> UserSummary:
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
        )


class LoginRequest(BaseSchema):
    email: EmailAddress
    password: str = Field(min_length=1, max_length=passwords.MAX_LENGTH)


class SessionSummary(BaseSchema):
    id: UUID
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool

    @classmethod
    def of(cls, session: Session, *, current: bool) -> SessionSummary:
        return cls(
            id=session.id,
            user_agent=session.user_agent,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            expires_at=session.expires_at,
            current=current,
        )


class CurrentIdentity(BaseSchema):
    """Who the caller is, and how they proved it."""

    user: UserSummary
    credential_kind: Literal["session", "token"]
    credential_id: UUID
    scope: Literal["read", "full"]


class TokenSummary(BaseSchema):
    id: UUID
    name: str
    scope: Literal["read", "full"]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None

    @classmethod
    def of(cls, token: AccessToken) -> TokenSummary:
        return cls(
            id=token.id,
            name=token.name,
            scope=token.scope,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
            expires_at=token.expires_at,
        )


class TokenCreateRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=100)
    scope: Literal["read", "full"] = "read"
    """Least privilege by default: a token is read-only unless it asks not to be."""

    expires_in_days: int | None = Field(default=None, gt=0, le=MAX_TOKEN_LIFETIME_DAYS)


class TokenCreated(BaseSchema):
    token: str
    """The plaintext, shown exactly once. It is not stored and cannot be shown again."""

    access_token: TokenSummary


# -------------------------------------------------------------------------- login


@public_router.post(
    "/login",
    summary="Log in with email and password",
    response_model=CurrentIdentity,
    responses={
        401: {"description": "Unknown credentials, or a disabled account"},
        429: {"description": "Too many failed attempts"},
    },
    dependencies=[Depends(enforce_request_ceiling)],
)
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    connection: DatabaseConnection,
) -> CurrentIdentity:
    """Exchange a password for a session cookie.

    Failed attempts are recorded and counted (07 § abuse protection). Because a failure
    answers `4xx`, its event has to be committed explicitly — the request's transaction is
    about to be rolled back by the exception, and an audit record that disappears when the
    thing it describes fails is worse than none.
    """
    settings = settings_of(request)
    address = client_ip(request)
    identified = client_ip_identifies_caller(request)
    email = identity.normalize_email(payload.email)

    # Login carries no ambient authority, so a non-browser client may omit the headers; a
    # browser that sends them must be on our own origin.
    enforce_same_origin(request, required=False)

    window = timedelta(minutes=settings.login_lockout_minutes)
    if await ratelimit.login_attempts_exhausted(
        connection,
        email=email,
        # Only an address that identifies *somebody* is counted. Behind a proxy whose headers
        # this instance does not trust — the shipped default — every caller arrives as the
        # proxy, so counting failures per address counts the whole instance: ten junk attempts
        # would lock every user out of logging in, for free, indefinitely (A13). The
        # per-identity ceiling below is unaffected, and the event still records the address
        # that was actually seen.
        client_ip=address if identified else None,
        max_attempts=settings.login_max_attempts,
        window=window,
    ):
        await ratelimit.note_refusal(
            connection, scope="login", key=email, window=window, client_ip=address
        )
        await connection.commit()
        raise ratelimit.TooManyRequests(
            detail=(
                f"More than {settings.login_max_attempts} failed attempts in "
                f"{settings.login_lockout_minutes} minutes."
            ),
            retry_after_seconds=settings.login_lockout_minutes * 60,
        )

    try:
        user = await identity.verify_credentials(connection, email=email, password=payload.password)
    except AccountDisabledError as disabled:
        raise AccountDisabledProblem() from disabled

    if user is None:
        await events.record(
            connection,
            action=events.LOGIN_FAILED,
            resource_type=events.RESOURCE_SESSION,
            actor=Actor.system(),
            # No password, and no statement about whether the address exists. `proxied` says
            # the recorded address is a proxy's, so a later per-address count knows to skip
            # it rather than treating one address as the whole instance (A13).
            details={"email": email} if identified else {"email": email, PROXIED_DETAIL: True},
            client_ip=address,
        )
        await connection.commit()
        raise AuthenticationRequired("Email or password is incorrect.")

    token, session = await identity.create_session(
        connection,
        user=user,
        idle_expiry=timedelta(days=settings.session_idle_expiry_days),
        user_agent=_user_agent(request),
        client_ip=address,
    )

    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_idle_expiry_days * 24 * 3600,
        **_cookie_attributes(settings),
    )

    return CurrentIdentity(
        user=UserSummary.of(user),
        credential_kind="session",
        credential_id=session.id,
        scope="full",
    )


# ------------------------------------------------------------ authenticated endpoints


@router.get("/me", summary="The authenticated caller", response_model=CurrentIdentity)
async def current_identity(credential: CurrentCredential) -> CurrentIdentity:
    return CurrentIdentity(
        user=UserSummary.of(credential.user),
        credential_kind=credential.kind,
        credential_id=credential.id,
        scope=credential.scope,
    )


@router.post(
    "/logout",
    summary="Log out of the current session",
    status_code=204,
    responses={204: {"description": "Session revoked"}},
)
async def logout(
    request: Request,
    response: Response,
    credential: CurrentCredential,
    connection: DatabaseConnection,
) -> Response:
    """Revoke the current session. A token-authenticated caller has nothing to log out of."""
    settings = settings_of(request)

    if credential.kind != "session":
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="This request is authenticated with an access token, not a session.",
        )

    await identity.revoke_session(
        connection,
        session_id=credential.id,
        user_id=credential.user.id,
        actor=Actor.user(credential.user.id),
        action=events.LOGGED_OUT,
    )
    response.delete_cookie(settings.session_cookie_name, **_cookie_attributes(settings))
    response.status_code = 204
    return response


@router.get("/sessions", summary="List your sessions", response_model=list[SessionSummary])
async def list_sessions(
    credential: CurrentCredential, connection: DatabaseConnection
) -> list[SessionSummary]:
    sessions = await identity.list_sessions(connection, user_id=credential.user.id)
    current_id = credential.id if credential.kind == "session" else None
    return [SessionSummary.of(session, current=session.id == current_id) for session in sessions]


@router.delete(
    "/sessions/{session_id}",
    summary="Revoke one of your sessions",
    status_code=204,
    responses={404: {"description": "No such session for this user"}},
)
async def revoke_session(
    session_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> Response:
    revoked = await identity.revoke_session(
        connection,
        session_id=session_id,
        user_id=credential.user.id,
        actor=Actor.user(credential.user.id),
        action=events.SESSION_REVOKED,
    )
    if not revoked:
        # Someone else's session is indistinguishable from one that never existed.
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return Response(status_code=204)


@router.get("/tokens", summary="List your access tokens", response_model=list[TokenSummary])
async def list_tokens(
    credential: CurrentCredential, connection: DatabaseConnection
) -> list[TokenSummary]:
    stored = await identity.list_access_tokens(connection, user_id=credential.user.id)
    return [TokenSummary.of(token) for token in stored]


@router.post(
    "/tokens",
    summary="Create an access token",
    status_code=201,
    response_model=TokenCreated,
    responses={409: {"description": "A token of that name already exists"}},
)
async def create_token(
    payload: TokenCreateRequest, credential: CurrentCredential, connection: DatabaseConnection
) -> TokenCreated:
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days is not None
        else None
    )

    existing = await identity.list_access_tokens(connection, user_id=credential.user.id)
    if any(token.name == payload.name.strip() for token in existing):
        raise ProblemException(
            status=409,
            slug="conflict",
            title="Conflict",
            detail="You already have an access token with that name.",
            errors=[FieldProblem(detail="already in use", pointer="/body/name")],
        )

    plaintext, created = await identity.create_access_token(
        connection,
        user=credential.user,
        name=payload.name,
        scope=payload.scope,
        expires_at=expires_at,
    )
    return TokenCreated(token=plaintext, access_token=TokenSummary.of(created))


@router.delete(
    "/tokens/{token_id}",
    summary="Revoke an access token",
    status_code=204,
    responses={404: {"description": "No such token for this user"}},
)
async def revoke_token(
    token_id: UUID, credential: CurrentCredential, connection: DatabaseConnection
) -> Response:
    revoked = await identity.revoke_access_token(
        connection, token_id=token_id, user=credential.user
    )
    if not revoked:
        raise ProblemException(status=404, slug="not-found", title="Not found")
    return Response(status_code=204)
