"""The extractor registry, from both sides of the boundary.

Two audiences meet here, and almost every test below is about the line between them: an
administrator provisions an extractor id and its credential, and a container declares what it
can do. So the interesting cases are the refusals — a manifest claiming somebody else's id, a
second producer of one rendition kind, a credential that works on the wrong API — because each
one is a rule from [ADR-0020](../../decisions/ADR-0020-extractor-dispatch-and-wire-protocol.md)
that would otherwise be enforced only by good behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from store_everything.api.extractor_api.router import EXTRACTOR_API_PREFIX
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.problems import problem_type
from store_everything.tokens import EXTRACTOR_TOKEN_PREFIX
from tests.identity_helpers import BASE_URL, SAME_ORIGIN, read_events
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    create_member,
    instance,
    signed_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

EXTRACTORS = f"{API_V1_PREFIX}/extractors"
REGISTRATION = f"{EXTRACTOR_API_PREFIX}/registration"


def manifest(**overrides: Any) -> dict[str, Any]:
    """A minimal valid manifest — `pdf-text` as spec 05 describes it."""
    document: dict[str, Any] = {
        "id": "pdf-text",
        "version": "1.0.0",
        "api_version": "v1",
        "model": {"name": "pymupdf", "version": "1.28"},
        "accepts": {"mime_types": ["application/pdf"]},
        "produces": ["text_segments", "metadata"],
        "cost_class": "medium",
    }
    document.update(overrides)
    return document


async def provision(
    admin: httpx.AsyncClient, extractor_id: str = "pdf-text", **body: Any
) -> dict[str, Any]:
    response = await admin.post(EXTRACTORS, json={"id": extractor_id, **body}, headers=SAME_ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


@asynccontextmanager
async def as_extractor(app: FastAPI, token: str) -> AsyncGenerator[httpx.AsyncClient]:
    """A client that presents an extractor credential — no cookie, no origin, like a container."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


# ------------------------------------------------------------------------- provisioning


async def test_provisioning_mints_a_credential_and_lists_the_extractor(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        body = await provision(admin)

        assert body["token"].startswith(EXTRACTOR_TOKEN_PREFIX)
        assert body["extractor"]["id"] == "pdf-text"
        # Provisioned but never started: the manifest half is empty, and honestly so.
        assert body["extractor"]["registered"] is False
        assert body["extractor"]["enabled"] is True
        assert body["extractor"]["version"] is None
        assert body["extractor"]["last_seen_at"] is None
        assert body["extractor_token"]["name"] == "initial"

        listed = await admin.get(EXTRACTORS)
        assert [item["id"] for item in listed.json()] == ["pdf-text"]

        events = await read_events(identity_database, action="extractor.provisioned")
        assert [event["details"]["extractor_id"] for event in events] == ["pdf-text"]
        assert events[0]["actor_type"] == "user"

        # The credential is its own record, so a rotation is legible in the audit trail.
        minted = await read_events(identity_database, action="extractor.token_created")
        assert minted[0]["details"] == {"extractor_id": "pdf-text", "name": "initial"}


async def test_the_credential_plaintext_is_never_shown_again(
    identity_settings: Settings,
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await provision(admin)

        listed = await admin.get(f"{EXTRACTORS}/pdf-text/tokens")

        assert listed.status_code == 200, listed.text
        assert [token["name"] for token in listed.json()] == ["initial"]
        assert "token" not in listed.text


async def test_provisioning_the_same_id_twice_is_refused(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await provision(admin)

        again = await admin.post(EXTRACTORS, json={"id": "pdf-text"}, headers=SAME_ORIGIN)

        assert again.status_code == 409
        assert again.json()["type"] == problem_type("conflict")


@pytest.mark.parametrize("candidate", ["PDF-Text", "pdf_text", "pdf--text", "-pdf", ""])
async def test_an_id_that_is_not_a_name_we_can_route_on_is_refused(
    identity_settings: Settings, candidate: str
) -> None:
    """Extractor ids end up in operation kinds and URLs, so the shape is enforced at the edge."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        response = await admin.post(EXTRACTORS, json={"id": candidate}, headers=SAME_ORIGIN)

        assert response.status_code == 422, response.text


async def test_only_an_administrator_may_manage_extractors(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await create_member(admin)
        await provision(admin)

        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            listed = await member.get(EXTRACTORS)
            provisioning = await member.post(
                EXTRACTORS, json={"id": "image-vision"}, headers=SAME_ORIGIN
            )
            disabling = await member.patch(
                f"{EXTRACTORS}/pdf-text", json={"enabled": False}, headers=SAME_ORIGIN
            )

        assert listed.status_code == 403
        assert provisioning.status_code == 403
        assert disabling.status_code == 403

    async with (
        instance(identity_settings) as app,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as anonymous,
    ):
        assert (await anonymous.get(EXTRACTORS)).status_code == 401


# -------------------------------------------------------------------------- registration


async def test_a_container_registers_what_it_can_do(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]

        async with as_extractor(app, token) as extractor:
            response = await extractor.put(REGISTRATION, json=manifest())

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["changed"] is True
        assert body["registered_at"] is not None
        assert body["manifest"]["produces"] == ["text_segments", "metadata"]

        stored = (await admin.get(f"{EXTRACTORS}/pdf-text")).json()
        assert stored["registered"] is True
        assert stored["version"] == "1.0.0"
        assert stored["model_name"] == "pymupdf"
        assert stored["model_version"] == "1.28"
        assert stored["cost_class"] == "medium"
        # Declared, and visible to an admin without reading a compose file: this is the
        # extractor that may talk to the outside world, or in this case may not (ADR-0021).
        assert stored["network"] == "none"
        assert stored["last_seen_at"] is not None

        events = await read_events(identity_database, action="extractor.registered")
        assert len(events) == 1
        assert events[0]["actor_type"] == "extractor"
        assert events[0]["details"] == {
            "extractor_id": "pdf-text",
            "version": "1.0.0",
            "model_version": "1.28",
            "previous_version": None,
            "previous_model_version": None,
        }


async def test_re_registering_the_same_manifest_records_nothing(
    identity_settings: Settings, identity_database: str
) -> None:
    """Containers restart. The event log is the one table nothing ever deletes."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]

        async with as_extractor(app, token) as extractor:
            first = await extractor.put(REGISTRATION, json=manifest())
            second = await extractor.put(REGISTRATION, json=manifest())

        assert first.json()["changed"] is True
        assert second.json()["changed"] is False
        # Same manifest, same `registered_at`: nothing was rewritten.
        assert second.json()["registered_at"] == first.json()["registered_at"]
        assert len(await read_events(identity_database, action="extractor.registered")) == 1


async def test_a_new_version_records_the_one_it_replaced(
    identity_settings: Settings, identity_database: str
) -> None:
    """The eligibility data reprocessing needs (F-009/FR-2) is written when the version moves."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]

        async with as_extractor(app, token) as extractor:
            await extractor.put(REGISTRATION, json=manifest())
            upgraded = await extractor.put(
                REGISTRATION,
                json=manifest(version="1.1.0", model={"name": "pymupdf", "version": "1.29"}),
            )

        assert upgraded.json()["changed"] is True
        events = await read_events(identity_database, action="extractor.registered")
        assert len(events) == 2
        assert events[-1]["details"] == {
            "extractor_id": "pdf-text",
            "version": "1.1.0",
            "model_version": "1.29",
            "previous_version": "1.0.0",
            "previous_model_version": "1.28",
        }


async def test_a_manifest_may_not_claim_another_extractors_identity(
    identity_settings: Settings,
) -> None:
    """A credential says *which* extractor this is; a manifest cannot contradict it."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]
        await provision(admin, "image-vision")

        async with as_extractor(app, token) as extractor:
            response = await extractor.put(REGISTRATION, json=manifest(id="image-vision"))

        assert response.status_code == 403
        assert response.json()["type"] == problem_type("extractor-identity-mismatch")

        stored = (await admin.get(f"{EXTRACTORS}/image-vision")).json()
        assert stored["registered"] is False


async def test_a_contract_version_this_core_does_not_speak_is_refused(
    identity_settings: Settings,
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]

        async with as_extractor(app, token) as extractor:
            response = await extractor.put(REGISTRATION, json=manifest(api_version="v2"))

        assert response.status_code == 409
        assert response.json()["type"] == problem_type("unsupported-contract-version")
        assert "v2" in response.json()["detail"]


async def test_unknown_manifest_fields_survive_registration(identity_settings: Settings) -> None:
    """Forward compatibility within `v1.x` (05 § compatibility rules), and the echo that
    lets an author see a mistyped field instead of wondering why it did nothing."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]

        async with as_extractor(app, token) as extractor:
            response = await extractor.put(
                REGISTRATION, json=manifest(future_capability={"beam": "me up"})
            )

        assert response.status_code == 200, response.text
        assert response.json()["manifest"]["future_capability"] == {"beam": "me up"}
        stored = (await admin.get(f"{EXTRACTORS}/pdf-text")).json()
        assert stored["manifest"]["future_capability"] == {"beam": "me up"}


@pytest.mark.parametrize(
    ("overrides", "because"),
    [
        ({"accepts": {}}, "an extractor that accepts nothing can never be routed work"),
        ({"produces": []}, "an extractor that produces nothing has no reason to run"),
        (
            {"produces": ["renditions"]},
            "a declared renditions output with no kinds is unroutable",
        ),
        (
            {"renditions": [{"kind": "searchable-pdf", "format": "pdf", "label": "Searchable"}]},
            "kinds without the matching output claim a namespace nothing writes to",
        ),
        ({"produces": ["embeddings"]}, "the same rule for embedding spaces"),
        ({"embedding_spaces": ["text-v1"]}, "and in the other direction"),
        ({"accepts": {"mime_types": ["application"]}}, "not a media-type pattern"),
        ({"produces": ["metadata", "metadata"]}, "a duplicate output kind"),
        ({"cost_class": "trivial"}, "not one of the declared cost classes"),
        ({"model": {"name": "pymupdf"}}, "a model without a version cannot be provenance"),
    ],
)
async def test_an_incoherent_manifest_is_refused(
    identity_settings: Settings, overrides: dict[str, Any], because: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]

        async with as_extractor(app, token) as extractor:
            response = await extractor.put(REGISTRATION, json=manifest(**overrides))

        assert response.status_code == 422, f"{because}: {response.text}"
        assert response.json()["errors"], because


# ------------------------------------------------------------------- single-provider kinds


def _rendition_manifest(extractor_id: str, kind: str = "searchable-pdf") -> dict[str, Any]:
    return manifest(
        id=extractor_id,
        produces=["text_segments", "renditions"],
        renditions=[{"kind": kind, "format": "pdf", "label": "Searchable PDF"}],
    )


async def test_two_extractors_cannot_produce_the_same_rendition_kind(
    identity_settings: Settings,
) -> None:
    """The rule the primary key of `extractor_claim` enforces: "which one wins" has no answer,
    so it is never asked."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        first = (await provision(admin, "tesseract-ocr"))["token"]
        second = (await provision(admin, "other-ocr"))["token"]

        async with as_extractor(app, first) as extractor:
            accepted = await extractor.put(REGISTRATION, json=_rendition_manifest("tesseract-ocr"))
        async with as_extractor(app, second) as extractor:
            refused = await extractor.put(REGISTRATION, json=_rendition_manifest("other-ocr"))

        assert accepted.status_code == 200, accepted.text
        assert refused.status_code == 409, refused.text
        problem = refused.json()
        assert problem["type"] == problem_type("output-kind-already-claimed")
        # Names the claimant, so an admin knows which of the two to change.
        assert "tesseract-ocr" in problem["detail"]
        assert "searchable-pdf" in problem["detail"]
        assert problem["errors"][0]["pointer"] == "/body/renditions"

        # And the refusal left the second extractor's previous state alone.
        stored = (await admin.get(f"{EXTRACTORS}/other-ocr")).json()
        assert stored["registered"] is False


@pytest.mark.parametrize(
    ("overrides", "pointer"),
    [
        (
            {"produces": ["metadata", "embeddings"], "embedding_spaces": ["clip-v1"]},
            "/body/embedding_spaces",
        ),
        (
            {"produces": ["metadata", "derived_assets"], "derived_asset_kinds": ["keyframe"]},
            "/body/derived_asset_kinds",
        ),
    ],
)
async def test_embedding_spaces_and_derived_kinds_are_exclusive_too(
    identity_settings: Settings, overrides: dict[str, Any], pointer: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        first = (await provision(admin, "image-vision"))["token"]
        second = (await provision(admin, "other-vision"))["token"]

        async with as_extractor(app, first) as extractor:
            accepted = await extractor.put(
                REGISTRATION, json=manifest(id="image-vision", **overrides)
            )
        async with as_extractor(app, second) as extractor:
            refused = await extractor.put(
                REGISTRATION, json=manifest(id="other-vision", **overrides)
            )

        assert accepted.status_code == 200, accepted.text
        assert refused.status_code == 409, refused.text
        assert refused.json()["errors"][0]["pointer"] == pointer


async def test_releasing_a_kind_lets_another_extractor_take_it(
    identity_settings: Settings,
) -> None:
    """A capability can move between extractors — the two halves just cannot overlap."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        first = (await provision(admin, "tesseract-ocr"))["token"]
        second = (await provision(admin, "other-ocr"))["token"]

        async with as_extractor(app, first) as extractor:
            await extractor.put(REGISTRATION, json=_rendition_manifest("tesseract-ocr"))
            # The same extractor, no longer producing renditions at all.
            released = await extractor.put(
                REGISTRATION, json=manifest(id="tesseract-ocr", produces=["text_segments"])
            )
        async with as_extractor(app, second) as extractor:
            taken = await extractor.put(REGISTRATION, json=_rendition_manifest("other-ocr"))

        assert released.status_code == 200, released.text
        assert taken.status_code == 200, taken.text


async def test_a_refused_registration_releases_nothing(identity_settings: Settings) -> None:
    """The order in `register()`, asserted from outside: kinds are settled before the manifest
    lands, so an extractor that tries to swap one of its kinds for a taken one keeps what it
    had rather than ending up owning neither."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        first = (await provision(admin, "tesseract-ocr"))["token"]
        second = (await provision(admin, "other-ocr"))["token"]
        third = (await provision(admin, "third-ocr"))["token"]

        async with as_extractor(app, first) as extractor:
            await extractor.put(REGISTRATION, json=_rendition_manifest("tesseract-ocr"))
        async with as_extractor(app, second) as extractor:
            await extractor.put(
                REGISTRATION, json=_rendition_manifest("other-ocr", "subtitles-srt")
            )

        # Release `searchable-pdf`, claim a kind the other one owns: all or nothing.
        async with as_extractor(app, first) as extractor:
            refused = await extractor.put(
                REGISTRATION, json=_rendition_manifest("tesseract-ocr", "subtitles-srt")
            )
        # If the release had stuck, this would now succeed.
        async with as_extractor(app, third) as extractor:
            poaching = await extractor.put(REGISTRATION, json=_rendition_manifest("third-ocr"))

        assert refused.status_code == 409, refused.text
        assert poaching.status_code == 409, poaching.text
        assert "tesseract-ocr" in poaching.json()["detail"]


async def test_an_extractor_keeps_its_own_kinds_across_registrations(
    identity_settings: Settings,
) -> None:
    """Re-declaring what you already own is not a conflict with yourself."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin, "tesseract-ocr"))["token"]

        async with as_extractor(app, token) as extractor:
            await extractor.put(REGISTRATION, json=_rendition_manifest("tesseract-ocr"))
            again = await extractor.put(
                REGISTRATION,
                json=_rendition_manifest("tesseract-ocr") | {"version": "1.2.0"},
            )

        assert again.status_code == 200, again.text
        assert again.json()["changed"] is True


# --------------------------------------------------------------------------- credentials


async def test_a_revoked_credential_stops_working(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        body = await provision(admin)
        token, token_id = body["token"], body["extractor_token"]["id"]

        revoked = await admin.delete(
            f"{EXTRACTORS}/pdf-text/tokens/{token_id}", headers=SAME_ORIGIN
        )
        async with as_extractor(app, token) as extractor:
            response = await extractor.put(REGISTRATION, json=manifest())

        assert revoked.status_code == 204, revoked.text
        assert response.status_code == 401
        assert response.json()["type"] == problem_type("authentication-required")
        assert await read_events(identity_database, action="extractor.token_revoked")

        # Revoking it twice is not a second revocation.
        assert (
            await admin.delete(f"{EXTRACTORS}/pdf-text/tokens/{token_id}", headers=SAME_ORIGIN)
        ).status_code == 404


async def test_a_credential_can_be_rotated_without_a_gap(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        body = await provision(admin)
        old_token, old_id = body["token"], body["extractor_token"]["id"]

        minted = await admin.post(
            f"{EXTRACTORS}/pdf-text/tokens", json={"name": "rotated"}, headers=SAME_ORIGIN
        )
        assert minted.status_code == 201, minted.text
        new_token = minted.json()["token"]

        # Both work while the container is being restarted with the new one.
        async with as_extractor(app, old_token) as extractor:
            assert (await extractor.put(REGISTRATION, json=manifest())).status_code == 200
        async with as_extractor(app, new_token) as extractor:
            assert (await extractor.put(REGISTRATION, json=manifest())).status_code == 200

        await admin.delete(f"{EXTRACTORS}/pdf-text/tokens/{old_id}", headers=SAME_ORIGIN)

        async with as_extractor(app, old_token) as extractor:
            assert (await extractor.put(REGISTRATION, json=manifest())).status_code == 401
        async with as_extractor(app, new_token) as extractor:
            assert (await extractor.put(REGISTRATION, json=manifest())).status_code == 200


async def test_a_credential_name_is_not_reused_after_revocation(
    identity_settings: Settings,
) -> None:
    """Revoked rows keep their name so the audit trail stays readable — so the name is spent."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        body = await provision(admin)
        await admin.delete(
            f"{EXTRACTORS}/pdf-text/tokens/{body['extractor_token']['id']}", headers=SAME_ORIGIN
        )

        again = await admin.post(
            f"{EXTRACTORS}/pdf-text/tokens", json={"name": "initial"}, headers=SAME_ORIGIN
        )

        assert again.status_code == 409, again.text
        assert again.json()["errors"][0]["pointer"] == "/body/name"


async def test_the_two_credential_spaces_do_not_overlap(identity_settings: Settings) -> None:
    """An extractor credential is not a way to read files, and a user's is not a way to register.

    The separation is structural — two tables, two lookups — and this is the test that says so.
    """
    async with instance(identity_settings) as app, signed_in(app) as admin:
        extractor_token = (await provision(admin))["token"]
        minted = await admin.post(
            f"{API_V1_PREFIX}/auth/tokens",
            json={"name": "for-a-script", "scope": "full"},
            headers=SAME_ORIGIN,
        )
        assert minted.status_code == 201, minted.text
        user_token = minted.json()["token"]

        async with as_extractor(app, extractor_token) as extractor:
            reached_user_api = await extractor.get(f"{API_V1_PREFIX}/workspaces")
        async with as_extractor(app, user_token) as impostor:
            reached_extractor_api = await impostor.put(REGISTRATION, json=manifest())

        assert reached_user_api.status_code == 401, reached_user_api.text
        assert reached_extractor_api.status_code == 401, reached_extractor_api.text


async def test_an_unknown_credential_is_refused(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await provision(admin)

        async with as_extractor(app, f"{EXTRACTOR_TOKEN_PREFIX}not-a-real-credential") as caller:
            unknown = await caller.put(REGISTRATION, json=manifest())

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as anonymous:
            missing = await anonymous.put(REGISTRATION, json=manifest())
            malformed = await anonymous.put(
                REGISTRATION, json=manifest(), headers={"Authorization": "Basic whatever"}
            )

        assert unknown.status_code == 401
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert malformed.status_code == 401


# -------------------------------------------------------------------------- enable/disable


async def test_disabling_an_extractor_does_not_silence_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """`enabled` means exactly one thing — whether work is routed here. A disabled extractor
    still registers, so re-enabling it needs no restart."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        token = (await provision(admin))["token"]

        disabled = await admin.patch(
            f"{EXTRACTORS}/pdf-text", json={"enabled": False}, headers=SAME_ORIGIN
        )
        async with as_extractor(app, token) as extractor:
            registered = await extractor.put(REGISTRATION, json=manifest())
        enabled = await admin.patch(
            f"{EXTRACTORS}/pdf-text", json={"enabled": True}, headers=SAME_ORIGIN
        )

        assert disabled.json()["enabled"] is False
        assert registered.status_code == 200, registered.text
        assert registered.json()["enabled"] is False
        assert enabled.json()["enabled"] is True

        assert len(await read_events(identity_database, action="extractor.disabled")) == 1
        assert len(await read_events(identity_database, action="extractor.enabled")) == 1


async def test_setting_the_state_it_already_has_records_nothing(
    identity_settings: Settings, identity_database: str
) -> None:
    """ "Who turned OCR off" must not be buried under a client that resends its state."""
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await provision(admin)

        for _ in range(3):
            response = await admin.patch(
                f"{EXTRACTORS}/pdf-text", json={"enabled": True}, headers=SAME_ORIGIN
            )
            assert response.status_code == 200, response.text

        assert await read_events(identity_database, action="extractor.enabled") == []


async def test_an_absent_extractor_is_a_not_found_everywhere(identity_settings: Settings) -> None:
    async with instance(identity_settings) as app, signed_in(app) as admin:
        assert (await admin.get(f"{EXTRACTORS}/pdf-text")).status_code == 404
        assert (
            await admin.patch(
                f"{EXTRACTORS}/pdf-text", json={"enabled": False}, headers=SAME_ORIGIN
            )
        ).status_code == 404
        assert (await admin.get(f"{EXTRACTORS}/pdf-text/tokens")).status_code == 404
        assert (
            await admin.post(
                f"{EXTRACTORS}/pdf-text/tokens", json={"name": "x"}, headers=SAME_ORIGIN
            )
        ).status_code == 404
