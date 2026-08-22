"""The upload protocol end to end: creation, resumption, refusal, cancellation.

ADR-0017 says it plainly — the public tus conformance tester cannot validate this draft, so
this suite is the only thing keeping the dialect honest. It therefore asserts the wire
(status codes and headers) as well as the outcome (bytes on disk, rows in the database),
because a client we did not write cares about the first and the user cares about the second.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import janitor, names, operations, resumable, uploads, workspacefs
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.runner import Job
from store_everything.tables import file, upload_session
from tests.identity_helpers import read_events
from tests.upload_helpers import (
    append,
    cancel,
    create_upload,
    files_url,
    offset_of,
    upload_url,
)
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    create_member,
    create_workspace,
    instance,
    provision_pending,
    signed_in,
    workspace_ready,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CONTENT = b"the quick brown fox jumps over the lazy dog" * 32
DIGEST = hashlib.sha256(CONTENT).hexdigest()


async def count_files(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(select(func.count()).select_from(file))).scalar_one()
    finally:
        await engine.dispose()


async def expire(database_url: str, upload_id: UUID) -> None:
    """Push a session's deadline into the past, as a week of silence would."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                update(upload_session)
                .where(upload_session.c.id == upload_id)
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1), state="expired")
            )
            await connection.commit()
    finally:
        await engine.dispose()


# ------------------------------------------------------------------------ the wire


@pytest.mark.fr("F-001/FR-14")
async def test_options_advertises_the_limits(
    identity_settings: Settings, identity_database: str
) -> None:
    """A server without the protocol answers `501` here, so `200` + `Upload-Limit` is the
    signal that resumable uploads exist (ADR-0017)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        response = await client.request("OPTIONS", files_url(workspace))

    assert response.status_code == 200
    limit = response.headers[resumable.LIMIT_HEADER]
    assert "max-append-size=" in limit
    assert "min-append-size=" in limit
    assert "max-age=" in limit


@pytest.mark.fr("F-001/FR-1")
async def test_a_one_request_upload_stores_the_file(
    identity_settings: Settings, identity_database: str
) -> None:
    """The ordinary case: a small file pays no extra round trip."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        response = await create_upload(client, workspace, "notes.txt", body=CONTENT)

    assert response.status_code == 201, response.text
    assert response.headers[resumable.COMPLETE_HEADER] == "?1"
    body = response.json()
    assert response.headers["Location"] == f"/files/{body['id']}"
    assert body["path"] == "notes.txt"
    assert body["size"] == len(CONTENT)
    assert body["content_hash"] == DIGEST
    assert body["media_class"] == "document"

    # The bytes are at the real path, unmodified (ADR-0003).
    assert (root / "notes.txt").read_bytes() == CONTENT
    created = await read_events(identity_database, action="file.created")
    assert [event["details"]["path"] for event in created] == ["notes.txt"]


@pytest.mark.fr("F-001/FR-14")
async def test_a_client_that_does_not_speak_the_protocol_still_uploads(
    identity_settings: Settings, identity_database: str
) -> None:
    """ADR-0017's fallback: no interop version and no `Upload-Complete` is an ordinary
    upload, not an error — which is what keeps a future iOS release from breaking."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        response = await create_upload(
            client, workspace, "plain.bin", body=CONTENT, complete=None, interop=None
        )

    assert response.status_code == 201, response.text
    assert (root / "plain.bin").read_bytes() == CONTENT


@pytest.mark.fr("F-001/FR-2", "F-001/FR-14")
async def test_an_upload_is_created_appended_to_and_finished(
    identity_settings: Settings, identity_database: str
) -> None:
    first, second = CONTENT[:100], CONTENT[100:]

    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(
            client, workspace, "video.mp4", complete=False, length=len(CONTENT)
        )
        assert created.status_code == 201, created.text
        assert created.headers[resumable.COMPLETE_HEADER] == "?0"
        assert created.headers[resumable.OFFSET_HEADER] == "0"
        upload_id = created.json()["id"]
        assert created.headers["Location"] == upload_url(upload_id).removeprefix(API_V1_PREFIX)

        # While the upload is in flight its bytes live in the workspace's own staging area,
        # on the destination's filesystem so that finalizing is a rename (ADR-0018).
        partial = await append(client, upload_id, 0, first)
        assert partial.status_code == 204
        assert partial.headers[resumable.OFFSET_HEADER] == str(len(first))
        assert list(workspacefs.staging_directory(root).iterdir())

        probe = await offset_of(client, upload_id)
        assert probe.status_code == 204
        assert probe.headers[resumable.OFFSET_HEADER] == str(len(first))
        assert probe.headers[resumable.LENGTH_HEADER] == str(len(CONTENT))
        assert probe.headers["Cache-Control"] == "no-store"

        finished = await append(client, upload_id, len(first), second, complete=True)

    assert finished.status_code == 200, finished.text
    assert finished.headers[resumable.COMPLETE_HEADER] == "?1"
    assert finished.json()["content_hash"] == DIGEST
    assert (root / "video.mp4").read_bytes() == CONTENT
    # Nothing is left staged once the rename has happened.
    assert list(workspacefs.staging_directory(root).iterdir()) == []


@pytest.mark.fr("F-001/FR-2", "F-001/FR-14")
async def test_an_append_at_a_stale_offset_is_refused_without_corrupting_anything(
    identity_settings: Settings, identity_database: str
) -> None:
    """F-001/AC-1: the `409` carries the offset to resume from, and the retry still yields
    the source's exact bytes."""
    first, second = CONTENT[:100], CONTENT[100:]

    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = created.json()["id"]
        await append(client, upload_id, 0, first)

        stale = await append(client, upload_id, 0, first)
        assert stale.status_code == 409
        assert stale.headers[resumable.OFFSET_HEADER] == str(len(first))
        assert stale.json()["type"] == resumable.OFFSET_MISMATCH_TYPE

        resumed = await append(client, upload_id, len(first), second, complete=True)

    assert resumed.status_code == 200, resumed.text
    assert (root / "video.mp4").read_bytes() == CONTENT
    assert resumed.json()["content_hash"] == DIGEST


@pytest.mark.fr("F-001/FR-15")
async def test_staging_that_no_longer_covers_the_offset_ends_the_session(
    identity_settings: Settings, identity_database: str
) -> None:
    """An acknowledged offset is never wrong — so when it cannot be honoured, nothing is assembled.

    Staging lives in the user-visible `.workspace/staging/`, so a client with SMB access can
    truncate it. The append path opened it with `"ab"` (creating an empty file if it had to) and
    committed the file's *size* as the new offset, so the offset walked backwards and the next
    chunk was stored at the wrong position: a self-consistent file nobody uploaded.
    """
    first, second = CONTENT[:100], CONTENT[100:]

    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = created.json()["id"]
        await append(client, upload_id, 0, first)

        # What a user poking around in the staging directory over SMB leaves behind.
        staging = workspacefs.staging_directory(root) / f"{upload_id}.partial"
        assert staging.stat().st_size == len(first)
        staging.write_bytes(first[:10])

        refused = await append(client, upload_id, len(first), second, complete=True)
        # And the session is over: resuming it would assemble the same wrong file.
        again = await append(client, upload_id, len(first), second, complete=True)

    assert refused.status_code == 410, refused.text
    assert "cannot be resumed" in refused.json()["detail"]
    assert again.status_code == 404
    assert not (root / "video.mp4").exists()
    assert await count_files(identity_database) == 0


@pytest.mark.fr("F-001/FR-20", "F-001/FR-7")
async def test_two_uploads_finishing_on_one_path_do_not_destroy_each_other(
    identity_settings: Settings, identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing is a check-then-act over two systems, so it runs one publisher at a time.

    Both finalizes look for what is at the path before either has committed, so both used to
    find it free — and the loser's `os.replace` destroyed the winner's bytes with no snapshot and
    no version, before the unique index refused its rows a moment too late to matter.
    """
    mine, yours = CONTENT, CONTENT[::-1]
    # Both publishers held at the instant before the rename, so the test does not depend on
    # winning a race: whichever guard is missing, this is where the damage would be done. The
    # timeout is what makes it terminate when serialisation works and the second never arrives.
    rendezvous = threading.Barrier(2)
    real_assemble = uploads.assemble

    def waiting_assemble(*args: Any, **kwargs: Any) -> uploads.Assembled:
        with contextlib.suppress(threading.BrokenBarrierError):
            rendezvous.wait(timeout=0.5)
        return real_assemble(*args, **kwargs)

    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        first = await create_upload(client, workspace, "contested.bin", complete=False)
        second = await create_upload(client, workspace, "contested.bin", complete=False)
        assert first.status_code == 201 and second.status_code == 201
        one, two = first.json()["id"], second.json()["id"]
        await append(client, one, 0, mine[:10])
        await append(client, two, 0, yours[:10])

        monkeypatch.setattr(uploads, "assemble", waiting_assemble)
        finished = await asyncio.gather(
            append(client, one, 10, mine[10:], complete=True),
            append(client, two, 10, yours[10:], complete=True),
        )

    statuses = sorted(response.status_code for response in finished)
    assert statuses == [200, 409], [response.text for response in finished]
    # Exactly one file, and the bytes on disk are the ones its row describes.
    assert await count_files(identity_database) == 1
    winner = next(response for response in finished if response.status_code == 200)
    stored = (root / "contested.bin").read_bytes()
    assert hashlib.sha256(stored).hexdigest() == winner.json()["content_hash"]
    assert stored in {mine, yours}


@pytest.mark.fr("F-001/FR-14")
async def test_an_append_must_declare_the_partial_upload_media_type(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = created.json()["id"]

        wrong = await append(client, upload_id, 0, b"data", content_type="application/json")
        missing = await append(client, upload_id, None, b"data")

    assert wrong.status_code == 415
    assert missing.status_code == 400


@pytest.mark.fr("F-001/FR-14")
async def test_cancelling_discards_the_staged_bytes(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = created.json()["id"]
        await append(client, upload_id, 0, CONTENT[:100])

        cancelled = await cancel(client, upload_id)
        again = await cancel(client, upload_id)
        gone = await offset_of(client, upload_id)

    assert cancelled.status_code == 204
    # `DELETE` is idempotent (08 § idempotency), so a retry is not an error.
    assert again.status_code == 204
    assert gone.status_code == 404
    assert list(workspacefs.staging_directory(root).iterdir()) == []
    assert not (root / "video.mp4").exists()


async def test_an_expired_upload_answers_gone(
    identity_settings: Settings, identity_database: str
) -> None:
    """`410` rather than `404`: "it expired" tells a client to start over, while "never
    existed" tells it to check its bookkeeping."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = UUID(created.json()["id"])
        await expire(identity_database, upload_id)

        probe = await offset_of(client, upload_id)
        appended = await append(client, upload_id, 0, CONTENT)

    assert probe.status_code == 410
    assert appended.status_code == 410


async def test_a_finished_upload_replays_its_outcome(
    identity_settings: Settings, identity_database: str
) -> None:
    """The lost-response case (08 § idempotency): repeating the final append returns the same
    file rather than registering a second one."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(client, workspace, "notes.txt", complete=False)
        upload_id = created.json()["id"]
        finished = await append(client, upload_id, 0, CONTENT, complete=True)
        replayed = await append(client, upload_id, len(CONTENT), CONTENT, complete=True)

    assert finished.status_code == 200
    assert replayed.status_code == 200
    assert replayed.json()["id"] == finished.json()["id"]
    assert await count_files(identity_database) == 1


# ------------------------------------------------------------------------- integrity


@pytest.mark.fr("F-001/FR-1")
async def test_a_declared_hash_is_verified(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        response = await create_upload(
            client, workspace, "notes.txt", body=CONTENT, content_hash=DIGEST
        )

    assert response.status_code == 201, response.text
    assert (root / "notes.txt").read_bytes() == CONTENT


async def test_a_mismatched_hash_publishes_nothing(
    identity_settings: Settings, identity_database: str
) -> None:
    """The protocol carries no integrity digest, so this check is the only thing between a
    corrupted transfer and a file the user will trust (ADR-0017)."""
    wrong = hashlib.sha256(b"something else").hexdigest()

    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        response = await create_upload(
            client, workspace, "notes.txt", body=CONTENT, content_hash=wrong
        )

    assert response.status_code == 422
    assert not (root / "notes.txt").exists()
    assert await count_files(identity_database) == 0


async def test_a_malformed_hash_is_refused_before_any_bytes_move(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        response = await create_upload(client, workspace, "notes.txt", content_hash="not-a-hash")

    assert response.status_code == 422


# ---------------------------------------------------------------------------- paths


@pytest.mark.fr("F-001/FR-7")
async def test_a_collision_is_refused_on_the_comparison_key(
    identity_settings: Settings, identity_database: str
) -> None:
    """`Report.pdf` collides with `report.pdf`, which is the whole point of the key."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        first = await create_upload(client, workspace, "report.pdf", body=CONTENT)
        same = await create_upload(client, workspace, "report.pdf", body=CONTENT)
        cased = await create_upload(client, workspace, "Report.pdf", body=CONTENT)

    assert first.status_code == 201
    assert same.status_code == 409
    assert cased.status_code == 409


@pytest.mark.fr("F-001/FR-20")
async def test_an_unregistered_file_on_the_storage_is_never_overwritten(
    identity_settings: Settings, identity_database: str
) -> None:
    """A file hand-copied onto the NAS since the last scan is content the app has never seen.

    The collision checks consult registered rows, so this path — the one writer that publishes
    over a destination — could destroy it with no snapshot, no version and no trash entry to
    recover from. `move_entry` and `folders.create` both refuse exactly this state; ADR-0019's
    rule is report, never repair, and the window is up to a whole scan interval wide.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        arrived = root / "arrived-by-hand.txt"
        arrived.write_bytes(b"copied straight onto the share")

        response = await create_upload(client, workspace, "arrived-by-hand.txt", body=CONTENT)

    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("Something is already on the storage")
    assert arrived.read_bytes() == b"copied straight onto the share", "the bytes were replaced"
    assert await count_files(identity_database) == 0


async def test_a_nested_path_creates_its_folders(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        response = await create_upload(
            client, workspace, "Photos/2026/summer/beach.jpg", body=CONTENT
        )

    assert response.status_code == 201, response.text
    assert response.json()["path"] == "Photos/2026/summer/beach.jpg"
    assert (root / "Photos" / "2026" / "summer" / "beach.jpg").read_bytes() == CONTENT
    # The folders are real rows too, so a listing will find them (F-015/FR-1). The root is the
    # first, with the empty name it really has.
    created = await read_events(identity_database, action="folder.created")
    assert [event["details"]["name"] for event in created] == ["", "Photos", "2026", "summer"]


async def test_a_failed_upload_creates_no_folders(
    identity_settings: Settings, identity_database: str
) -> None:
    """Directories are made at finalize, not at creation, so an abandoned upload leaves
    nothing behind in the user's tree."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "Photos/2026/beach.jpg", complete=False)
        await append(client, UUID(created.json()["id"]), 0, CONTENT[:10])

    assert not (root / "Photos").exists()


async def test_a_name_cannot_be_a_file_and_a_folder_at_once(
    identity_settings: Settings, identity_database: str
) -> None:
    """A directory entry is one or the other. Two tables cannot say so as one constraint, and
    on disk it would surface as a mid-upload `FileExistsError`."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        await create_upload(client, workspace, "Photos/beach.jpg", body=CONTENT)

        over_folder = await create_upload(client, workspace, "Photos", body=CONTENT)
        under_file = await create_upload(client, workspace, "Photos/beach.jpg/nested", body=CONTENT)

    assert over_folder.status_code == 409
    assert under_file.status_code == 409


@pytest.mark.parametrize("path", ["../escape", ".workspace/marker", "a//b", "/absolute"])
@pytest.mark.fr("F-001/FR-13")
async def test_a_path_outside_the_policy_is_refused(
    identity_settings: Settings, identity_database: str, path: str
) -> None:
    """Including the control directory: `.workspace` is reserved at a workspace root, so no
    upload can write into it or shadow it (ADR-0018)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        response = await create_upload(client, workspace, path, body=CONTENT)

    assert response.status_code == 422
    assert response.json()["errors"][0]["pointer"] == "/query/path"


async def test_the_control_directory_is_never_a_target(
    identity_settings: Settings, identity_database: str
) -> None:
    """F-001/FR-13 from the other side: the marker survives an upload aimed at it."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        before = workspacefs.marker_path(root).read_bytes()
        response = await create_upload(
            client, workspace, f"{names.CONTROL_DIRECTORY}/marker", body=b"overwritten"
        )

    assert response.status_code == 422
    assert workspacefs.marker_path(root).read_bytes() == before


# ---------------------------------------------------------------------------- limits


async def test_a_body_over_the_append_limit_is_refused(
    identity_settings: Settings, identity_database: str
) -> None:
    """The number is published in `Upload-Limit`, so this is a negotiated refusal rather than
    a mystery failure (ADR-0017)."""
    async with workspace_ready(identity_settings, identity_database, upload_max_append_size=32) as (
        client,
        workspace,
        _,
    ):
        response = await create_upload(client, workspace, "notes.txt", body=CONTENT)

    assert response.status_code == 413


async def test_an_upload_over_the_instance_limit_is_refused(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database, upload_max_size=16) as (
        client,
        workspace,
        root,
    ):
        declared = await create_upload(
            client, workspace, "notes.txt", complete=False, length=len(CONTENT)
        )
        undeclared = await create_upload(client, workspace, "other.txt", body=CONTENT)

    # Refused on the declared length before a byte is written, and on the real size after.
    assert declared.status_code == 413
    assert undeclared.status_code == 413
    assert not (root / "other.txt").exists()


async def test_a_large_append_is_not_charged_to_the_request_ceiling(
    identity_settings: Settings, identity_database: str
) -> None:
    """What the ceiling rations is per-request overhead, not throughput: an append at or over
    `min-append-size` is exempt, so a fast link uploading a big file cannot rate-limit itself
    (07 § abuse protection)."""
    async with workspace_ready(
        identity_settings, identity_database, rate_limit_per_minute=4, upload_min_append_size=8
    ) as (client, workspace, root):
        created = await create_upload(client, workspace, "notes.txt", complete=False)
        assert created.status_code == 201, created.text
        upload_id = UUID(created.json()["id"])
        await exhaust_the_ceiling(client, upload_id)

        appended = await append(client, upload_id, 0, CONTENT, complete=True)

    assert appended.status_code == 200, appended.text
    assert (root / "notes.txt").read_bytes() == CONTENT


async def test_a_small_append_spends_the_ordinary_budget(
    identity_settings: Settings, identity_database: str
) -> None:
    """The other half of the rule, and the reason the exemption is safe: kilobyte appends —
    the shape that would otherwise be an `fsync` storm — are counted like any request."""
    async with workspace_ready(
        identity_settings, identity_database, rate_limit_per_minute=4, upload_min_append_size=1024
    ) as (client, workspace, _):
        created = await create_upload(client, workspace, "notes.txt", complete=False)
        assert created.status_code == 201, created.text
        upload_id = UUID(created.json()["id"])
        await exhaust_the_ceiling(client, upload_id)

        refused = await append(client, upload_id, 0, b"tiny")

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "60"


async def exhaust_the_ceiling(client: httpx.AsyncClient, upload_id: UUID) -> None:
    """Spend the request budget with ordinary requests, so what follows is about the append.

    Self-calibrating on purpose: counting the requests a fixture happens to make would make
    these tests fail the next time a helper gains a round trip.
    """
    for _ in range(10):
        if (await offset_of(client, upload_id)).status_code == 429:
            return
    pytest.fail("the request ceiling never engaged")


# ------------------------------------------------------------------------ ownership


async def test_another_users_workspace_is_not_found(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app:
        async with signed_in(app) as admin:
            await create_member(admin)
            created = await create_workspace(admin, "Mine")
            workspace = UUID(created.json()["id"])
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            response = await create_upload(member, workspace, "notes.txt", body=CONTENT)
            options = await member.request("OPTIONS", files_url(workspace))

    assert response.status_code == 404
    assert options.status_code == 404


async def test_another_users_upload_session_is_not_found(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app:
        async with signed_in(app) as admin:
            await create_member(admin)
            created = await create_workspace(admin, "Mine")
            await provision_pending(identity_database)
            started = await create_upload(
                admin, UUID(created.json()["id"]), "x.txt", complete=False
            )
            upload_id = started.json()["id"]
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            probe = await offset_of(member, upload_id)
            appended = await append(member, upload_id, 0, CONTENT)
            cancelled = await cancel(member, upload_id)

    assert (probe.status_code, appended.status_code, cancelled.status_code) == (404, 404, 404)


async def test_an_unprovisioned_workspace_cannot_take_files(
    identity_settings: Settings, identity_database: str
) -> None:
    """The row exists before the directory does (ADR-0010), and an upload must not race that."""
    async with instance(identity_settings) as app, signed_in(app) as client:
        created = await create_workspace(client, "Fresh")
        response = await create_upload(
            client, UUID(created.json()["id"]), "notes.txt", body=CONTENT
        )

    assert response.status_code == 409
    assert "provision" in response.json()["detail"]


async def test_an_unknown_upload_is_not_found(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, _, _root):
        response = await offset_of(client, uuid4())

    assert response.status_code == 404


async def test_uploading_requires_the_same_origin(
    identity_settings: Settings, identity_database: str
) -> None:
    """The session cookie is ambient authority, so a state-changing request must prove where
    it came from (07 § tokens & credentials)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        response = await client.post(
            files_url(workspace), params={"path": "notes.txt"}, content=CONTENT
        )

    assert response.status_code == 403


async def test_the_staged_bytes_never_leave_the_workspace(
    identity_settings: Settings, identity_database: str
) -> None:
    """Staging shares the destination's filesystem, which is why it lives in the tree at all
    (ADR-0018): the commit has to be a rename, not a copy."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "notes.txt", complete=False)
        await append(client, UUID(created.json()["id"]), 0, CONTENT[:20])

        staged = list(workspacefs.staging_directory(root).iterdir())

    assert len(staged) == 1
    assert staged[0].name.startswith(created.json()["id"])
    assert Path(root) in staged[0].parents


# --------------------------------------------------------------------------- debris


async def lapse(database_url: str, upload_id: UUID) -> None:
    """Move a session's deadline into the past without touching its state — what a week of
    silence looks like just before the janitor notices."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                update(upload_session)
                .where(upload_session.c.id == upload_id)
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            await connection.commit()
    finally:
        await engine.dispose()


async def sweep(database_url: str, settings: Settings) -> dict[str, Any]:
    """Run the janitor the way the worker does: claimed, leased, committed with its result."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await operations.enqueue(connection, kind=janitor.KIND, max_attempts=3)
            await connection.commit()
            claimed = await operations.claim(
                connection,
                worker="test/janitor",
                lease=timedelta(minutes=5),
                kinds=(janitor.KIND,),
            )
            assert claimed is not None
            result = await janitor.collect(
                Job(operation=claimed, connection=connection), settings=settings
            )
            await operations.succeed(connection, claimed=claimed, result=result)
            await connection.commit()
            return result
    finally:
        await engine.dispose()


def age(path: Path) -> None:
    """Back-date a file past any grace window used here, so age is never why a test passes."""
    long_ago = (datetime.now(UTC) - timedelta(days=7)).timestamp()
    os.utime(path, (long_ago, long_ago))


async def test_an_open_uploads_staged_bytes_survive_the_janitor(
    identity_settings: Settings, identity_database: str
) -> None:
    """A paused upload has days to be resumed (ADR-0017), so its staging is live data however
    old the file looks. Collecting it would silently destroy a multi-gigabyte transfer."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = UUID(created.json()["id"])
        await append(client, upload_id, 0, CONTENT[:100])
        staged = next(iter(workspacefs.staging_directory(root).iterdir()))
        age(staged)

        result = await sweep(identity_database, identity_settings)

        assert staged.exists(), "the janitor collected an upload that is still resumable"
        assert result["sessions_expired"] == 0
        assert result["staging_collected"] == 0

        # And it is still resumable in fact, not just on disk.
        finished = await append(client, upload_id, 100, CONTENT[100:], complete=True)

    assert finished.status_code == 200, finished.text
    assert (root / "video.mp4").read_bytes() == CONTENT


async def test_an_expired_uploads_staged_bytes_are_collected(
    identity_settings: Settings, identity_database: str
) -> None:
    """The other side of the same rule: once the deadline passes, the bytes are debris."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = UUID(created.json()["id"])
        await append(client, upload_id, 0, CONTENT[:100])
        staged = next(iter(workspacefs.staging_directory(root).iterdir()))
        age(staged)
        await lapse(identity_database, upload_id)

        result = await sweep(identity_database, identity_settings)
        gone = await offset_of(client, upload_id)

    assert result["sessions_expired"] == 1
    assert result["staging_collected"] == 1
    assert not staged.exists()
    assert gone.status_code == 410


@pytest.mark.fr("F-001/FR-15")
async def test_unacknowledged_bytes_are_discarded_when_an_upload_resumes(
    identity_settings: Settings, identity_database: str
) -> None:
    """The crash window, exercised through the real endpoints.

    A crash between the `fsync` and the offset's commit leaves bytes on disk that the client
    was never told about. The resume must discard them rather than build them into the file —
    otherwise the assembled content is longer than what was sent, and the hash says so.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, root):
        created = await create_upload(client, workspace, "video.mp4", complete=False)
        upload_id = UUID(created.json()["id"])
        await append(client, upload_id, 0, CONTENT[:100])

        # Exactly what a power cut after the fsync leaves behind: durable bytes, no offset.
        staged = next(iter(workspacefs.staging_directory(root).iterdir()))
        with staged.open("ab") as handle:
            handle.write(b"bytes nobody acknowledged")

        probe = await offset_of(client, upload_id)
        assert probe.headers[resumable.OFFSET_HEADER] == "100"
        finished = await append(client, upload_id, 100, CONTENT[100:], complete=True)

    assert finished.status_code == 200, finished.text
    assert finished.json()["content_hash"] == DIGEST
    assert (root / "video.mp4").read_bytes() == CONTENT
