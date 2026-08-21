"""Creating the first administrator.

A fresh instance has no accounts, and every endpoint that could create one requires an
admin — so something has to break the circle. Two ways in, both audited
(07-identity-permissions-sharing.md § users):

- `SE_BOOTSTRAP_ADMIN_EMAIL` + `SE_BOOTSTRAP_ADMIN_PASSWORD` at start-up, for an
  unattended install;
- `store-everything create-admin`, for an operator who would rather not put a password in
  the environment.

Both are refused once *any* account exists, so neither is a standing back door. The check
and the insert are one statement, which makes two racing containers safe without a lock.
"""

from __future__ import annotations

import logging

from sqlalchemy import exists, insert, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from store_everything import events, identity, passwords
from store_everything.config import Settings
from store_everything.events import Actor
from store_everything.ids import new_id
from store_everything.tables import app_user

_logger = logging.getLogger(__name__)

BOOTSTRAP_DISPLAY_NAME = "Administrator"


async def create_first_admin(
    connection: AsyncConnection, *, email: str, password: str
) -> identity.User | None:
    """Create the first admin, or return `None` because the instance already has accounts.

    The emptiness test rides inside the `INSERT ... WHERE NOT EXISTS`, so it cannot be
    true-then-stale by the time the row is written.
    """
    passwords.check_policy(password)
    normalized = identity.normalize_email(email)

    result = await connection.execute(
        insert(app_user)
        .from_select(
            ["id", "email", "display_name", "password_hash", "role"],
            select(
                literal(new_id()),
                literal(normalized),
                literal(BOOTSTRAP_DISPLAY_NAME),
                literal(passwords.hash_password(password)),
                literal("admin"),
            ).where(~exists(select(app_user.c.id))),
        )
        .returning(
            app_user.c.id,
            app_user.c.email,
            app_user.c.display_name,
            app_user.c.role,
            app_user.c.is_active,
            app_user.c.created_at,
        )
    )
    row = result.first()
    if row is None:
        return None

    user = identity.User(*tuple(row))
    await events.record(
        connection,
        action=events.USER_CREATED,
        resource_type=events.RESOURCE_USER,
        resource_id=user.id,
        # Nobody is logged in yet, so the actor is the instance itself.
        actor=Actor.system(),
        details={"email": user.email, "role": user.role, "via": "bootstrap"},
    )
    return user


async def run_at_startup(engine: AsyncEngine, settings: Settings) -> None:
    """Apply the bootstrap configuration if there is any, and never fail start-up over it.

    An unreachable database or a pending migration is a normal state on a fresh install
    (`/readyz` already reports it); refusing to start would only make the operator's first
    experience worse. The variables stay effective until they succeed once.
    """
    password = settings.bootstrap_admin_password
    # `.env` ships both variables empty, and an unset environment variable written as
    # `SE_BOOTSTRAP_ADMIN_PASSWORD=` arrives as an empty string rather than as `None` —
    # so "absent" has to mean either.
    if not settings.bootstrap_admin_email.strip() or password is None:
        return
    if not password.get_secret_value():
        _logger.warning("SE_BOOTSTRAP_ADMIN_EMAIL is set but its password is empty")
        return

    try:
        async with engine.connect() as connection:
            created = await create_first_admin(
                connection,
                email=settings.bootstrap_admin_email,
                password=password.get_secret_value(),
            )
            await connection.commit()
    except passwords.WeakPasswordError as weak:
        _logger.error("bootstrap admin not created: %s", weak)
        return
    except Exception:
        _logger.warning("bootstrap admin could not be created yet", exc_info=True)
        return

    if created is None:
        _logger.warning(
            "SE_BOOTSTRAP_ADMIN_EMAIL is set but ignored: this instance already has accounts"
        )
    else:
        _logger.info("created the first administrator", extra={"email": created.email})
