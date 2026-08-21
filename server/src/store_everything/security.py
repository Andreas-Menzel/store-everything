"""The authentication boundary.

Deny by default: every endpoint declares its auth requirement, and a missing declaration
means *closed*, not open (08-api-principles.md § conventions). The `/api/v1` router carries
`require_auth` as a router-level dependency, so a route added without thinking about auth
is still refused.

Two credential kinds, deliberately not interchangeable (07-identity-permissions-sharing.md
§ tokens & credentials):

- **personal access tokens** in the `Authorization` header — for scripts, agents and the
  mobile apps; scoped, so a read-only token cannot write;
- **session tokens** in an `HttpOnly` cookie — for the browser, where a token readable by
  JavaScript is a token an injected script can exfiltrate.

The cookie is *ambient authority*: the browser attaches it to any request to this origin,
including one triggered by another site. So cookie-authenticated unsafe requests must prove
they came from our own origin, which is what `enforce_same_origin` does.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from store_everything import identity, tokens
from store_everything.config import Settings
from store_everything.identity import AccountDisabledError, Credential
from store_everything.problems import ProblemException
from store_everything.ratelimit import RequestLimiter, TooManyRequests, note_refusal

_logger = logging.getLogger(__name__)

_BEARER = "bearer"

#: Methods that cannot change state, so they need no cross-origin proof.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_RATE_LIMIT_WINDOW = timedelta(minutes=1)


class AuthenticationRequired(ProblemException):
    """`401` for a credential that is missing, unknown, expired or revoked.

    One type for all four on purpose: which of them it was is information about the
    credential an unauthenticated caller has not proven they own. The client reaction is
    identical — re-authenticate (08-api-principles.md § errors).
    """

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(
            status=401,
            slug="authentication-required",
            title="Authentication required",
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


class AccountDisabledProblem(ProblemException):
    """`401` for a valid credential whose account is disabled — a *terminal* state.

    Typed separately because clients react differently: re-authenticating cannot help, and
    a caching client must lock rather than keep retrying (14-client-sync-and-caching.md).
    """

    def __init__(self) -> None:
        super().__init__(
            status=401,
            slug="account-disabled",
            title="Account disabled",
            detail="This account has been disabled by an administrator.",
        )


class Forbidden(ProblemException):
    def __init__(self, *, slug: str, title: str, detail: str) -> None:
        super().__init__(status=403, slug=slug, title=title, detail=detail)


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def limiter_of(request: Request) -> RequestLimiter:
    return request.app.state.request_limiter


def client_ip(request: Request) -> str | None:
    """The caller's address.

    `X-Forwarded-*` is applied by the ASGI server, and only for proxy addresses the
    operator configured (ADR-0009) — so this is the real client when the deployment is set
    up correctly and the proxy's own address when it is not, never an attacker's claim.
    """
    return request.client.host if request.client is not None else None


def _expected_origin(request: Request) -> str | None:
    host = request.headers.get("host")
    return f"{request.url.scheme}://{host}".lower() if host else None


def enforce_same_origin(request: Request, *, required: bool) -> None:
    """Reject a state-changing request that a foreign site may have triggered.

    `Sec-Fetch-Site` is the reliable signal where it exists; `Origin` is the fallback.
    With `required`, absence of both is itself a refusal — that is the cookie case, where
    the browser would have sent one. Without it (password login, which carries no ambient
    authority), a request from a non-browser client is allowed to omit them.
    """
    if request.method in SAFE_METHODS:
        return

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        if fetch_site.lower() != "same-origin":
            raise Forbidden(
                slug="cross-site-request",
                title="Cross-site request refused",
                detail="This endpoint only accepts same-origin requests from a browser.",
            )
        return

    origin = request.headers.get("origin")
    if origin is not None:
        if origin.lower() != _expected_origin(request):
            raise Forbidden(
                slug="cross-site-request",
                title="Cross-site request refused",
                detail="The Origin header does not match this instance.",
            )
        return

    if required:
        raise Forbidden(
            slug="cross-site-request",
            title="Cross-site request refused",
            detail=(
                "A cookie-authenticated request must carry Origin or Sec-Fetch-Site. "
                "Use a personal access token for programmatic access."
            ),
        )


async def _authenticate(request: Request, connection: AsyncConnection) -> Credential:
    settings = settings_of(request)

    authorization = request.headers.get("authorization")
    if authorization is not None:
        kind, _, value = authorization.partition(" ")
        if kind.lower() != _BEARER or not value:
            raise AuthenticationRequired("Expected an `Authorization: Bearer <token>` header.")
        credential = await identity.authenticate_access_token(connection, token=value.strip())
        if credential is None:
            raise AuthenticationRequired("The access token is not valid.")
        return credential

    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise AuthenticationRequired("This endpoint requires authentication.")

    credential = await identity.authenticate_session(
        connection,
        token=cookie,
        idle_expiry=timedelta(days=settings.session_idle_expiry_days),
    )
    if credential is None:
        raise AuthenticationRequired("The session has expired or been revoked.")
    # Ambient authority: prove the request originated here before it may change state.
    enforce_same_origin(request, required=True)
    return credential


def _presented_credential(request: Request) -> str | None:
    """The raw credential the caller offered, whatever kind it is."""
    settings = settings_of(request)
    return request.headers.get("authorization") or request.cookies.get(settings.session_cookie_name)


async def require_auth(request: Request) -> Credential:
    """Authenticate the caller, or refuse the request.

    Deliberately **not** a consumer of the request's transaction. A request carrying no
    credential is refused before any connection is opened, which keeps a scanner hammering
    `/api/v1` from occupying the connection pool — and makes the answer `401` rather than
    `500` on an instance whose database is down. Credential verification then runs on its
    own short-lived connection: stamping `last_used_at` is a diagnostic that should persist
    even when the handler's own transaction rolls back.

    Also enforces token scope: a `read` token is refused on any state-changing method, so
    least privilege is a property of the boundary rather than of each handler.
    """
    if _presented_credential(request) is None:
        raise AuthenticationRequired("This endpoint requires authentication.")

    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.connect() as connection:
            credential = await _authenticate(request, connection)
            await connection.commit()
    except AccountDisabledError as disabled:
        raise AccountDisabledProblem() from disabled

    if credential.scope == "read" and request.method not in SAFE_METHODS:
        raise Forbidden(
            slug="insufficient-scope",
            title="Insufficient token scope",
            detail="This token is read-only.",
        )

    return credential


CurrentCredential = Annotated[Credential, Depends(require_auth)]


async def require_admin(credential: CurrentCredential) -> Credential:
    """Instance administration only. Note that admin is *not* data access (07)."""
    if not credential.user.is_admin:
        raise Forbidden(
            slug="admin-required",
            title="Administrator role required",
            detail="This endpoint is restricted to instance administrators.",
        )
    return credential


AdminCredential = Annotated[Credential, Depends(require_admin)]


async def enforce_request_ceiling(request: Request) -> None:
    """The app-level request ceiling for `/api/v1`.

    Keyed on the presented credential where there is one, so one noisy token cannot spend
    another user's budget, and on the client address otherwise. The key is a digest: raw
    credentials never become dictionary keys, and therefore never appear in a heap dump.

    The common case — a request under the limit — touches neither the database nor the
    disk, which is the point of an in-process counter. Only a refusal writes, and only
    once per window (`note_refusal`).
    """
    settings = settings_of(request)
    presented = _presented_credential(request)
    key = (
        f"credential:{tokens.digest(presented)}"
        if presented is not None
        else f"ip:{client_ip(request) or 'unknown'}"
    )

    if limiter_of(request).allow(key):
        return

    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.connect() as connection:
            await note_refusal(
                connection,
                scope="api",
                key=key,
                window=_RATE_LIMIT_WINDOW,
                client_ip=client_ip(request),
            )
            # The refusal outlives the request it refuses, so it commits on its own.
            await connection.commit()
    except SQLAlchemyError:
        # Being unable to record the refusal is no reason to stop refusing.
        _logger.warning("could not record a rate-limit refusal", exc_info=True)

    raise TooManyRequests(
        detail=f"More than {settings.rate_limit_per_minute} requests in one minute.",
        retry_after_seconds=60,
    )


__all__ = [
    "AccountDisabledProblem",
    "AdminCredential",
    "AuthenticationRequired",
    "CurrentCredential",
    "Forbidden",
    "client_ip",
    "enforce_request_ceiling",
    "enforce_same_origin",
    "require_admin",
    "require_auth",
    "settings_of",
]
