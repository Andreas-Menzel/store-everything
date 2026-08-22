"""The identity surface end to end, against a real database.

These tests drive the API the way a client does — cookies, headers, status codes — because
the interesting failures live at that boundary: a session that authenticates a request it
should not, a credential that survives revocation, a response that leaks a token.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select

from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.problems import problem_type
from store_everything.tables import app_user, event
from tests.identity_helpers import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    SAME_ORIGIN,
    login,
    read_events,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AUTH = f"{API_V1_PREFIX}/auth"
USERS = f"{API_V1_PREFIX}/users"


# ------------------------------------------------------------------------ logging in


@pytest.mark.fr("F-027/FR-3")
async def test_login_sets_a_session_cookie_and_reports_the_caller(
    identity_client: httpx.AsyncClient,
) -> None:
    response = await login(identity_client)

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == ADMIN_EMAIL
    assert body["credential_kind"] == "session"

    cookie = response.cookies.get("se_session")
    assert cookie is not None
    assert cookie.startswith("sesess_")

    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/" in header


async def test_the_session_token_is_not_stored_in_readable_form(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    response = await login(identity_client)
    plaintext = response.cookies["se_session"]

    from sqlalchemy.ext.asyncio import create_async_engine

    from store_everything.tables import user_session

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            stored = (await connection.execute(select(user_session.c.token_hash))).scalars().all()
    finally:
        await engine.dispose()

    assert stored
    assert plaintext not in stored


async def test_a_wrong_password_is_refused_and_recorded(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    response = await identity_client.post(
        f"{AUTH}/login",
        json={"email": ADMIN_EMAIL, "password": "not-the-password"},
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 401
    assert response.json()["type"] == problem_type("authentication-required")
    assert "se_session" not in response.cookies

    # The failure survives the failed request: it is what the lockout counts, and what an
    # operator investigating an intrusion needs.
    failures = await read_events(identity_database, action="auth.login_failed")
    assert len(failures) == 1
    assert failures[0]["details"] == {"email": ADMIN_EMAIL}


async def test_an_unknown_address_is_indistinguishable_from_a_wrong_password(
    identity_client: httpx.AsyncClient,
) -> None:
    unknown = await identity_client.post(
        f"{AUTH}/login",
        json={"email": "nobody@example.com", "password": ADMIN_PASSWORD},
        headers=SAME_ORIGIN,
    )
    wrong = await identity_client.post(
        f"{AUTH}/login",
        json={"email": ADMIN_EMAIL, "password": "wrong-password-here"},
        headers=SAME_ORIGIN,
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_email_case_and_padding_do_not_create_a_second_account(
    identity_client: httpx.AsyncClient,
) -> None:
    response = await identity_client.post(
        f"{AUTH}/login",
        json={"email": f"  {ADMIN_EMAIL.upper()}  ", "password": ADMIN_PASSWORD},
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 200
    assert response.json()["user"]["email"] == ADMIN_EMAIL


async def test_repeated_failures_lock_the_account_out(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    for _ in range(3):
        refused = await identity_client.post(
            f"{AUTH}/login",
            json={"email": ADMIN_EMAIL, "password": "still-wrong"},
            headers=SAME_ORIGIN,
        )
        assert refused.status_code == 401

    # The fixture's ceiling is three attempts, so the fourth is refused before the password
    # is even looked at — and the correct password does not help.
    locked = await identity_client.post(
        f"{AUTH}/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        headers=SAME_ORIGIN,
    )

    assert locked.status_code == 429
    assert locked.headers["retry-after"]
    assert await read_events(identity_database, action="auth.rate_limited")


# ------------------------------------------------------------ using the session


async def test_me_answers_for_a_logged_in_caller(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    response = await identity_client.get(f"{AUTH}/me")

    assert response.status_code == 200
    assert response.json()["user"]["email"] == ADMIN_EMAIL


async def test_logout_revokes_the_session_it_used(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    logout = await identity_client.post(f"{AUTH}/logout", headers=SAME_ORIGIN)
    assert logout.status_code == 204

    # The cookie the client still holds is now worthless.
    assert (await identity_client.get(f"{AUTH}/me")).status_code == 401


async def test_a_revoked_session_stops_working(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)
    sessions = (await identity_client.get(f"{AUTH}/sessions")).json()
    assert len(sessions) == 1
    assert sessions[0]["current"] is True

    revoked = await identity_client.delete(
        f"{AUTH}/sessions/{sessions[0]['id']}", headers=SAME_ORIGIN
    )
    assert revoked.status_code == 204
    assert (await identity_client.get(f"{AUTH}/me")).status_code == 401


async def test_an_unsafe_request_without_an_origin_is_refused(
    identity_client: httpx.AsyncClient,
) -> None:
    """The cookie is ambient authority, so a state-changing call must prove where it came from."""
    await login(identity_client)

    response = await identity_client.post(f"{AUTH}/logout")

    assert response.status_code == 403
    assert response.json()["type"] == problem_type("cross-site-request")
    # Still logged in: the refusal happened before the handler.
    assert (await identity_client.get(f"{AUTH}/me")).status_code == 200


async def test_a_cross_site_unsafe_request_is_refused(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    response = await identity_client.post(
        f"{AUTH}/logout", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert (await identity_client.get(f"{AUTH}/me")).status_code == 200


async def test_sec_fetch_site_is_honoured_when_present(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    refused = await identity_client.post(
        f"{AUTH}/logout", headers={"Sec-Fetch-Site": "cross-site", "Origin": SAME_ORIGIN["Origin"]}
    )
    assert refused.status_code == 403

    allowed = await identity_client.post(
        f"{AUTH}/logout", headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert allowed.status_code == 204


async def test_a_safe_request_needs_no_origin(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    assert (await identity_client.get(f"{AUTH}/sessions")).status_code == 200


# ------------------------------------------------------------ access tokens


async def _create_token(client: httpx.AsyncClient, **payload: Any) -> dict[str, Any]:
    body = {"name": "agent", "scope": "read"} | payload
    response = await client.post(f"{AUTH}/tokens", json=body, headers=SAME_ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_personal_access_token_authenticates_without_a_cookie(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)
    created = await _create_token(identity_client, scope="full")
    plaintext = created["token"]
    assert plaintext.startswith("sepat_")

    # A fresh client: no cookie jar, only the header.
    response = await identity_app_client.get(
        f"{AUTH}/me", headers={"Authorization": f"Bearer {plaintext}"}
    )

    assert response.status_code == 200
    assert response.json()["credential_kind"] == "token"


async def test_a_token_is_never_shown_again(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)
    created = await _create_token(identity_client)

    listed = (await identity_client.get(f"{AUTH}/tokens")).json()

    assert len(listed) == 1
    assert listed[0]["id"] == created["access_token"]["id"]
    assert created["token"] not in (await identity_client.get(f"{AUTH}/tokens")).text


async def test_a_read_only_token_cannot_change_anything(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)
    created = await _create_token(identity_client, scope="read")
    headers = {"Authorization": f"Bearer {created['token']}"}

    readable = await identity_app_client.get(f"{AUTH}/tokens", headers=headers)
    assert readable.status_code == 200

    refused = await identity_app_client.post(
        f"{AUTH}/tokens", json={"name": "escalation", "scope": "full"}, headers=headers
    )
    assert refused.status_code == 403
    assert refused.json()["type"] == problem_type("insufficient-scope")


async def test_a_revoked_token_stops_working(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)
    created = await _create_token(identity_client, scope="full")
    headers = {"Authorization": f"Bearer {created['token']}"}

    assert (await identity_app_client.get(f"{AUTH}/me", headers=headers)).status_code == 200

    revoked = await identity_client.delete(
        f"{AUTH}/tokens/{created['access_token']['id']}", headers=SAME_ORIGIN
    )
    assert revoked.status_code == 204

    assert (await identity_app_client.get(f"{AUTH}/me", headers=headers)).status_code == 401


async def test_a_token_authenticated_caller_cannot_log_out(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)
    created = await _create_token(identity_client, scope="full")

    response = await identity_app_client.post(
        f"{AUTH}/logout", headers={"Authorization": f"Bearer {created['token']}"}
    )

    assert response.status_code == 409


async def test_duplicate_token_names_are_refused(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)
    await _create_token(identity_client, name="laptop")

    response = await identity_client.post(
        f"{AUTH}/tokens", json={"name": "laptop", "scope": "read"}, headers=SAME_ORIGIN
    )

    assert response.status_code == 409


async def test_a_garbled_authorization_header_is_refused(
    identity_app_client: httpx.AsyncClient,
) -> None:
    for value in ("", "Bearer", "Basic abc", "Bearer sepat_nonsense"):
        response = await identity_app_client.get(f"{AUTH}/me", headers={"Authorization": value})
        assert response.status_code == 401, value


# ------------------------------------------------------------ administration


async def test_an_admin_can_create_an_account_that_can_then_log_in(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)

    created = await identity_client.post(
        USERS,
        json={
            "email": "member@example.com",
            "display_name": "Member",
            "password": "a-long-enough-password",
            "role": "member",
        },
        headers=SAME_ORIGIN,
    )
    assert created.status_code == 201
    assert created.json()["role"] == "member"

    signed_in = await identity_app_client.post(
        f"{AUTH}/login",
        json={"email": "member@example.com", "password": "a-long-enough-password"},
        headers=SAME_ORIGIN,
    )
    assert signed_in.status_code == 200


async def test_a_member_cannot_administer_accounts(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)
    await identity_client.post(
        USERS,
        json={
            "email": "member@example.com",
            "display_name": "Member",
            "password": "a-long-enough-password",
            "role": "member",
        },
        headers=SAME_ORIGIN,
    )
    await login(identity_app_client, email="member@example.com", password="a-long-enough-password")

    listing = await identity_app_client.get(USERS)
    creating = await identity_app_client.post(
        USERS,
        json={
            "email": "another@example.com",
            "display_name": "Another",
            "password": "a-long-enough-password",
        },
        headers=SAME_ORIGIN,
    )

    assert listing.status_code == 403
    assert listing.json()["type"] == problem_type("admin-required")
    assert creating.status_code == 403


async def test_a_duplicate_email_is_refused(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    response = await identity_client.post(
        USERS,
        json={
            "email": ADMIN_EMAIL.upper(),
            "display_name": "Impostor",
            "password": "a-long-enough-password",
        },
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 409


async def test_a_short_password_is_refused_with_a_field_pointer(
    identity_client: httpx.AsyncClient,
) -> None:
    await login(identity_client)

    response = await identity_client.post(
        USERS,
        json={"email": "short@example.com", "display_name": "Short", "password": "tiny"},
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 422
    pointers = [error["pointer"] for error in response.json()["errors"]]
    assert "/body/password" in pointers
    # The submitted value is never echoed back.
    assert "tiny" not in response.text


async def test_disabling_an_account_locks_it_out_immediately(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)
    created = (
        await identity_client.post(
            USERS,
            json={
                "email": "member@example.com",
                "display_name": "Member",
                "password": "a-long-enough-password",
                "role": "member",
            },
            headers=SAME_ORIGIN,
        )
    ).json()

    await login(identity_app_client, email="member@example.com", password="a-long-enough-password")
    assert (await identity_app_client.get(f"{AUTH}/me")).status_code == 200

    disabled = await identity_client.patch(
        f"{USERS}/{created['id']}", json={"is_active": False}, headers=SAME_ORIGIN
    )
    assert disabled.status_code == 200

    # A live session belonging to a disabled account is refused with the terminal type, so
    # a caching client knows re-authenticating cannot help.
    response = await identity_app_client.get(f"{AUTH}/me")
    assert response.status_code == 401
    assert response.json()["type"] == problem_type("account-disabled")


async def test_the_last_active_admin_cannot_lock_themselves_out(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    me = (await login(identity_client)).json()
    admin_id = me["user"]["id"]

    demoted = await identity_client.patch(
        f"{USERS}/{admin_id}", json={"role": "member"}, headers=SAME_ORIGIN
    )
    disabled = await identity_client.patch(
        f"{USERS}/{admin_id}", json={"is_active": False}, headers=SAME_ORIGIN
    )

    assert demoted.status_code == 409
    assert disabled.status_code == 409

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(select(app_user.c.role).where(app_user.c.id == admin_id))
            ).scalar_one()
    finally:
        await engine.dispose()
    assert role == "admin"


async def test_changing_a_password_ends_every_session(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    await login(identity_client)
    created = (
        await identity_client.post(
            USERS,
            json={
                "email": "member@example.com",
                "display_name": "Member",
                "password": "a-long-enough-password",
                "role": "member",
            },
            headers=SAME_ORIGIN,
        )
    ).json()
    await login(identity_app_client, email="member@example.com", password="a-long-enough-password")

    await identity_client.patch(
        f"{USERS}/{created['id']}",
        json={"password": "a-different-long-password"},
        headers=SAME_ORIGIN,
    )

    assert (await identity_app_client.get(f"{AUTH}/me")).status_code == 401


async def test_an_empty_patch_is_a_validation_error(identity_client: httpx.AsyncClient) -> None:
    me = (await login(identity_client)).json()

    response = await identity_client.patch(
        f"{USERS}/{me['user']['id']}", json={}, headers=SAME_ORIGIN
    )

    assert response.status_code == 422


async def test_users_are_listed_page_by_page(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)
    for index in range(3):
        await identity_client.post(
            USERS,
            json={
                "email": f"member{index}@example.com",
                "display_name": f"Member {index}",
                "password": "a-long-enough-password",
            },
            headers=SAME_ORIGIN,
        )

    first = (await identity_client.get(USERS, params={"limit": 2})).json()
    assert len(first["data"]) == 2
    assert first["next_cursor"]

    second = (
        await identity_client.get(USERS, params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()

    assert len(second["data"]) == 2
    assert second["next_cursor"] is None
    # Keyset pagination: no item appears on both pages.
    assert not {user["id"] for user in first["data"]} & {user["id"] for user in second["data"]}


async def test_a_nonsense_cursor_is_a_validation_error(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    response = await identity_client.get(USERS, params={"cursor": "not-a-cursor"})

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/query/cursor"


async def test_an_unknown_account_is_not_found(identity_client: httpx.AsyncClient) -> None:
    await login(identity_client)

    response = await identity_client.get(f"{USERS}/0192f000-0000-7000-8000-000000000000")

    assert response.status_code == 404


# ------------------------------------------------------------ the event log


@pytest.mark.fr("F-011/FR-1", "02/INV-6")
async def test_every_mutation_leaves_an_event(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    await login(identity_client)
    created = (
        await identity_client.post(
            USERS,
            json={
                "email": "member@example.com",
                "display_name": "Member",
                "password": "a-long-enough-password",
            },
            headers=SAME_ORIGIN,
        )
    ).json()
    await identity_client.patch(
        f"{USERS}/{created['id']}", json={"display_name": "Renamed"}, headers=SAME_ORIGIN
    )
    token = await _create_token(identity_client)
    await identity_client.delete(
        f"{AUTH}/tokens/{token['access_token']['id']}", headers=SAME_ORIGIN
    )
    await identity_client.post(f"{AUTH}/logout", headers=SAME_ORIGIN)

    actions = [row["action"] for row in await read_events(identity_database)]

    assert actions == [
        "user.created",  # the bootstrap admin
        "auth.login_succeeded",
        "user.created",
        "user.updated",
        "token.created",
        "token.revoked",
        "auth.logged_out",
    ]


async def test_events_carry_the_request_id_of_their_cause(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    response = await login(identity_client)
    request_id = response.headers["x-request-id"]

    rows = await read_events(identity_database, action="auth.login_succeeded")

    assert rows[0]["request_id"] == request_id


async def test_no_event_detail_carries_a_credential(
    identity_client: httpx.AsyncClient, identity_database: str
) -> None:
    await login(identity_client)
    created = await _create_token(identity_client)
    await identity_client.post(
        f"{AUTH}/login", json={"email": ADMIN_EMAIL, "password": "wrong"}, headers=SAME_ORIGIN
    )

    rows = await read_events(identity_database)
    serialized = repr(rows)

    assert created["token"] not in serialized
    assert ADMIN_PASSWORD not in serialized
    assert "argon2" not in serialized
    for row in rows:
        for key in row["details"]:
            assert not any(
                secret in key.lower() for secret in ("password", "token", "secret", "credential")
            )


@pytest.mark.fr("F-011/FR-2")
async def test_an_event_row_cannot_be_updated(identity_database: str) -> None:
    """Immutability is enforced by the database, not by everyone remembering (F-011/FR-2)."""
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError, match="immutable"):
                await connection.execute(update(event).values(action="tampered"))
    finally:
        await engine.dispose()


async def test_a_disabled_account_cannot_log_in_even_with_the_right_password(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    """The failure is typed as terminal: re-authenticating will never help."""
    await login(identity_client)
    created = (
        await identity_client.post(
            USERS,
            json={
                "email": "member@example.com",
                "display_name": "Member",
                "password": "a-long-enough-password",
                "role": "member",
            },
            headers=SAME_ORIGIN,
        )
    ).json()
    await identity_client.patch(
        f"{USERS}/{created['id']}", json={"is_active": False}, headers=SAME_ORIGIN
    )

    response = await login(
        identity_app_client, email="member@example.com", password="a-long-enough-password"
    )

    assert response.status_code == 401
    assert response.json()["type"] == problem_type("account-disabled")
    assert "se_session" not in response.cookies


async def test_a_credential_belonging_to_someone_else_is_not_found(
    identity_client: httpx.AsyncClient, identity_app_client: httpx.AsyncClient
) -> None:
    """Revocation is scoped to the owner, and a stranger's id looks like a missing one."""
    await login(identity_client)
    await identity_client.post(
        USERS,
        json={
            "email": "member@example.com",
            "display_name": "Member",
            "password": "a-long-enough-password",
            "role": "member",
        },
        headers=SAME_ORIGIN,
    )
    admin_session = (await identity_client.get(f"{AUTH}/sessions")).json()[0]
    admin_token = await _create_token(identity_client, name="admins-token")

    await login(identity_app_client, email="member@example.com", password="a-long-enough-password")

    stolen_session = await identity_app_client.delete(
        f"{AUTH}/sessions/{admin_session['id']}", headers=SAME_ORIGIN
    )
    stolen_token = await identity_app_client.delete(
        f"{AUTH}/tokens/{admin_token['access_token']['id']}", headers=SAME_ORIGIN
    )

    assert stolen_session.status_code == 404
    assert stolen_token.status_code == 404
    # And the admin's own credentials still work.
    assert (await identity_client.get(f"{AUTH}/me")).status_code == 200


async def test_changing_an_unknown_account_is_not_found(
    identity_client: httpx.AsyncClient,
) -> None:
    await login(identity_client)

    response = await identity_client.patch(
        f"{USERS}/0192f000-0000-7000-8000-000000000000",
        json={"display_name": "Nobody"},
        headers=SAME_ORIGIN,
    )

    assert response.status_code == 404


async def test_logout_also_clears_the_cookie_in_the_browser(
    identity_client: httpx.AsyncClient,
) -> None:
    """Revoking server-side is what matters; clearing the cookie is what stops the browser
    from presenting a dead credential on every subsequent request."""
    await login(identity_client)

    response = await identity_client.post(f"{AUTH}/logout", headers=SAME_ORIGIN)

    cleared = response.headers.get_list("set-cookie")
    assert cleared, "expected logout to clear the session cookie"
    assert any("se_session=" in header and "Max-Age=0" in header for header in cleared)
