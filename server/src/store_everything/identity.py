"""Accounts, sessions and personal access tokens: the data access for identity.

Hand-written SQL over SQLAlchemy Core, no ORM session (ADR-0012). Rows come back as
frozen dataclasses so the rest of the service works with typed values instead of mappings
whose keys a type checker cannot see.

Every function that changes state takes the caller's connection and writes its event on it
(ADR-0007) — there is no path through this module that mutates without recording why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from store_everything import events, passwords, tokens
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import access_token, app_user, user_session

Role = Literal["admin", "member"]
Scope = Literal["read", "full"]


class AccountDisabledError(Exception):
    """The credential is valid but its account is switched off.

    Raised rather than folded into "not authenticated" because the two are different facts
    with different client reactions, and the caller has already proven they hold the
    credential (08-api-principles.md § errors: terminal account states are typed).
    """


#: Re-exported from the module that mints the credentials this stamps, so sessions, personal
#: access tokens and extractor tokens share one policy rather than three copies of a number.
LAST_USED_RESOLUTION = tokens.LAST_USED_RESOLUTION


def normalize_email(email: str) -> str:
    """Case and surrounding space are not identity: `Alice@x` and `alice@x ` are one account."""
    return email.strip().lower()


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    email: str
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True, slots=True)
class Session:
    id: UUID
    user_id: UUID
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccessToken:
    id: UUID
    user_id: UUID
    name: str
    scope: Scope
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class Credential:
    """An authenticated credential and the user behind it."""

    user: User
    kind: Literal["session", "token"]
    id: UUID
    scope: Scope


_USER_COLUMNS = (
    app_user.c.id,
    app_user.c.email,
    app_user.c.display_name,
    app_user.c.role,
    app_user.c.is_active,
    app_user.c.created_at,
)


def _user_query() -> Select[tuple[UUID, str, str, Role, bool, datetime]]:
    return select(*_USER_COLUMNS)


def _as_user(row: tuple[UUID, str, str, Role, bool, datetime]) -> User:
    return User(*row)


# ------------------------------------------------------------------------ accounts


async def active_admin_ids(connection: AsyncConnection) -> set[UUID]:
    """Who can still administer this instance — the guard against locking everyone out."""
    result = await connection.execute(
        select(app_user.c.id).where(app_user.c.role == "admin", app_user.c.is_active.is_(True))
    )
    return {row[0] for row in result.all()}


async def get_user(connection: AsyncConnection, user_id: UUID) -> User | None:
    result = await connection.execute(_user_query().where(app_user.c.id == user_id))
    row = result.first()
    return _as_user(tuple(row)) if row is not None else None


async def find_user_by_email(connection: AsyncConnection, email: str) -> User | None:
    result = await connection.execute(
        _user_query().where(app_user.c.email == normalize_email(email))
    )
    row = result.first()
    return _as_user(tuple(row)) if row is not None else None


async def list_users(
    connection: AsyncConnection,
    *,
    limit: int,
    after: tuple[datetime, UUID] | None = None,
) -> list[User]:
    """Users in creation order, keyset-paginated (08-api-principles.md § pagination)."""
    query = _user_query().order_by(app_user.c.created_at, app_user.c.id).limit(limit)
    if after is not None:
        created_at, user_id = after
        query = query.where(
            or_(
                app_user.c.created_at > created_at,
                and_(app_user.c.created_at == created_at, app_user.c.id > user_id),
            )
        )
    result = await connection.execute(query)
    return [_as_user(tuple(row)) for row in result.all()]


async def create_user(
    connection: AsyncConnection,
    *,
    email: str,
    display_name: str,
    password: str,
    role: Role,
    actor: Actor,
) -> User:
    """Create an account. Raises `passwords.WeakPasswordError` before touching the database."""
    password_hash = passwords.hash_password(password)
    normalized = normalize_email(email)

    result = await connection.execute(
        insert(app_user)
        .values(
            id=new_id(),
            email=normalized,
            display_name=display_name.strip(),
            password_hash=password_hash,
            role=role,
        )
        .returning(*_USER_COLUMNS)
    )
    user = _as_user(tuple(result.one()))

    await events.record(
        connection,
        action=events.USER_CREATED,
        resource_type=events.RESOURCE_USER,
        resource_id=user.id,
        actor=actor,
        details={"email": user.email, "display_name": user.display_name, "role": user.role},
    )
    return user


async def update_user(
    connection: AsyncConnection,
    *,
    user_id: UUID,
    actor: Actor,
    display_name: str | None = None,
    role: Role | None = None,
    is_active: bool | None = None,
    password: str | None = None,
) -> User | None:
    """Apply the given changes and record what changed. `None` means "leave alone"."""
    changes: dict[str, object] = {}
    if display_name is not None:
        changes["display_name"] = display_name.strip()
    if role is not None:
        changes["role"] = role
    if is_active is not None:
        changes["is_active"] = is_active
    if password is not None:
        changes["password_hash"] = passwords.hash_password(password)

    if not changes:
        return await get_user(connection, user_id)

    result = await connection.execute(
        update(app_user)
        .where(app_user.c.id == user_id)
        .values(**changes, updated_at=func.now())
        .returning(*_USER_COLUMNS)
    )
    row = result.first()
    if row is None:
        return None
    user = _as_user(tuple(row))

    # The audit record names the fields that changed, never their old or new secret values.
    await events.record(
        connection,
        action=events.USER_UPDATED,
        resource_type=events.RESOURCE_USER,
        resource_id=user.id,
        actor=actor,
        details={"email": user.email, "fields": sorted(changes)},
    )
    if password is not None:
        await events.record(
            connection,
            action=events.USER_PASSWORD_CHANGED,
            resource_type=events.RESOURCE_USER,
            resource_id=user.id,
            actor=actor,
            details={"email": user.email},
        )
        # A new password invalidates every session: that is the point of changing it.
        await revoke_all_sessions(connection, user_id=user.id, actor=actor)

    return user


async def verify_credentials(
    connection: AsyncConnection, *, email: str, password: str
) -> User | None:
    """Return the user when the password matches and the account is usable.

    The stored hash is re-hashed transparently when the cost parameters have moved on.
    """
    result = await connection.execute(
        select(*_USER_COLUMNS, app_user.c.password_hash).where(
            app_user.c.email == normalize_email(email)
        )
    )
    row = result.first()
    if row is None:
        # No account: still pay the hashing cost, so response time does not disclose
        # whether an email is registered.
        passwords.verify_password(_absent_user_hash(), password)
        return None

    *user_values, password_hash = tuple(row)
    if not passwords.verify_password(password_hash, password):
        return None

    user = _as_user(tuple(user_values))  # pyright: ignore[reportArgumentType]
    if not user.is_active:
        raise AccountDisabledError(user.email)

    if passwords.needs_rehash(password_hash):
        await connection.execute(
            update(app_user)
            .where(app_user.c.id == user.id)
            .values(password_hash=passwords.hash_password(password), updated_at=func.now())
        )

    return user


@cache
def _absent_user_hash() -> str:
    """A real argon2id hash of a value nobody knows.

    Verifying against it makes a login for an unregistered address cost the same as one for
    a registered address, so response time does not disclose which accounts exist. Computed
    on first use rather than at import, because hashing is deliberately slow and every
    process — including the CLI — would otherwise pay for it.
    """
    return passwords.hash_password(tokens.mint("absent_").plaintext)


# ------------------------------------------------------------------------ sessions


async def create_session(
    connection: AsyncConnection,
    *,
    user: User,
    idle_expiry: timedelta,
    user_agent: str | None,
    client_ip: str | None,
) -> tuple[str, Session]:
    """Open a session and return its plaintext token — the only time it exists."""
    minted = tokens.mint(tokens.SESSION_TOKEN_PREFIX)
    now = datetime.now(UTC)

    result = await connection.execute(
        insert(user_session)
        .values(
            id=new_id(),
            user_id=user.id,
            token_hash=minted.digest,
            user_agent=user_agent,
            expires_at=now + idle_expiry,
        )
        .returning(
            user_session.c.id,
            user_session.c.user_id,
            user_session.c.user_agent,
            user_session.c.created_at,
            user_session.c.last_used_at,
            user_session.c.expires_at,
        )
    )
    session = Session(*tuple(result.one()))

    await events.record(
        connection,
        action=events.LOGIN_SUCCEEDED,
        resource_type=events.RESOURCE_SESSION,
        resource_id=session.id,
        actor=Actor.user(user.id),
        details={"email": user.email},
        client_ip=client_ip,
    )
    return minted.plaintext, session


async def authenticate_session(
    connection: AsyncConnection, *, token: str, idle_expiry: timedelta
) -> Credential | None:
    """Resolve a session token, extending its idle expiry.

    The lookup is an indexed equality on the token's digest, so the secret itself is never
    compared in application code.
    """
    now = datetime.now(UTC)
    result = await connection.execute(
        select(
            user_session.c.id,
            user_session.c.last_used_at,
            *_USER_COLUMNS,
        )
        .join(app_user, app_user.c.id == user_session.c.user_id)
        .where(
            user_session.c.token_hash == tokens.digest(token),
            user_session.c.revoked_at.is_(None),
            user_session.c.expires_at > now,
        )
    )
    row = result.first()
    if row is None:
        return None

    session_id, last_used_at, *user_values = tuple(row)
    user = _as_user(tuple(user_values))  # pyright: ignore[reportArgumentType]
    if not user.is_active:
        raise AccountDisabledError(user.email)

    if now - last_used_at >= LAST_USED_RESOLUTION:
        await connection.execute(
            update(user_session)
            .where(user_session.c.id == session_id)
            .values(last_used_at=now, expires_at=now + idle_expiry)
        )

    return Credential(user=user, kind="session", id=session_id, scope="full")


async def list_sessions(connection: AsyncConnection, *, user_id: UUID) -> list[Session]:
    """A user's live sessions, newest first."""
    result = await connection.execute(
        select(
            user_session.c.id,
            user_session.c.user_id,
            user_session.c.user_agent,
            user_session.c.created_at,
            user_session.c.last_used_at,
            user_session.c.expires_at,
        )
        .where(
            user_session.c.user_id == user_id,
            user_session.c.revoked_at.is_(None),
            user_session.c.expires_at > datetime.now(UTC),
        )
        .order_by(user_session.c.created_at.desc())
    )
    return [Session(*tuple(row)) for row in result.all()]


async def revoke_session(
    connection: AsyncConnection, *, session_id: UUID, user_id: UUID, actor: Actor, action: str
) -> bool:
    """Revoke one of a user's sessions. False when it was already gone or never theirs."""
    result = await connection.execute(
        update(user_session)
        .where(
            user_session.c.id == session_id,
            user_session.c.user_id == user_id,
            user_session.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(user_session.c.id)
    )
    if result.first() is None:
        return False

    await events.record(
        connection,
        action=action,
        resource_type=events.RESOURCE_SESSION,
        resource_id=session_id,
        actor=actor,
    )
    return True


async def revoke_all_sessions(connection: AsyncConnection, *, user_id: UUID, actor: Actor) -> int:
    result = await connection.execute(
        update(user_session)
        .where(user_session.c.user_id == user_id, user_session.c.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(user_session.c.id)
    )
    revoked = [row[0] for row in result.all()]
    for session_id in revoked:
        await events.record(
            connection,
            action=events.SESSION_REVOKED,
            resource_type=events.RESOURCE_SESSION,
            resource_id=session_id,
            actor=actor,
        )
    return len(revoked)


# ------------------------------------------------------------- personal access tokens

_TOKEN_COLUMNS = (
    access_token.c.id,
    access_token.c.user_id,
    access_token.c.name,
    access_token.c.scope,
    access_token.c.created_at,
    access_token.c.last_used_at,
    access_token.c.expires_at,
)


async def create_access_token(
    connection: AsyncConnection,
    *,
    user: User,
    name: str,
    scope: Scope,
    expires_at: datetime | None,
) -> tuple[str, AccessToken]:
    """Mint a personal access token. Its plaintext is returned once and never stored."""
    minted = tokens.mint(tokens.ACCESS_TOKEN_PREFIX)

    result = await connection.execute(
        insert(access_token)
        .values(
            id=new_id(),
            user_id=user.id,
            name=name.strip(),
            token_hash=minted.digest,
            scope=scope,
            expires_at=expires_at,
        )
        .returning(*_TOKEN_COLUMNS)
    )
    created = AccessToken(*tuple(result.one()))

    await events.record(
        connection,
        action=events.TOKEN_CREATED,
        resource_type=events.RESOURCE_TOKEN,
        resource_id=created.id,
        actor=Actor.user(user.id),
        # The token's name and scope, never its value.
        details={"name": created.name, "scope": created.scope},
    )
    return minted.plaintext, created


async def authenticate_access_token(
    connection: AsyncConnection, *, token: str
) -> Credential | None:
    now = datetime.now(UTC)
    result = await connection.execute(
        select(
            access_token.c.id,
            access_token.c.scope,
            access_token.c.last_used_at,
            *_USER_COLUMNS,
        )
        .join(app_user, app_user.c.id == access_token.c.user_id)
        .where(
            access_token.c.token_hash == tokens.digest(token),
            access_token.c.revoked_at.is_(None),
            or_(access_token.c.expires_at.is_(None), access_token.c.expires_at > now),
        )
    )
    row = result.first()
    if row is None:
        return None

    token_id, scope, last_used_at, *user_values = tuple(row)
    user = _as_user(tuple(user_values))  # pyright: ignore[reportArgumentType]
    if not user.is_active:
        raise AccountDisabledError(user.email)

    if last_used_at is None or now - last_used_at >= LAST_USED_RESOLUTION:
        await connection.execute(
            update(access_token).where(access_token.c.id == token_id).values(last_used_at=now)
        )

    return Credential(user=user, kind="token", id=token_id, scope=scope)


async def list_access_tokens(connection: AsyncConnection, *, user_id: UUID) -> list[AccessToken]:
    result = await connection.execute(
        select(*_TOKEN_COLUMNS)
        .where(access_token.c.user_id == user_id, access_token.c.revoked_at.is_(None))
        .order_by(access_token.c.created_at.desc())
    )
    return [AccessToken(*tuple(row)) for row in result.all()]


async def revoke_access_token(connection: AsyncConnection, *, token_id: UUID, user: User) -> bool:
    result = await connection.execute(
        update(access_token)
        .where(
            access_token.c.id == token_id,
            access_token.c.user_id == user.id,
            access_token.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(access_token.c.name)
    )
    row = result.first()
    if row is None:
        return False

    await events.record(
        connection,
        action=events.TOKEN_REVOKED,
        resource_type=events.RESOURCE_TOKEN,
        resource_id=token_id,
        actor=Actor.user(user.id),
        details={"name": row[0]},
    )
    return True
