"""The vocabulary: one global DAG, its synonyms, and the rules that keep it one thing.

[F-003](../../features/F-003-tagging.md) and
[ADR-0006](../../decisions/ADR-0006-hierarchical-tags-dag.md).
The tests come in four groups, and the second is the one that matters most:

- **it works** — a tag is created with its place and its synonyms, the closure answers "what is
  under `nature`", completion offers what the caller actually uses;
- **one word means one thing** — case, whitespace and Unicode spelling collapse to one identity,
  and a synonym can never be another tag's name, because both live in one keyed table;
- **restructuring is free** — re-parenting and renaming touch no file rows, which is the whole
  reason expansion happens at query time (ADR-0006);
- **it refuses** — cycles, a member curating the taxonomy, erasing a tag something carries, a
  quarantined suggestion being applied by hand.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from store_everything import tags
from store_everything.api.v1.router import API_V1_PREFIX
from store_everything.config import Settings
from store_everything.events import Actor
from store_everything.tables import file_tag, tag_closure
from tests.identity_helpers import SAME_ORIGIN, read_events
from tests.tag_helpers import (
    TAGS,
    added,
    connected,
    create_tag,
    names_on_file,
    tag_file,
    update_tag,
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


# --------------------------------------------------------------------------------- it works


@pytest.mark.fr("F-003/FR-10")
async def test_a_tag_is_created_with_its_place_and_its_synonyms(
    identity_settings: Settings, identity_database: str
) -> None:
    """One request carries the word, where it sits, and what else means it."""
    async with instance(identity_settings) as app, signed_in(app) as client:
        finance = await added(client, "finance")
        response = await create_tag(
            client, "invoice", parents=[finance], aliases=["bill", "Rechnung"]
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "invoice"
    assert body["status"] == "active"
    assert body["aliases"] == ["bill", "Rechnung"]
    assert [parent["name"] for parent in body["parents"]] == ["finance"]
    assert [ancestor["name"] for ancestor in body["ancestors"]] == ["finance"]
    assert body["usage"] == {"files": 0, "folders": 0}

    # Three separate actions in the log, not one `tag.updated`: an admin asking "who moved
    # `invoice` under `finance`" should not have to read a details blob to find out.
    recorded = [entry["action"] for entry in await read_events(identity_database)]
    assert recorded.count("tag.created") == 2
    assert recorded.count("tag.alias_added") == 2
    assert recorded.count("tag.parent_added") == 1


@pytest.mark.fr("F-003/FR-1")
async def test_a_broad_tag_reaches_every_tag_below_it(
    identity_settings: Settings, identity_database: str
) -> None:
    """The closure, and the diamond that makes it more than a tree.

    `tree` sits under `plant` *and* under `landscaping`, which is ADR-0006's multi-parent case.
    Expansion asks whether a pair exists, and the depth it records is the **shortest** path —
    two ways down to the same tag is one row, not two.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        nature = await added(client, "nature")
        plant = await added(client, "plant", parents=[nature])
        landscaping = await added(client, "landscaping", parents=[nature])
        tree = await added(client, "tree", parents=[plant, landscaping])
        # A second path to `tree` of a different length: nature → tree directly.
        await update_tag(client, tree, parents=[str(plant), str(landscaping), str(nature)])
        detail = await client.get(f"{TAGS}/{tree}")

    assert detail.status_code == 200
    assert [one["name"] for one in detail.json()["ancestors"]] == [
        "landscaping",
        "nature",
        "plant",
    ]

    async with connected(identity_database) as connection:
        under_nature = (
            await connection.execute(
                select(tag_closure.c.descendant_id, tag_closure.c.depth).where(
                    tag_closure.c.ancestor_id == nature
                )
            )
        ).all()
        depths = {row.descendant_id: row.depth for row in under_nature}

    assert set(depths) == {nature, plant, landscaping, tree}
    assert depths[nature] == 0, "a tag is its own descendant, so a subtree includes its root"
    assert depths[plant] == 1
    # Reachable at 2 (via `plant`) and at 1 (directly): the shorter one is what is recorded.
    assert depths[tree] == 1


@pytest.mark.fr("F-003/FR-1")
async def test_the_closure_follows_the_edges_both_ways(
    identity_settings: Settings, identity_database: str
) -> None:
    """A deep chain is reachable end to end, and cutting one edge really shrinks the closure.

    Both halves are about the rebuild. Its recursion is bounded by the number of tags — in an
    acyclic graph no path can be longer than that — so a five-level chain must come out whole
    rather than truncated. And a *removal* is why the table is rebuilt rather than incremented:
    subtracting an edge's contribution would be wrong wherever another path still connects the
    same pair, so the honest thing is to recompute.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        chain: list[UUID] = []
        for name in ("a", "b", "c", "d", "e"):
            chain.append(await added(client, name, parents=[chain[-1]] if chain else None))
        cut = await update_tag(client, chain[1], parents=[])
        assert cut.status_code == 200, cut.text

    async with connected(identity_database) as connection:
        reachable = {
            (row.ancestor_id, row.descendant_id): row.depth
            for row in (
                await connection.execute(
                    select(
                        tag_closure.c.ancestor_id,
                        tag_closure.c.descendant_id,
                        tag_closure.c.depth,
                    )
                )
            ).all()
        }

    top, second, last = chain[0], chain[1], chain[4]
    assert reachable[(second, last)] == 3, "b to e is still three levels down"
    assert (top, last) not in reachable, "cutting a → b disconnected the whole tail"
    assert (top, top) in reachable, "and every tag is still its own ancestor"
    assert not any(
        ancestor == descendant and depth > 0 for (ancestor, descendant), depth in reachable.items()
    )


@pytest.mark.fr("F-003/FR-8")
async def test_completion_offers_the_words_this_caller_uses(
    identity_settings: Settings, identity_database: str
) -> None:
    """Prefix completion, ranked by the caller's own usage, synonyms included.

    Ranking by usage is what makes a tag box usable: three files tagged `investment` should put
    it above an `invoice` used once, whatever the alphabet says.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        await added(client, "invoice", aliases=["bill"])
        await added(client, "investment")
        await added(client, "invitation")
        for index in range(3):
            created = await create_upload(client, workspace, f"Photos/n{index}.txt", body=b"x")
            assert created.status_code == 201, created.text
            applied = await tag_file(client, created.json()["id"], name="investment")
            assert applied.status_code == 201, applied.text
        one = await create_upload(client, workspace, "Photos/one.txt", body=b"y")
        await tag_file(client, one.json()["id"], name="invoice")

        completion = await client.get(TAGS, params={"prefix": "inv"})
        by_alias = await client.get(TAGS, params={"prefix": "bil"})
        narrow = await client.get(TAGS, params={"prefix": "inv", "limit": 1})

    assert completion.status_code == 200
    offered = completion.json()["data"]
    assert [one["name"] for one in offered] == ["investment", "invoice", "invitation"]
    assert offered[0]["usage"] == {"files": 3, "folders": 0}
    assert offered[1]["usage"] == {"files": 1, "folders": 0}
    # A completion is one page: there is nothing to page through.
    assert completion.json()["next_cursor"] is None
    assert [one["name"] for one in narrow.json()["data"]] == ["investment"]

    # Typing a synonym offers the tag it means, and says which spelling matched.
    matched = by_alias.json()["data"]
    assert [one["name"] for one in matched] == ["invoice"]
    assert matched[0]["matched"] == "bill"
    assert matched[0]["matched_alias"] is True


@pytest.mark.fr("F-003/FR-8")
async def test_usage_counts_are_the_callers_own(
    identity_settings: Settings, identity_database: str
) -> None:
    """A count is over what the caller can see — not over the instance.

    Otherwise an autocomplete would tell every member how many files somebody else has tagged
    `divorce`, and instance admin is not data access (07).
    """
    async with instance(identity_settings) as app, signed_in(app) as admin:
        await added(admin, "tax")
        member = await create_member(admin)
        mine = await create_workspace(admin, "Mine")
        theirs = await create_workspace(admin, "Theirs", owner=member)
        assert theirs.status_code == 201, theirs.text
        await provision_pending(identity_database)

        for index in range(2):
            created = await create_upload(admin, mine.json()["id"], f"Mine/a{index}.txt", body=b"x")
            await tag_file(admin, created.json()["id"], name="tax")
        admin_view = await admin.get(TAGS, params={"prefix": "ta"})

        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as other:
            created = await create_upload(other, theirs.json()["id"], "Theirs/b.txt", body=b"y")
            assert created.status_code == 201, created.text
            await tag_file(other, created.json()["id"], name="tax")
            member_view = await other.get(TAGS, params={"prefix": "ta"})
        admin_again = await admin.get(TAGS, params={"prefix": "ta"})

    assert admin_view.json()["data"][0]["usage"] == {"files": 2, "folders": 0}
    assert member_view.json()["data"][0]["usage"] == {"files": 1, "folders": 0}
    # The member's file did not change what the admin is told.
    assert admin_again.json()["data"][0]["usage"] == {"files": 2, "folders": 0}


async def test_the_taxonomy_lists_in_name_order_and_pages(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as client:
        for name in ("delta", "alpha", "charlie", "bravo"):
            await added(client, name)
        first = await client.get(TAGS, params={"limit": 2})
        assert first.status_code == 200, first.text
        second = await client.get(TAGS, params={"limit": 2, "cursor": first.json()["next_cursor"]})

    assert [one["name"] for one in first.json()["data"]] == ["alpha", "bravo"]
    assert [one["name"] for one in second.json()["data"]] == ["charlie", "delta"]
    assert second.json()["next_cursor"] is None


# ------------------------------------------------------------------- one word, one meaning


@pytest.mark.fr("F-003/FR-1")
async def test_two_spellings_of_one_word_cannot_both_be_tags(
    identity_settings: Settings, identity_database: str
) -> None:
    """Normalization is identity, not tidiness (FR-1).

    A vocabulary holding `Invoice` and `invoice`, or `tax return` and `tax  return`, is one
    where completion offers the same word twice and two files tagged "the same" do not match.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        created = await create_tag(client, "  tax   return ")
        assert created.status_code == 201, created.text
        assert created.json()["name"] == "tax return", "whitespace collapses on the way in"

        cased = await create_tag(client, "Tax Return")
        spaced = await create_tag(client, "tax  return")
        # `Müller` composed vs. decomposed: the same word to a person, and to us.
        composed = await create_tag(client, "Müller")
        decomposed = await create_tag(client, "Müller")
        blank = await create_tag(client, "   ")
        # A tab is whitespace, so it collapses like any other run of it; a bell is not a name.
        tabbed = await create_tag(client, "tab\there")
        control = await create_tag(client, "bell\x07here")
        listed = await client.get(TAGS)

    assert cased.status_code == 409, cased.text
    assert spaced.status_code == 409
    assert composed.status_code == 201
    assert decomposed.status_code == 409
    assert blank.status_code == 422
    assert tabbed.status_code == 201
    assert tabbed.json()["name"] == "tab here"
    assert control.status_code == 422
    assert [one["name"] for one in listed.json()["data"]] == ["Müller", "tab here", "tax return"]


@pytest.mark.fr("F-003/FR-1")
async def test_a_synonym_can_never_be_another_tags_name(
    identity_settings: Settings, identity_database: str
) -> None:
    """Canonical names and synonyms share one keyed table, so `bill` cannot mean two things.

    This is the failure the schema is shaped to prevent: two rows claiming one word, resolved
    by whichever query happened to run first.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        invoice = await added(client, "invoice", aliases=["bill"])
        as_a_tag = await create_tag(client, "Bill")
        other = await added(client, "receipt")
        as_an_alias = await update_tag(client, other, aliases=["BILL"])
        own_name = await update_tag(client, invoice, aliases=["Invoice"])
        detail = await client.get(f"{TAGS}/{invoice}")

    assert as_a_tag.status_code == 409, as_a_tag.text
    assert "synonym" in as_a_tag.json()["detail"]
    assert as_an_alias.status_code == 409
    # A tag's own name is not a synonym for itself — a rule worth stating rather than absorbing.
    assert own_name.status_code == 422, own_name.text
    assert detail.json()["aliases"] == ["bill"]


@pytest.mark.fr("F-003/FR-1")
async def test_a_name_is_bounded_and_a_taken_name_stays_taken(
    identity_settings: Settings, identity_database: str
) -> None:
    """The two remaining ways a name can be refused: too long, and already meaning something."""
    async with instance(identity_settings) as app, signed_in(app) as client:
        invoice = await added(client, "invoice")
        receipt = await added(client, "receipt")
        too_long = await create_tag(client, "x" * 101)
        onto_taken = await update_tag(client, receipt, name="Invoice")
        renamed_long = await update_tag(client, receipt, name="y" * 101)
        detail = await client.get(f"{TAGS}/{invoice}")

    # The bound is on the request schema as well as in the name policy, so an over-long name
    # never reaches the vocabulary — whichever layer says no first.
    assert too_long.status_code == 422, too_long.text
    assert onto_taken.status_code == 409, onto_taken.text
    assert str(invoice) in onto_taken.json()["detail"]
    assert renamed_long.status_code == 422
    assert detail.json()["name"] == "invoice", "the tag that held the name was not touched"


@pytest.mark.fr("F-003/FR-8")
async def test_a_completion_offers_each_tag_once(
    identity_settings: Settings, identity_database: str
) -> None:
    """Two spellings of one tag can match one prefix; the tag is still offered once.

    `invoice` with the synonym `invoices` matches `invoic` twice. Offering it twice would be a
    bug a user sees, and the canonical name is the one to show.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        await added(client, "invoice", aliases=["invoices"])
        # Two synonyms match, and the canonical name does not — so the *closest* one is shown.
        await added(client, "automobile", aliases=["car", "carriage"])
        offered = await client.get(TAGS, params={"prefix": "invoic"})
        synonyms = await client.get(TAGS, params={"prefix": "car"})
        blank = await client.get(TAGS, params={"prefix": " "})

    matches = offered.json()["data"]
    assert [one["name"] for one in matches] == ["invoice"]
    assert matches[0]["matched"] == "invoice"
    assert matches[0]["matched_alias"] is False

    by_synonym = synonyms.json()["data"]
    assert [one["name"] for one in by_synonym] == ["automobile"]
    assert by_synonym[0]["matched"] == "car"
    assert by_synonym[0]["matched_alias"] is True
    assert blank.json()["data"] == [], "a prefix of nothing completes nothing"


@pytest.mark.fr("F-003/FR-10")
async def test_two_admins_naming_one_word_at_once(
    identity_settings: Settings, identity_database: str
) -> None:
    """The name registry's primary key is the guarantee; the pre-check is only the message.

    Two connections, one word: the second's lookup runs while the first is still uncommitted, so
    it sees nothing and inserts — and finds out at the database. Answering that with a conflict
    the caller can retry is the difference between a race and a `500`.
    """
    async with connected(identity_database) as reader:
        admin = await tags.create(reader, name="placeholder", actor=Actor.system())

    engine = create_async_engine(identity_database)
    try:
        async with engine.connect() as first, engine.connect() as second:
            await tags.create(first, name="tax", actor=Actor.system(), created_by=admin.created_by)
            racing = asyncio.create_task(
                tags.create(second, name="tax", actor=Actor.system(), created_by=admin.created_by)
            )
            # Long enough for the second insert to be waiting on the first's uncommitted row.
            await asyncio.sleep(0.2)
            await first.commit()
            with pytest.raises(tags.NameRaceError) as raced:
                await racing
            await second.rollback()
    finally:
        await engine.dispose()

    assert raced.value.name == "tax"


# ------------------------------------------------------------- restructuring is free


@pytest.mark.fr("F-003/FR-1")
async def test_restructuring_the_taxonomy_touches_no_file(
    identity_settings: Settings, identity_database: str
) -> None:
    """The point of expanding at query time: a file keeps exactly the tag it was given.

    Re-parenting `tree` under `landscaping` must not write `landscaping` onto a forest photo —
    that is the materialization ADR-0006 rejects, and this is the test that would catch it.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        plant = await added(client, "plant")
        landscaping = await added(client, "landscaping")
        tree = await added(client, "tree", parents=[plant])
        created = await create_upload(client, workspace, "Photos/forest.jpg", body=b"x")
        applied = await tag_file(client, created.json()["id"], tag=tree)
        assert applied.status_code == 201, applied.text
        file_id = created.json()["id"]
        before = await client.get(f"{TAGS}/{tree}")

        async with connected(identity_database) as connection:
            stamped = (
                await connection.execute(
                    select(file_tag.c.updated_at).where(file_tag.c.tag_id == tree)
                )
            ).scalar_one()

        moved = await update_tag(client, tree, parents=[str(landscaping)])
        assert moved.status_code == 200, moved.text
        after = await names_on_file(client, file_id)

        async with connected(identity_database) as connection:
            rows = (
                await connection.execute(
                    select(file_tag.c.tag_id, file_tag.c.updated_at).where(
                        file_tag.c.file_id == created.json()["id"]
                    )
                )
            ).all()

    assert [one["name"] for one in before.json()["parents"]] == ["plant"]
    assert after == ["tree"], "the file carries the specific tag and nothing else"
    assert [(row.tag_id, row.updated_at) for row in rows] == [(tree, stamped)]


@pytest.mark.fr("F-003/FR-10")
async def test_a_rename_follows_the_tag_onto_its_files(
    identity_settings: Settings, identity_database: str
) -> None:
    """A rename is one row: the files keep the tag, and the old word stops resolving.

    Not keeping the old name as a synonym is a decision — a rename says the word was wrong. An
    admin who wants it to keep resolving adds it back, and the second half of this test is that
    doing so works, including promoting an existing synonym to the canonical name.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        tree = await added(client, "tree", aliases=["timber"])
        created = await create_upload(client, workspace, "Photos/forest.jpg", body=b"x")
        file_id = created.json()["id"]
        await tag_file(client, file_id, tag=tree)

        renamed = await update_tag(client, tree, name="trees")
        after_rename = await names_on_file(client, file_id)
        by_old_name = await tag_file(client, file_id, name="tree")

        promoted = await update_tag(client, tree, name="timber")
        after_promotion = await client.get(f"{TAGS}/{tree}")

    assert renamed.status_code == 200, renamed.text
    assert after_rename == ["trees"], "the tag is the same tag, under a different word"
    assert by_old_name.status_code == 422, "the old word no longer means anything"

    assert promoted.status_code == 200, promoted.text
    body = after_promotion.json()
    assert body["name"] == "timber"
    assert body["aliases"] == [], "the promoted synonym is the name now, not both"


@pytest.mark.fr("F-003/FR-10")
async def test_a_spelling_change_is_not_a_new_word(
    identity_settings: Settings, identity_database: str
) -> None:
    """`invoice` to `Invoice` changes what is displayed and nothing else.

    The identity is the folded key, so this is not a rename onto a free name — it is the same
    row, and a tag with the same identity must not be refused as a collision with itself.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        invoice = await added(client, "invoice")
        created = await create_upload(client, workspace, "Photos/tax.pdf", body=b"x")
        await tag_file(client, created.json()["id"], tag=invoice)

        cased = await update_tag(client, invoice, name="Invoice")
        on_file = await names_on_file(client, created.json()["id"])
        listed = await client.get(TAGS)

    assert cased.status_code == 200, cased.text
    assert cased.json()["name"] == "Invoice"
    assert on_file == ["Invoice"]
    assert len(listed.json()["data"]) == 1, "one tag, not two spellings of one"


@pytest.mark.fr("F-003/FR-10")
async def test_synonyms_and_parents_are_replaced_by_what_is_sent(
    identity_settings: Settings, identity_database: str
) -> None:
    """A declarative edit: the lists sent are what the tag ends up with.

    Which also means the removals happen — and that sending the set a tag already has is not an
    edit, so it leaves nothing in the audit trail to explain.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        finance = await added(client, "finance")
        invoice = await added(client, "invoice", parents=[finance], aliases=["bill", "facture"])

        trimmed = await update_tag(client, invoice, aliases=["bill"])
        unchanged = await update_tag(client, invoice, parents=[str(finance)])
        detail = await client.get(f"{TAGS}/{invoice}")
        by_dropped_name = await client.get(TAGS, params={"prefix": "factur"})

    assert trimmed.status_code == 200, trimmed.text
    assert unchanged.status_code == 200
    assert detail.json()["aliases"] == ["bill"]
    assert [one["name"] for one in detail.json()["parents"]] == ["finance"]
    assert by_dropped_name.json()["data"] == [], "the dropped synonym resolves to nothing"

    recorded = [entry["action"] for entry in await read_events(identity_database)]
    assert recorded.count("tag.alias_removed") == 1
    # The parent it already had was not re-recorded: sending the same set changes nothing.
    assert recorded.count("tag.parent_added") == 1


@pytest.mark.fr("F-003/FR-10")
async def test_a_merge_absorbs_the_place_in_the_dag(
    identity_settings: Settings, identity_database: str
) -> None:
    """The merged tag's edges move to the survivor — without duplicating or looping.

    Four cases in one graph, because merging is where a taxonomy can quietly break:

    - a parent both tags share (`finance`) is **already there** and must not be added twice;
    - a parent only the merged tag had (`expenses`) **moves**;
    - a parent that sits *below* the survivor (`paid`) would make it its own ancestor, so that
      edge is **dropped** — merging a tag into something above its own parent is a legitimate
      thing to want, and refusing the whole merge over it would be pedantry;
    - a child both tags share (`receipt`) is already there too.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        finance = await added(client, "finance")
        invoice = await added(client, "invoice", parents=[finance])
        expenses = await added(client, "expenses")
        paid = await added(client, "paid", parents=[invoice])
        bill = await added(client, "bill", parents=[finance, expenses, paid])
        await added(client, "receipt", parents=[invoice, bill])
        scan = await added(client, "scanned bill", parents=[bill])

        merged = await client.post(
            f"{TAGS}/{bill}/merge", json={"into": str(invoice)}, headers=SAME_ORIGIN
        )
        survivor = await client.get(f"{TAGS}/{invoice}")
        moved_child = await client.get(f"{TAGS}/{scan}")

    assert merged.status_code == 200, merged.text
    body = survivor.json()
    assert [one["name"] for one in body["parents"]] == ["expenses", "finance"]
    assert [one["name"] for one in body["children"]] == ["paid", "receipt", "scanned bill"]
    # Nearest first: the child now sits under `invoice`, which sits under `finance`.
    assert [one["name"] for one in moved_child.json()["ancestors"]] == [
        "invoice",
        "expenses",
        "finance",
    ]


@pytest.mark.fr("F-003/FR-10")
async def test_a_merge_never_lets_a_child_become_an_ancestor(
    identity_settings: Settings, identity_database: str
) -> None:
    """The other direction of the same rule: `bill → boring → invoice`, merged upward.

    `boring` is the merged tag's child *and* the survivor's parent, so moving that edge across
    would make `invoice` its own ancestor. It is dropped, and the graph stays a DAG.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        boring = await added(client, "boring")
        invoice = await added(client, "invoice", parents=[boring])
        bill = await added(client, "bill")
        await update_tag(client, boring, parents=[str(bill)])

        merged = await client.post(
            f"{TAGS}/{bill}/merge", json={"into": str(invoice)}, headers=SAME_ORIGIN
        )
        survivor = await client.get(f"{TAGS}/{invoice}")

    assert merged.status_code == 200, merged.text
    body = survivor.json()
    assert [one["name"] for one in body["children"]] == [], "the looping edge was dropped"
    assert [one["name"] for one in body["parents"]] == ["boring"]
    assert [one["name"] for one in body["ancestors"]] == ["boring"]


@pytest.mark.fr("F-003/FR-10")
async def test_a_merge_needs_two_different_tags(
    identity_settings: Settings, identity_database: str
) -> None:
    async with instance(identity_settings) as app, signed_in(app) as client:
        invoice = await added(client, "invoice")
        into_itself = await client.post(
            f"{TAGS}/{invoice}/merge", json={"into": str(invoice)}, headers=SAME_ORIGIN
        )
        into_nothing = await client.post(
            f"{TAGS}/{invoice}/merge",
            json={"into": "0198c0de-0000-7000-8000-000000000000"},
            headers=SAME_ORIGIN,
        )
        from_nothing = await client.post(
            f"{TAGS}/0198c0de-0000-7000-8000-000000000000/merge",
            json={"into": str(invoice)},
            headers=SAME_ORIGIN,
        )

    assert into_itself.status_code == 422, into_itself.text
    assert into_nothing.status_code == 422
    assert from_nothing.status_code == 404


@pytest.mark.fr("F-003/FR-10")
async def test_a_merge_folds_one_word_into_another(
    identity_settings: Settings, identity_database: str
) -> None:
    """Two words for one concept become one tag — and everything both carried survives.

    A merge is what makes a taxonomy safe to grow carelessly: whatever anybody typed still
    resolves, it just resolves to one tag now.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        parent = await added(client, "finance")
        invoice = await added(client, "invoice", parents=[parent])
        bill = await added(client, "bill", aliases=["facture"])
        both = await create_upload(client, workspace, "Photos/both.txt", body=b"a")
        only_bill = await create_upload(client, workspace, "Photos/bill.txt", body=b"b")
        only_invoice = await create_upload(client, workspace, "Photos/invoice.txt", body=b"c")
        await tag_file(client, both.json()["id"], tag=invoice)
        await tag_file(client, both.json()["id"], tag=bill)
        await tag_file(client, only_bill.json()["id"], tag=bill)
        await tag_file(client, only_invoice.json()["id"], tag=invoice)

        merged = await client.post(
            f"{TAGS}/{bill}/merge", json={"into": str(invoice)}, headers=SAME_ORIGIN
        )
        gone = await client.get(f"{TAGS}/{bill}")
        survivor = await client.get(f"{TAGS}/{invoice}")
        by_merged_name = await client.get(TAGS, params={"prefix": "bil"})
        on_both = await names_on_file(client, both.json()["id"])
        on_one = await names_on_file(client, only_bill.json()["id"])
        untouched = await names_on_file(client, only_invoice.json()["id"])

    assert merged.status_code == 200, merged.text
    # Two files carried the merged word; the third only ever had the survivor and is not moved.
    assert merged.json()["moved_files"] == 2
    assert untouched == ["invoice"]
    assert gone.status_code == 404, "the merged tag is not a tag any more"
    assert survivor.json()["aliases"] == ["bill", "facture"], "every spelling still resolves"
    assert [one["name"] for one in by_merged_name.json()["data"]] == ["invoice"]
    assert on_both == ["invoice"], "the file that had both carries one row, not two"
    assert on_one == ["invoice"]

    recorded = await read_events(identity_database, action="tag.merged")
    assert recorded[0]["details"]["merged"] == "bill"
    assert recorded[0]["details"]["into"] == "invoice"


@pytest.mark.fr("F-003/FR-10")
async def test_a_merge_keeps_the_stronger_curation(
    identity_settings: Settings, identity_database: str
) -> None:
    """Where both tags were on one file, the stronger statement survives.

    The rejection here is written directly: rejecting is what a user does to a *machine's*
    claim, and machine claims arrive with the auto-tag path. Merge has to handle the state
    already, because a merge of two long-lived tags is exactly where it turns up.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        keep = await added(client, "invoice")
        fold = await added(client, "bill")
        someone_else = await create_member(client)
        created = await create_upload(client, workspace, "Photos/one.txt", body=b"a")
        file_id = created.json()["id"]
        applied = await tag_file(client, file_id, tag=fold)
        assert applied.status_code == 201, applied.text

        async with connected(identity_database) as connection:
            # The survivor already carries somebody else's rejection of the word being folded in.
            await connection.execute(
                file_tag.insert().values(
                    file_id=file_id,
                    tag_id=keep,
                    provenance="rejected",
                    user_id=someone_else,
                )
            )

        merged = await client.post(
            f"{TAGS}/{fold}/merge", json={"into": str(keep)}, headers=SAME_ORIGIN
        )
        listed = await client.get(f"{API_V1_PREFIX}/files/{file_id}/tags")

    assert merged.status_code == 200, merged.text
    # `manual` beats `rejected`: the user applied this concept under one of its names, and a
    # merge must not turn that into a removal.
    surviving = listed.json()
    assert [one["name"] for one in surviving] == ["invoice"]
    assert surviving[0]["provenance"] == "manual"
    # The author travels with the statement: the row must not claim the rejecter applied it.
    assert surviving[0]["user"] == applied.json()["user"] != str(someone_else)


# ------------------------------------------------------------------------------ it refuses


@pytest.mark.fr("F-003/FR-10")
async def test_a_cycle_is_refused_before_it_exists(
    identity_settings: Settings, identity_database: str
) -> None:
    """A tag cannot be its own ancestor — checked against the closure, not the direct edges.

    The two-step case is the one that matters: `plant → tree` exists, so making `plant` a child
    of `tree` closes a loop, and so does making it a child of anything below `tree`.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        plant = await added(client, "plant")
        tree = await added(client, "tree", parents=[plant])
        leaf = await added(client, "leaf", parents=[tree])

        direct = await update_tag(client, plant, parents=[str(tree)])
        distant = await update_tag(client, plant, parents=[str(leaf)])
        itself = await update_tag(client, plant, parents=[str(plant)])
        unknown = await update_tag(client, plant, parents=["0198c0de-0000-7000-8000-000000000000"])
        nothing = await update_tag(client, plant)

    assert direct.status_code == 409, direct.text
    assert "own ancestor" in direct.json()["detail"]
    assert distant.status_code == 409
    assert itself.status_code == 409
    assert unknown.status_code == 422, unknown.text
    assert nothing.status_code == 422, "a request that changes nothing is not an update"


@pytest.mark.fr("F-003/FR-10")
async def test_only_an_administrator_curates_the_vocabulary(
    identity_settings: Settings, identity_database: str
) -> None:
    """Members apply the vocabulary; they do not shape it (FR-10).

    The read side is deliberately open: a shared vocabulary that members cannot browse is not
    usable. What they may not see is what is *not* vocabulary — a pending suggestion or a word
    the instance turned down.
    """
    async with instance(identity_settings) as app:
        async with signed_in(app) as admin:
            existing = await added(admin, "finance")
            other = await added(admin, "invoice")
            await create_member(admin)

        async with signed_in(app, email=MEMBER_EMAIL, password=MEMBER_PASSWORD) as member:
            created = await create_tag(member, "personal")
            renamed = await update_tag(member, existing, name="money")
            merged = await member.post(
                f"{TAGS}/{other}/merge", json={"into": str(existing)}, headers=SAME_ORIGIN
            )
            erased = await member.delete(f"{TAGS}/{existing}", headers=SAME_ORIGIN)
            listed = await member.get(TAGS)
            quarantined = await member.get(TAGS, params={"status": "suggested"})

    assert created.status_code == 403, created.text
    assert renamed.status_code == 403
    assert merged.status_code == 403
    assert erased.status_code == 403
    assert listed.status_code == 200
    assert [one["name"] for one in listed.json()["data"]] == ["finance", "invoice"]
    assert quarantined.status_code == 422, quarantined.text


async def test_a_suggested_tag_is_not_vocabulary(
    identity_settings: Settings, identity_database: str
) -> None:
    """Quarantine, from the outside.

    A machine-suggested tag is created here through the module the auto-tagger will use, because
    the review surface that turns one into vocabulary is the next piece of work. What is already
    true is the part that protects the vocabulary: it is invisible to completion, absent from a
    member's listing, and cannot be applied by hand.
    """
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        async with connected(identity_database) as connection:
            suggested = await tags.create(
                connection, name="wombat", actor=Actor.extractor(), status="suggested"
            )

        async with connected(identity_database) as connection:
            turned_down = await tags.create(
                connection, name="platypus", actor=Actor.extractor(), status="rejected"
            )

        created = await create_upload(client, workspace, "Photos/one.txt", body=b"a")
        by_hand = await tag_file(client, created.json()["id"], tag=suggested.id)
        refused = await tag_file(client, created.json()["id"], tag=turned_down.id)
        completion = await client.get(TAGS, params={"prefix": "wom"})
        listed = await client.get(TAGS)
        for_admins = await client.get(TAGS, params={"status": "suggested"})
        readable = await client.get(f"{TAGS}/{suggested.id}")

    assert by_hand.status_code == 409, by_hand.text
    assert "approve" in by_hand.json()["detail"]
    # A word the instance turned down gets the other answer: not "wait", but "no".
    assert refused.status_code == 409, refused.text
    assert "rejected" in refused.json()["detail"]
    assert completion.json()["data"] == []
    assert listed.json()["data"] == []
    assert [one["name"] for one in for_admins.json()["data"]] == ["wombat"]
    # Readable by id: a member whose file carries a suggestion has to be able to see what it is.
    assert readable.status_code == 200
    assert readable.json()["status"] == "suggested"


async def test_the_endpoint_refuses_what_it_cannot_answer(
    identity_settings: Settings, identity_database: str
) -> None:
    """The request-shaped refusals, in one place.

    `GET /tags` is two endpoints wearing one path, so the combinations that mean nothing have to
    say so rather than silently picking a mode: a completion has no cursor and no status, and a
    listing cursor from somewhere else is not a position in this one.
    """
    async with instance(identity_settings) as app, signed_in(app) as client:
        paged_completion = await client.get(TAGS, params={"prefix": "in", "cursor": "whatever"})
        filtered_completion = await client.get(TAGS, params={"prefix": "in", "status": "all"})
        bad_cursor = await client.get(TAGS, params={"cursor": "not-a-cursor"})
        unknown_parent = await create_tag(
            client, "invoice", parents=["0198c0de-0000-7000-8000-000000000000"]
        )
        missing = await update_tag(client, "0198c0de-0000-7000-8000-000000000000", name="anything")
        everything = await client.get(TAGS, params={"status": "all"})

    assert paged_completion.status_code == 422, paged_completion.text
    assert filtered_completion.status_code == 422
    assert bad_cursor.status_code == 422
    assert unknown_parent.status_code == 422, unknown_parent.text
    assert unknown_parent.json()["errors"][0]["pointer"] == "/parents"
    assert missing.status_code == 404
    assert everything.json()["data"] == [], "nothing was created along the way"


@pytest.mark.fr("F-003/FR-10")
async def test_a_tag_something_carries_cannot_be_erased(
    identity_settings: Settings, identity_database: str
) -> None:
    """Hard deletion is for a typo with no history; anything applied is refused (ADR-0006)."""
    async with workspace_ready(identity_settings, identity_database) as (client, workspace, _):
        typo = await added(client, "invoiec")
        used = await added(client, "invoice")
        created = await create_upload(client, workspace, "Photos/one.txt", body=b"a")
        await tag_file(client, created.json()["id"], tag=used)

        erased = await client.delete(f"{TAGS}/{typo}", headers=SAME_ORIGIN)
        refused = await client.delete(f"{TAGS}/{used}", headers=SAME_ORIGIN)
        missing = await client.delete(
            f"{TAGS}/0198c0de-0000-7000-8000-000000000000", headers=SAME_ORIGIN
        )
        listed = await client.get(TAGS)

    assert erased.status_code == 204, erased.text
    assert refused.status_code == 409, refused.text
    assert "1 file(s)" in refused.json()["detail"]
    assert missing.status_code == 404
    assert [one["name"] for one in listed.json()["data"]] == ["invoice"]
