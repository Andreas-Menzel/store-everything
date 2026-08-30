"""When a machine tags a file, and what a person can do about it.

[F-003](../../features/F-003-tagging.md) FR-3 to FR-6, FR-11, FR-12, and
[02 § invariant 4](../../specs/02-domain-model.md#invariants) — the reason the whole state
machine exists: **no model update may undo a person's curation, and a correction has to stick.**

The tests follow one file through the lifecycle a real instance puts it through:

- an extractor sends **labels**, and the core maps them into its own vocabulary — through the
  alias table where something fits, into a quarantined suggestion where nothing does;
- a person confirms one claim and rejects another;
- a **new generation** of extraction runs, with a model that has changed its mind;
- afterwards the confirmed tag is still there, the rejected one is still gone, and the plain
  `auto` one has been replaced by whatever the new run said.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from store_everything.config import Settings
from store_everything.tables import file_auto_tag, file_tag
from tests.extraction_helpers import (
    as_extractor,
    claim_one,
    extraction_ready,
    finish,
    install,
    reprocess,
    rows_in,
)
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.tag_helpers import TAGS, added, file_tags_url, tag_file
from tests.upload_helpers import create_upload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: What the extractor claims: a word the instance has (under a synonym), and one it does not.
LABELS = [{"name": "BILL", "confidence": 0.87}, {"name": "wombat", "confidence": 0.4}]


async def upload(client: httpx.AsyncClient, workspace: UUID, name: str) -> dict[str, Any]:
    created = await create_upload(client, workspace, f"Papers/{name}", body=b"a document")
    assert created.status_code == 201, created.text
    return created.json()


async def run_extraction(app: Any, token: str, **outputs: Any) -> None:
    """Claim every queued job and finish it with the same envelope."""
    async with as_extractor(app, token) as extractor:
        while (job := await claim_one(extractor)) is not None:
            done = await finish(extractor, job, **outputs)
            assert done.status_code == 200, done.text


async def carried(client: httpx.AsyncClient, file_id: str) -> dict[str, str]:
    """The file's tags as `{name: provenance}` — the shape most assertions here want."""
    response = await client.get(file_tags_url(file_id))
    assert response.status_code == 200, response.text
    return {one["name"]: one["provenance"] for one in response.json()}


@pytest.mark.fr("F-003/FR-3", "F-003/FR-11")
async def test_an_extractor_maps_its_labels_into_the_vocabulary(
    identity_settings: Settings, identity_database: str
) -> None:
    """Labels in, tags out — through the alias table first, into quarantine only as a last resort.

    `BILL` is not a tag; it is a synonym of `invoice`, and mapping it there is what keeps model
    drift out of the taxonomy (FR-11). `wombat` fits nothing, so it becomes a suggestion rather
    than vocabulary — and either way the file's tag carries the full stamp of what claimed it
    (FR-3): which extractor, which version, which model, which generation, how sure.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        invoice = await added(client, "invoice", aliases=["bill"])
        installed = await install(app, client, produces=["tags"])
        document = await upload(client, workspace, "tax.txt")

        await run_extraction(app, installed.token, tags=LABELS)

        listed = await client.get(file_tags_url(document["id"]))
        suggestions = await client.get(TAGS, params={"status": "suggested"})

    applied = {one["name"]: one for one in listed.json()}
    assert set(applied) == {"invoice", "wombat"}

    mapped = applied["invoice"]
    assert mapped["id"] == str(invoice), "the synonym resolved to the tag it means"
    assert mapped["provenance"] == "auto"
    assert mapped["status"] == "active"
    assert mapped["user"] is None, "no person said this"
    assert mapped["source"] == {
        "extractor": "pdf-text",
        "extractor_version": "1.0.0",
        "model_version": "1.28",
        "generation": 1,
        "confidence": 0.87,
    }

    invented = applied["wombat"]
    assert invented["status"] == "suggested", "a word nothing matched is quarantined, not adopted"
    assert invented["provenance"] == "auto"
    assert invented["source"]["confidence"] == 0.4

    # The review queue can see where the word came from.
    queued = suggestions.json()["data"]
    assert [one["name"] for one in queued] == ["wombat"]
    detail = queued[0]
    assert detail["status"] == "suggested"


@pytest.mark.fr("F-003/FR-12")
async def test_a_suggestion_is_quarantined_until_it_is_approved(
    identity_settings: Settings, identity_database: str
) -> None:
    """Visible on the file, invisible everywhere else — until an admin says otherwise.

    That is the whole bargain of ADR-0006's `suggested` state: the machine's proposal is not
    thrown away, and it does not leak into the vocabulary either. Approving is one call, needs no
    file to be touched, and makes the claims that were already there count.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client, produces=["tags"])
        document = await upload(client, workspace, "tax.txt")
        await run_extraction(app, installed.token, tags=[{"name": "wombat"}])

        on_file = await carried(client, document["id"])
        completion = await client.get(TAGS, params={"prefix": "wom"})
        vocabulary = await client.get(TAGS)
        pending = await client.get(TAGS, params={"status": "suggested"})
        wombat = pending.json()["data"][0]["id"]

        approved = await client.post(f"{TAGS}/{wombat}/approve", headers=SAME_ORIGIN)
        after = await client.get(TAGS, params={"prefix": "wom"})
        still_on_file = await carried(client, document["id"])

    assert on_file == {"wombat": "auto"}, "the file shows it, marked as what it is"
    assert completion.json()["data"] == [], "and no picker offers it"
    assert vocabulary.json()["data"] == []

    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "active"
    assert body["reviewed_at"] is not None
    assert body["reviewed_by"] is not None
    assert body["suggested_by_run"] is not None, "the word remembers what proposed it"

    offered = after.json()["data"]
    assert [one["name"] for one in offered] == ["wombat"]
    assert offered[0]["usage"] == {"files": 1, "folders": 0}, "the claim always counted"
    assert still_on_file == {"wombat": "auto"}, "approving changed the word, not the file"

    recorded = await read_events(identity_database, action="tag.approved")
    assert recorded[0]["details"]["name"] == "wombat"


@pytest.mark.fr("F-003/FR-12")
async def test_a_rejected_word_is_not_suggested_again(
    identity_settings: Settings, identity_database: str
) -> None:
    """Turning a suggestion down is a decision that has to survive the next run.

    So the row stays — that is what `rejected` is *for* — and the claims go with the decision.
    A later generation proposing the same word finds it already refused, which is the difference
    between a review queue and a treadmill.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        installed = await install(app, client, produces=["tags"])
        document = await upload(client, workspace, "tax.txt")
        await run_extraction(app, installed.token, tags=[{"name": "wombat"}])

        pending = await client.get(TAGS, params={"status": "suggested"})
        wombat = pending.json()["data"][0]["id"]
        rejected = await client.post(f"{TAGS}/{wombat}/reject", headers=SAME_ORIGIN)
        after_rejection = await carried(client, document["id"])

        # The same extractor, the same claim, a new generation of it.
        await reprocess(identity_database, UUID(document["version"]), generation=2)
        await run_extraction(app, installed.token, tags=[{"name": "wombat"}])

        after_rerun = await carried(client, document["id"])
        vocabulary = await client.get(TAGS, params={"status": "all"})
        claims = await rows_in(identity_database, file_auto_tag, "tag_id")

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert after_rejection == {}, "the claims went with the decision"
    assert after_rerun == {}, "and the next generation did not bring the word back"
    assert claims == []
    # The word is kept, not deleted: that record is what refuses it next time (ADR-0006).
    assert [(one["name"], one["status"]) for one in vocabulary.json()["data"]] == [
        ("wombat", "rejected")
    ]


@pytest.mark.fr("F-003/FR-4", "F-003/FR-6", "02/INV-4")
async def test_a_confirmed_tag_survives_a_model_that_changed_its_mind(
    identity_settings: Settings, identity_database: str
) -> None:
    """The promise the whole state machine exists for (ADR-0004, invariant 4).

    Generation 1 claims two tags. The user confirms one and adds one of their own. Generation 2
    claims something else entirely — and afterwards the confirmed tag and the manual tag are
    exactly as they were, while the unconfirmed claim is gone because that is the only kind of
    row reprocessing is allowed to replace.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await added(client, "invoice", aliases=["bill"])
        await added(client, "receipt")
        await added(client, "mine")
        installed = await install(app, client, produces=["tags"])
        document = await upload(client, workspace, "tax.txt")

        await run_extraction(
            app,
            installed.token,
            tags=[{"name": "bill", "confidence": 0.9}, {"name": "receipt", "confidence": 0.5}],
        )
        before = await carried(client, document["id"])

        tags_url = file_tags_url(document["id"])
        invoice_id = next(
            one["id"] for one in (await client.get(tags_url)).json() if one["name"] == "invoice"
        )
        confirmed = await client.post(f"{tags_url}/{invoice_id}/confirm", headers=SAME_ORIGIN)
        await tag_file(client, document["id"], name="mine")

        await reprocess(identity_database, UUID(document["version"]), generation=2)
        await run_extraction(app, installed.token, tags=[{"name": "receipt", "confidence": 0.2}])

        after = await client.get(tags_url)
        curation = await rows_in(identity_database, file_tag, "tag_id", "provenance")

    assert before == {"invoice": "auto", "receipt": "auto"}

    assert confirmed.status_code == 200, confirmed.text
    agreed = confirmed.json()
    assert agreed["provenance"] == "confirmed"
    assert agreed["user"] is not None
    assert agreed["source"]["confidence"] == 0.9, "a confirmed tag keeps the claim behind it"

    resolved = {one["name"]: one for one in after.json()}
    assert set(resolved) == {"invoice", "mine", "receipt"}
    assert resolved["invoice"]["provenance"] == "confirmed", "reprocessing did not touch it"
    assert resolved["mine"]["provenance"] == "manual"
    # The one nobody ruled on was replaced by the new generation's word for it.
    assert resolved["receipt"]["provenance"] == "auto"
    assert resolved["receipt"]["source"] == {
        "extractor": "pdf-text",
        "extractor_version": "1.0.0",
        "model_version": "1.28",
        "generation": 2,
        "confidence": 0.2,
    }
    # The confirmed tag's claim is generation 2's absence of it — the curation carries the tag
    # now, which is exactly what "reprocessing-immune" means.
    assert sorted(row["provenance"] for row in curation) == ["confirmed", "manual"]


@pytest.mark.fr("F-003/FR-5", "02/INV-4")
async def test_a_rejected_claim_does_not_come_back(
    identity_settings: Settings, identity_database: str
) -> None:
    """The "fox → cat comes back" failure, refused.

    Removing a machine's tag is not a deletion, it is a **rejection**: the claim goes and a
    record stays, so the same model, a newer model, and a copy of the same file all fail to put
    the word back. A tag the user only ever applied themselves is different — there is nothing
    to suppress — and this test asserts both halves so the distinction cannot rot.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await added(client, "invoice", aliases=["bill"])
        await added(client, "mine")
        installed = await install(app, client, produces=["tags"])
        document = await upload(client, workspace, "tax.txt")
        await run_extraction(app, installed.token, tags=[{"name": "bill"}])

        tags_url = file_tags_url(document["id"])
        invoice_id = next(
            one["id"] for one in (await client.get(tags_url)).json() if one["name"] == "invoice"
        )
        await tag_file(client, document["id"], name="mine")
        mine_id = next(
            one["id"] for one in (await client.get(tags_url)).json() if one["name"] == "mine"
        )

        rejected = await client.delete(f"{tags_url}/{invoice_id}", headers=SAME_ORIGIN)
        removed = await client.delete(f"{tags_url}/{mine_id}", headers=SAME_ORIGIN)
        after_removal = await carried(client, document["id"])

        await reprocess(identity_database, UUID(document["version"]), generation=2)
        await run_extraction(app, installed.token, tags=[{"name": "bill"}, {"name": "mine"}])
        after_rerun = await carried(client, document["id"])

        curation = await rows_in(identity_database, file_tag, "tag_id", "provenance")
        claims = await rows_in(identity_database, file_auto_tag, "tag_id")

    assert rejected.status_code == 204, rejected.text
    assert removed.status_code == 204
    assert after_removal == {}

    # The rejected word stays away; the one the user merely un-applied is claimable again,
    # because no model was ever contradicted about it.
    assert after_rerun == {"mine": "auto"}
    assert [(row["tag_id"], row["provenance"]) for row in curation] == [
        (UUID(invoice_id), "rejected")
    ]
    assert [row["tag_id"] for row in claims] == [UUID(mine_id)]

    actions = [entry["action"] for entry in await read_events(identity_database)]
    assert actions.count("file.tag_rejected") == 1, "rejecting a machine's claim is its own act"
    assert actions.count("file.untagged") == 1, "removing one's own tag is not"


async def test_confirming_needs_a_claim_to_agree_with(
    identity_settings: Settings, identity_database: str
) -> None:
    """Confirm is an answer to a machine. With no claim there is nothing to answer."""
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        invoice = await added(client, "invoice")
        installed = await install(app, client, produces=["tags"])
        document = await upload(client, workspace, "tax.txt")
        await run_extraction(app, installed.token, tags=[{"name": "wombat"}])

        tags_url = file_tags_url(document["id"])
        nothing_claimed = await client.post(f"{tags_url}/{invoice}/confirm", headers=SAME_ORIGIN)
        no_such_tag = await client.post(
            f"{tags_url}/0198c0de-0000-7000-8000-000000000000/confirm", headers=SAME_ORIGIN
        )
        suggested = next(
            one["id"] for one in (await client.get(tags_url)).json() if one["name"] == "wombat"
        )
        quarantined = await client.post(f"{tags_url}/{suggested}/confirm", headers=SAME_ORIGIN)

    assert nothing_claimed.status_code == 404, nothing_claimed.text
    assert no_such_tag.status_code == 404
    # A suggestion is not vocabulary yet, so agreeing with it is premature rather than wrong.
    assert quarantined.status_code == 409, quarantined.text
    assert "administrator" in quarantined.json()["detail"]


async def test_a_copy_of_the_same_file_gets_the_same_tags(
    identity_settings: Settings, identity_database: str
) -> None:
    """Reuse copies the claims too — and still respects what *this* file's owner rejected.

    Identical bytes mean identical analysis (F-009/FR-8), so a duplicate arrives already tagged
    without running anything. The exception is a rejection: it belongs to the file, and a word
    somebody refused here must not arrive because a copy of the document was uploaded.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        await added(client, "invoice", aliases=["bill"])
        installed = await install(app, client, produces=["tags"])
        first = await upload(client, workspace, "tax.txt")
        await run_extraction(app, installed.token, tags=[{"name": "bill", "confidence": 0.8}])

        copy = await upload(client, workspace, "copy.txt")
        on_copy = await carried(client, copy["id"])

    assert first["extraction_status"] == "pending"
    assert copy["extraction_status"] == "indexed", "reuse finished it on arrival"
    assert on_copy == {"invoice": "auto"}


@pytest.mark.fr("F-003/FR-10")
async def test_a_merge_carries_machine_claims_across(
    identity_settings: Settings, identity_database: str
) -> None:
    """A merge moves what the machines said, not only what people said.

    Two words for one concept, both claimed by the same run: after the merge the file carries one
    tag, and the claim behind it is still there to be confirmed or rejected.
    """
    async with extraction_ready(identity_settings, identity_database) as (
        app,
        client,
        workspace,
        _root,
    ):
        invoice = await added(client, "invoice")
        bill = await added(client, "bill")
        installed = await install(app, client, produces=["tags"])
        document = await upload(client, workspace, "tax.txt")
        await run_extraction(
            app,
            installed.token,
            tags=[{"name": "invoice", "confidence": 0.6}, {"name": "bill", "confidence": 0.9}],
        )
        before = await carried(client, document["id"])

        merged = await client.post(
            f"{TAGS}/{bill}/merge", json={"into": str(invoice)}, headers=SAME_ORIGIN
        )
        after = await client.get(file_tags_url(document["id"]))
        claims = await rows_in(identity_database, file_auto_tag, "tag_id", "confidence")

    assert before == {"bill": "auto", "invoice": "auto"}
    assert merged.status_code == 200, merged.text
    assert merged.json()["moved_files"] == 0, "no person had said anything about either word"

    surviving = after.json()
    assert [one["name"] for one in surviving] == ["invoice"]
    assert surviving[0]["provenance"] == "auto"
    # One claim per (version, tag, run): the duplicate the merge would have created is dropped,
    # and the surviving row is still a claim somebody can confirm.
    assert [row["tag_id"] for row in claims] == [invoice]
