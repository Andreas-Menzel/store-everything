"""Tagging a file by hand: who said it, what everyone else sees, and what the log records.

[F-003](../../features/F-003-tagging.md) FR-1, FR-2 and FR-9. The interesting assertions are
not "the tag came back" but the ones about *shared state*: a tag lives on the file rather than
on the viewer, an edit from a second session wins by being later, and every change is in the
audit trail. The two-user half of that — Bob tagging Alice's file — needs grants, which arrive
with sharing in phase 4; phase 2's only permission is ownership, so the concurrency here is two
sessions of one account, which is a real client situation (a phone and a laptop) rather than a
stand-in.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from store_everything import files
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.tables import file_tag
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.tag_helpers import (
    TAGS,
    added,
    connected,
    file_tags_url,
    names_on_file,
    tag_file,
)
from tests.upload_helpers import create_upload
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


@pytest.mark.fr("F-003/FR-2")
async def test_a_file_carries_the_tags_it_is_given(
    identity_settings: Settings, identity_database: str
) -> None:
    """By id or by any spelling of the name, stamped with who applied it.

    Both ways in exist for two callers: a picker has the id from a completion, and a person
    typing has only the word — resolved through the synonym table, which is how `bill` lands on
    `invoice` rather than creating a second tag for it.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        me = (await client.get(f"{API_V1_PREFIX}/auth/me")).json()["user"]["id"]
        invoice = await added(client, "invoice", aliases=["bill"])
        await added(client, "scan")
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]

        by_id = await tag_file(client, file_id, tag=invoice)
        by_alias = await tag_file(client, file_id, name="BILL")
        by_name = await tag_file(client, file_id, name="scan")
        listed = await client.get(file_tags_url(file_id))
        summary = await client.get(f"{API_V1_PREFIX}/files/{file_id}")

    assert by_id.status_code == 201, by_id.text
    applied = by_id.json()
    assert applied["name"] == "invoice"
    assert applied["provenance"] == "manual"
    assert applied["status"] == "active"
    assert applied["user"] == me, "a tag says who put it there (F-003/FR-2)"

    # The synonym resolved to the tag it means rather than adding a second one.
    assert by_alias.status_code == 201
    assert by_alias.json()["id"] == str(invoice)
    assert by_name.status_code == 201

    assert [one["name"] for one in listed.json()] == ["invoice", "scan"]
    assert [one["name"] for one in summary.json()["tags"]] == ["invoice", "scan"]
    assert summary.json()["tags"][0]["provenance"] == "manual"


@pytest.mark.fr("F-003/FR-1")
async def test_a_file_carries_only_the_tag_it_was_given(
    identity_settings: Settings, identity_database: str
) -> None:
    """No ancestor is ever written onto a file — the flat-list half of FR-1.

    `tree` sits under `plant` under `nature`, and a forest photo tagged `tree` carries exactly
    one tag. Broad searches find it by expanding downward at query time (ADR-0006), which is why
    nothing here needs to touch the file.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        nature = await added(client, "nature")
        plant = await added(client, "plant", parents=[nature])
        tree = await added(client, "tree", parents=[plant])
        created = await create_upload(client, workspace, "Photos/forest.jpg", body=b"x")
        await tag_file(client, created.json()["id"], tag=tree)
        on_file = await names_on_file(client, created.json()["id"])

    assert on_file == ["tree"]


@pytest.mark.fr("F-003/FR-2")
async def test_applying_the_same_tag_twice_changes_nothing(
    identity_settings: Settings, identity_database: str
) -> None:
    """A POST that says what is already true is not an edit, and leaves no audit entry.

    Rewriting the row would also take the tag away from whoever applied it, which matters as
    soon as two people can write to one file.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        invoice = await added(client, "invoice")
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]
        first = await tag_file(client, file_id, tag=invoice)
        again = await tag_file(client, file_id, tag=invoice)
        listed = await client.get(file_tags_url(file_id))

    assert first.status_code == 201
    assert again.status_code == 201
    assert again.json() == first.json(), "the row that was already there, unchanged"
    assert len(listed.json()) == 1
    assert len(await read_events(identity_database, action="file.tagged")) == 1


@pytest.mark.fr("F-003/FR-2")
async def test_applying_a_tag_the_user_had_rejected_undoes_the_rejection(
    identity_settings: Settings, identity_database: str
) -> None:
    """A person changing their mind wins over their own earlier no.

    Rejection is a *negative record* that keeps machines from re-adding a tag (ADR-0004) — it is
    not a lock on the person who wrote it. The rejection here is inserted directly, because the
    act that produces one is removing a machine's claim, and machine claims arrive with the
    auto-tag path; what has to be true already is that applying the tag by hand flips the row
    rather than failing or duplicating it.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        invoice = await added(client, "invoice")
        me = (await client.get(f"{API_V1_PREFIX}/auth/me")).json()["user"]["id"]
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]

        async with connected(identity_database) as connection:
            await connection.execute(
                file_tag.insert().values(
                    file_id=UUID(file_id), tag_id=invoice, provenance="rejected", user_id=UUID(me)
                )
            )
        before = await names_on_file(client, file_id)
        applied = await tag_file(client, file_id, tag=invoice)
        after = await names_on_file(client, file_id)

    assert before == [], "a rejection is not a tag on the file"
    assert applied.status_code == 201, applied.text
    assert applied.json()["provenance"] == "manual"
    assert after == ["invoice"]

    recorded = await read_events(identity_database, action="file.tagged")
    assert recorded[0]["details"]["was"] == "rejected", "the log says what it changed from"


@pytest.mark.fr("F-003/FR-9")
async def test_two_sessions_edit_one_files_tags(
    identity_settings: Settings, identity_database: str
) -> None:
    """Tags are per-file shared state: last write wins, and the log carries every step.

    Both sessions read the same list — there is no per-viewer tag layer, by decision (F-003
    § out of scope). So the phone removing what the laptop added is a real edit both of them
    see, and the audit trail is where "why did my tag disappear" gets answered.
    """
    async with instance(identity_settings) as app, signed_in(app) as laptop:
        invoice = await added(laptop, "invoice")
        workspace = await create_workspace(laptop, "Photos")
        await provision_pending(identity_database)
        created = await create_upload(laptop, workspace.json()["id"], "Photos/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]

        async with signed_in(app) as phone:
            await tag_file(laptop, file_id, tag=invoice)
            # One shared truth: the other session sees the tag it did not add.
            seen_elsewhere = await names_on_file(phone, file_id)
            removed = await phone.delete(f"{file_tags_url(file_id)}/{invoice}", headers=SAME_ORIGIN)
            reapplied = await tag_file(laptop, file_id, tag=invoice)
            final_on_phone = await names_on_file(phone, file_id)
            final_on_laptop = await names_on_file(laptop, file_id)

    assert seen_elsewhere == ["invoice"]
    assert removed.status_code == 204, removed.text
    assert reapplied.status_code == 201
    assert final_on_phone == final_on_laptop == ["invoice"], "the last write is what stands"

    recorded = [
        (entry["action"], entry["details"].get("tag"))
        for entry in await read_events(identity_database)
        if entry["action"] in {"file.tagged", "file.untagged"}
    ]
    assert recorded == [
        ("file.tagged", "invoice"),
        ("file.untagged", "invoice"),
        ("file.tagged", "invoice"),
    ], "every change is in the log, in order (F-003/FR-9)"


@pytest.mark.fr("F-003/FR-2")
async def test_removing_a_tag_that_is_not_there_is_a_404(
    identity_settings: Settings, identity_database: str
) -> None:
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        invoice = await added(client, "invoice")
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]
        never_applied = await client.delete(
            f"{file_tags_url(file_id)}/{invoice}", headers=SAME_ORIGIN
        )
        no_such_tag = await client.delete(
            f"{file_tags_url(file_id)}/0198c0de-0000-7000-8000-000000000000", headers=SAME_ORIGIN
        )

    assert never_applied.status_code == 404, never_applied.text
    assert no_such_tag.status_code == 404


@pytest.mark.fr("F-003/FR-10")
async def test_a_word_that_is_not_vocabulary_is_refused(
    identity_settings: Settings, identity_database: str
) -> None:
    """Applying an unknown word does not create it — the vocabulary is admin-governed.

    This is the rule's user-visible edge: a member typing a word nobody approved gets told so,
    rather than quietly minting a tag the taxonomy never agreed to.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]
        unknown = await tag_file(client, file_id, name="whatever")
        # A name that normalizes to nothing means nothing — not "the empty tag".
        blank = await tag_file(client, file_id, name=" ")
        no_such_id = await tag_file(
            client, file_id, tag=UUID("0198c0de-0000-7000-8000-000000000000")
        )
        both = await client.post(
            file_tags_url(file_id),
            json={"name": "whatever", "tag": "0198c0de-0000-7000-8000-000000000000"},
            headers=SAME_ORIGIN,
        )
        neither = await client.post(file_tags_url(file_id), json={}, headers=SAME_ORIGIN)
        listed = await client.get(TAGS)

    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["errors"][0]["pointer"] == "/name"
    assert "administrator" in unknown.json()["errors"][0]["detail"]
    assert blank.status_code == 422
    assert no_such_id.status_code == 422
    assert both.status_code == 422
    assert neither.status_code == 422
    assert listed.json()["data"] == [], "nothing was created on the way"


async def test_a_tag_survives_a_new_version(
    identity_settings: Settings, identity_database: str
) -> None:
    """A tag describes the file, not its bytes, so a new upload onto the path keeps it.

    That is why curation is keyed by the file's UUID: replacing a scan with a better scan of the
    same invoice must not lose the word `invoice` (02 § file)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        invoice = await added(client, "invoice")
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"first")
        file_id = created.json()["id"]
        await tag_file(client, file_id, tag=invoice)

        replaced = await create_upload(
            client, workspace, "Photos/tax.pdf", body=b"second", if_exists="new_version"
        )
        assert replaced.status_code == 201, replaced.text
        assert replaced.json()["id"] == file_id
        after = await names_on_file(client, file_id)

    assert after == ["invoice"]


async def test_a_trashed_file_stops_counting_toward_usage(
    identity_settings: Settings, identity_database: str
) -> None:
    """Trashed items appear in no default surface, counts included (02 § invariants #7)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        invoice = await added(client, "invoice")
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]
        await tag_file(client, file_id, tag=invoice)
        before = await client.get(TAGS, params={"prefix": "inv"})

        async with connected(identity_database) as connection:
            await files.set_state(connection, file_id=created.json()["id"], state="trashed")
        after = await client.get(TAGS, params={"prefix": "inv"})

    assert before.json()["data"][0]["usage"] == {"files": 1, "folders": 0}
    assert after.json()["data"][0]["usage"] == {"files": 0, "folders": 0}


async def test_another_users_file_cannot_be_read_or_tagged(
    identity_settings: Settings, identity_database: str
) -> None:
    """Negative space: tags are not a way around file permissions.

    A file that is not the caller's answers `404` for its tags exactly as it does for itself —
    absent and forbidden are the same answer (08 § errors), and tagging it is refused the same
    way rather than with a different status that would confirm it exists.
    """
    async with instance(identity_settings) as app, signed_in(app) as admin:
        invoice = await added(admin, "invoice")
        await create_member(admin)
        mine = await create_workspace(admin, "Mine")
        await provision_pending(identity_database)
        created = await create_upload(admin, mine.json()["id"], "Mine/tax.pdf", body=b"pdf")
        file_id = created.json()["id"]
        await tag_file(admin, file_id, tag=invoice)

        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            read = await member.get(file_tags_url(file_id))
            tagged = await tag_file(member, file_id, tag=invoice)
            removed = await member.delete(
                f"{file_tags_url(file_id)}/{invoice}", headers=SAME_ORIGIN
            )
        still_there = await names_on_file(admin, file_id)

    assert read.status_code == 404, read.text
    assert tagged.status_code == 404
    assert removed.status_code == 404
    assert still_there == ["invoice"]
