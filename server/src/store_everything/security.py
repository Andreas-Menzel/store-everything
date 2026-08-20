"""Authentication boundary.

Deny by default: every endpoint declares its auth requirement, and a missing declaration
means *closed*, not open (08-api-principles.md § conventions). The `/api/v1` router
carries `require_auth` as a router-level dependency, so a route added without thinking
about auth is still refused.

Identity itself — accounts, sessions, personal access tokens — is phase 1
(07-identity-permissions-sharing.md). Until it exists, every authenticated endpoint
answers `401`, which is the honest response: no credential can currently be valid.
"""

from __future__ import annotations

from store_everything.problems import ProblemException


class AuthenticationRequired(ProblemException):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(
            status=401,
            slug="authentication-required",
            title="Authentication required",
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_auth() -> None:
    """FastAPI dependency guarding every authenticated endpoint."""
    raise AuthenticationRequired(
        "This deployment has no identity provider yet; authentication arrives in phase 1."
    )
