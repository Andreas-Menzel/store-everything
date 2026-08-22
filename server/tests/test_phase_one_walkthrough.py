"""The phase-1 exit criterion, walked end to end in one test.

Every step here is covered in detail elsewhere — uploads in `test_uploads_api.py`, scanning in
`test_scanning.py`, reconciliation in `test_reconciliation.py`, folders in `test_folders_api.py`.
This exists because the [ROADMAP](../../ROADMAP.md)'s phase-1 exit criterion is not a list of
features but a *path*:

    create user → log in → create workspace → resumable upload (interrupt + resume)
    → import an existing subtree → re-scan picks up an external add, modify and delete
    → browse folders → download bytes (Range supported)

A suite of green units can all pass while the path is broken between them: a session that does
not survive the next request, an upload whose file is invisible to the listing that should show
it, a rescan that needs a restart to notice anything. So the walk is asserted as a walk, in
order, on one instance — and it is deliberately readable top to bottom, because its other job is
to be the answer to "what does this thing actually do yet?".
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from store_everything import resumable
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.upload_helpers import append, create_upload, offset_of, upload_url
from tests.workspace_helpers import (
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    create_member,
    create_workspace,
    instance,
    provision_pending,
    scan_pending,
    signed_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Big enough to need more than one request, small enough to stay a fast test. The interruption
#: is what matters, not the size — the resume path is identical at 8 MB and at 8 GB.
VIDEO = bytes(range(256)) * 400
VIDEO_DIGEST = hashlib.sha256(VIDEO).hexdigest()

#: The subtree that already exists on the storage before the app ever sees it.
EXISTING: dict[str, bytes] = {
    "2019/january/receipt.pdf": b"%PDF-1.7 an old receipt",
    "2019/january/notes.txt": b"a note from january",
    "2020/summary.txt": b"the year in one file",
}


def build(root: Path, tree: dict[str, bytes]) -> None:
    for relative, content in tree.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


async def children(client: httpx.AsyncClient, folder_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"{API_V1_PREFIX}/folders/{folder_id}/children")
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


async def descend(client: httpx.AsyncClient, folder_id: str, *names: str) -> str:
    """Walk down by name, the way a person clicking through the tree does."""
    for name in names:
        entries = await children(client, folder_id)
        matched = [entry for entry in entries if entry["name"] == name]
        assert matched, f"no {name!r} among {[entry['name'] for entry in entries]}"
        folder_id = str(matched[0]["id"])
    return folder_id


async def test_the_phase_one_path_works_from_one_end_to_the_other(
    identity_settings: Settings, identity_database: str, tmp_path: Path
) -> None:
    existing = tmp_path / "nas" / "archive"
    build(existing, EXISTING)

    async with instance(identity_settings, adoption_roots=(existing,)) as app:
        # ---------------------------------------------------------------- create a user
        async with signed_in(app) as admin:
            member_id = await create_member(admin)

            # Adoption is admin-gated (F-001/FR-10), so the existing subtree is brought in by
            # the administrator and handed to the person who will own it.
            adopted = await create_workspace(
                admin, "The archive", adopt_path=existing, owner=member_id
            )
            assert adopted.status_code == 201, adopted.text
            archive = str(adopted.json()["id"])

        # ---------------------------------------------------------------- log in
        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            who = await member.get(f"{API_V1_PREFIX}/auth/me")
            assert who.status_code == 200
            assert who.json()["user"]["email"] == MEMBER_EMAIL

            # ------------------------------------------------------------ create a workspace
            created = await create_workspace(member, "Videos")
            assert created.status_code == 201, created.text
            videos = str(created.json()["id"])
            await provision_pending(identity_database)

            # ------------------------------------------------------------ resumable upload
            # First chunk, deliberately declared incomplete: this is the shape a client uses
            # when it cannot promise to finish in one request (ADR-0017).
            half = len(VIDEO) // 2
            opened = await create_upload(
                member, UUID(videos), "holiday.bin", body=VIDEO[:half], complete=False
            )
            assert opened.status_code == 201, opened.text
            upload_id = opened.headers["location"].rsplit("/", 1)[-1]

            # ------------------------------------------------------------ …interrupted
            # The connection dies here. Everything the client remembered is gone, so the only
            # trustworthy answer to "how far did it get?" is the server's own.
            resumed = await offset_of(member, upload_id)
            assert resumed.status_code == 204
            offset = int(resumed.headers["upload-offset"])
            assert offset == half, "the server committed exactly what it acknowledged"

            # ------------------------------------------------------------ …and resumed
            finished = await append(member, upload_id, offset, VIDEO[offset:], complete=True)
            assert finished.status_code == 200, finished.text
            uploaded = finished.headers["location"].rsplit("/", 1)[-1]

            # The upload resource is spent; the file is what remains.
            gone = await member.get(upload_url(upload_id))
            assert gone.status_code in {404, 405}

            stored = await member.get(f"{API_V1_PREFIX}/files/{uploaded}")
            assert stored.status_code == 200, stored.text
            assert stored.json()["content_hash"] == VIDEO_DIGEST
            assert stored.json()["size"] == len(VIDEO)
            # Written to the real path on the real storage, not into a private store (FR-1).
            assert (Path(created.json()["root_path"]) / "holiday.bin").read_bytes() == VIDEO

            # ------------------------------------------------------------ import the subtree
            await provision_pending(identity_database)
            await scan_pending(identity_database, identity_settings)

            archived = await member.get(f"{API_V1_PREFIX}/workspaces/{archive}")
            assert archived.status_code == 200, archived.text
            root = str(archived.json()["root_folder"])

            # Every file registered, and the tree untouched: same bytes, same paths.
            registered = {
                str(row["details"]["path"])
                for row in await read_events(identity_database, action="file.created")
            }
            assert registered == set(EXISTING) | {"holiday.bin"}
            for relative, content in EXISTING.items():
                assert (existing / relative).read_bytes() == content

            # ------------------------------------------------------------ external changes
            (existing / "2020" / "added-by-hand.txt").write_bytes(b"copied straight onto the NAS")
            (existing / "2020" / "summary.txt").write_bytes(b"rewritten outside the app")
            (existing / "2019" / "january" / "notes.txt").unlink()

            requested = await member.post(
                f"{API_V1_PREFIX}/workspaces/{archive}/rescan", json={}, headers=SAME_ORIGIN
            )
            assert requested.status_code == 202, requested.text
            await scan_pending(identity_database, identity_settings)

            # The addition is registered…
            appeared = {
                str(row["details"]["path"])
                for row in await read_events(identity_database, action="file.created")
            }
            assert "2020/added-by-hand.txt" in appeared

            # …the modification is a new version, with the previous bytes preserved (F-007/FR-1)…
            versioned = await read_events(identity_database, action="file.version_created")
            assert [str(row["details"]["path"]) for row in versioned] == ["2020/summary.txt"]

            # …and the deletion is a trash entry, never a silent disappearance (F-001/FR-6).
            trashed = await read_events(identity_database, action="file.trashed")
            assert [str(row["details"]["path"]) for row in trashed] == ["2019/january/notes.txt"]

            # ------------------------------------------------------------ browse folders
            top = sorted(entry["name"] for entry in await children(member, root))
            assert top == ["2019", "2020"], "folders first, and only the two that exist"

            january = await descend(member, root, "2019", "january")
            remaining = await children(member, january)
            # The trashed file is in no default listing (02 § invariants #7).
            assert [entry["name"] for entry in remaining] == ["receipt.pdf"]
            receipt = str(remaining[0]["id"])

            # ------------------------------------------------------------ download the bytes
            whole = await member.get(f"{API_V1_PREFIX}/files/{receipt}/content")
            assert whole.status_code == 200
            assert whole.content == EXISTING["2019/january/receipt.pdf"]
            assert whole.headers["accept-ranges"] == "bytes"

            part = await member.get(
                f"{API_V1_PREFIX}/files/{receipt}/content", headers={"Range": "bytes=5-13"}
            )
            assert part.status_code == 206, part.text
            assert part.content == EXISTING["2019/january/receipt.pdf"][5:14]
            assert part.headers["content-range"] == (
                f"bytes 5-13/{len(EXISTING['2019/january/receipt.pdf'])}"
            )

            # And the protocol the whole walk rode on is still advertised, so a fresh client can
            # discover it without being told (F-001/FR-14).
            options = await member.request("OPTIONS", f"{API_V1_PREFIX}/workspaces/{videos}/files")
            assert options.status_code == 200
            assert "max-append-size=" in options.headers[resumable.LIMIT_HEADER]
