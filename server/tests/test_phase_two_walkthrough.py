"""The phase-2 exit criterion, walked end to end in one test.

Every step is covered in detail elsewhere — thumbnails in `test_thumbnails.py`, previews in
`test_previews.py`, text in `test_document_text.py`, tag provenance in `test_auto_tags.py`. This
exists because the [ROADMAP](../../ROADMAP.md)'s phase-2 exit criterion is not a list of features
but a *path*:

    upload a half-scanned document → a thumbnail at the fixed tier set, with a placeholder in the
    listing → a preview descriptor naming its pages → page 2 rendered on demand → its own text
    layer read on page 1 → OCR on page 2, because the first extractor said so → a searchable PDF
    to download → and, on a second file, a machine's label arriving as a quarantined suggestion
    that a person confirms

and a suite of green units can all pass while the path between them is broken: a thumbnail that
exists but is not offered in the listing, a chained extractor that never receives its parameters,
a re-run that resurrects a rejected tag. So the walk is asserted as a walk, in order, on one
instance — with **five real extractors** running as their own threads of control, because the
claims are about what containers produce.

Its other job is to be the answer to "what does this thing do now?", so it is meant to be read top
to bottom.

The OCR steps need the engine. Where it is absent (`tools/tesseract-in-docker.sh` is one way to
have it), those steps are skipped and the rest of the walk still runs — a partial walk beats no
walk, and CI has the engine.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from typing import Any

import pymupdf
import pytest
from se_extractor import basic_metadata, pdf_pages, pdf_text, preview_gen, reference
from se_extractor import tesseract_ocr as ocr

from store_everything.api import API_V1_PREFIX
from store_everything.config import Settings
from tests.live_helpers import (
    active_workspace,
    await_run,
    facts,
    provision,
    running,
    segments,
    signed_in,
    upload,
)
from tests.live_instance import live_instance
from tests.test_document_text import half_scanned_document

pytestmark = [pytest.mark.integration]

HAS_ENGINE = shutil.which("tesseract") is not None

#: The fixed tier set of [F-028/FR-1](../../features/F-028-thumbnails-and-previews.md).
TIERS = (256, 512, 1024)


def test_the_phase_two_path_works_from_one_end_to_the_other(
    identity_settings: Settings,
) -> None:
    document = half_scanned_document()

    with live_instance(identity_settings) as instance, signed_in(instance.base_url) as admin:
        workspace = active_workspace(admin, "Archive")

        # ---------------------------------------------------------------- install the extractors
        # An operator's five commands, each minting one credential. Nothing is running yet.
        tokens = {
            identifier: provision(admin, identifier)
            for identifier in (
                preview_gen.EXTRACTOR_ID,
                pdf_pages.EXTRACTOR_ID,
                basic_metadata.EXTRACTOR_ID,
                pdf_text.EXTRACTOR_ID,
                ocr.EXTRACTOR_ID,
                reference.EXTRACTOR_ID,
            )
        }

        with (
            running(
                instance.base_url,
                tokens[preview_gen.EXTRACTOR_ID],
                preview_gen.MANIFEST,
                preview_gen.handle,
            ),
            running(
                instance.base_url,
                tokens[pdf_pages.EXTRACTOR_ID],
                pdf_pages.MANIFEST,
                pdf_pages.handle,
            ),
            running(
                instance.base_url,
                tokens[basic_metadata.EXTRACTOR_ID],
                basic_metadata.MANIFEST,
                basic_metadata.handle,
            ),
            running(
                instance.base_url,
                tokens[pdf_text.EXTRACTOR_ID],
                pdf_text.MANIFEST,
                pdf_text.handle,
            ),
            running(
                instance.base_url,
                tokens[ocr.EXTRACTOR_ID],
                ocr.build_manifest(),
                ocr.handle,
            ),
            running(
                instance.base_url,
                tokens[reference.EXTRACTOR_ID],
                # Narrowed to text on purpose. The double emits one segment per line of whatever
                # it is given, which is a useful sample for a note and thousands of segments of
                # mojibake for a PDF — its `*/*` manifest is right for its own tests and wrong
                # here, and a manifest is data.
                {**reference.build_manifest("verify"), "accepts": {"mime_types": ["text/*"]}},
                lambda job, context: reference.handle(job, context, mode="verify", delay=0.0),
            ),
        ):
            # ------------------------------------------------------------ upload
            created = upload(admin, workspace, "2024/scan.pdf", document, "application/pdf")
            identifier = str(created["id"])
            assert created["extraction_status"] == "pending"

            # ------------------------------------------------------------ pictures of it
            await_run(admin, identifier, preview_gen.EXTRACTOR_ID)
            thumbnails = {size: admin.get(_thumbnail(identifier, size)) for size in TIERS}
            # A size nobody rendered snaps *up* to one that exists, so a client asking for 300
            # gets the 512 rather than a 404 (F-028/FR-1).
            snapped = admin.get(_thumbnail(identifier, 300))

            # The listing a person actually sees: the folder the upload created, not a flat
            # index. A row has to be paintable from this response alone (F-028/FR-5).
            listed = _children(admin, _descend(admin, workspace, "2024"))

            # ------------------------------------------------------------ pages of it
            await_run(admin, identifier, pdf_pages.EXTRACTOR_ID)
            descriptor = admin.get(f"{API_V1_PREFIX}/files/{identifier}/preview")
            # Page 2 was never rendered eagerly: asking is what queues it (F-028/FR-7).
            queued = admin.get(f"{API_V1_PREFIX}/files/{identifier}/preview/pages/2")
            page_two = _await_page(admin, identifier, 2)

            # ------------------------------------------------------------ what it says
            await_run(admin, identifier, pdf_text.EXTRACTOR_ID)
            known = facts(admin, identifier)
            assert known["has_text_layer"] is True
            assert known["needs_ocr"] is True
            assert known["ocr_pages"] == [2]

            if HAS_ENGINE:
                # Nothing told OCR about this file: `pdf-text` wrote `needs_ocr`, and the OCR
                # manifest's predicate is what turned that into a job (ADR-0020).
                ocr_run = await_run(admin, identifier, ocr.EXTRACTOR_ID)
                assert ocr_run["state"] == "succeeded", ocr_run["error"]
                searchable = admin.get(
                    f"{API_V1_PREFIX}/files/{identifier}/renditions/searchable-pdf"
                )
            else:
                searchable = None

            found = segments(admin, identifier)

            # ------------------------------------------------------------ tags a machine proposed
            # A second file, because the label story is about labels: an extractor proposes, and
            # the instance's vocabulary decides whether that is a tag it knows (F-003/FR-11).
            note = upload(
                admin,
                workspace,
                "2024/note.txt",
                b"A note worth keeping.\nIt mentions xylophone marmalade.\n",
                "text/plain",
            )
            reference_run = await_run(admin, str(note["id"]), reference.EXTRACTOR_ID)
            assert reference_run["state"] == "succeeded", reference_run["error"]
            claimed = _proposed(admin, str(note["id"]))
            # A word no vocabulary of ours knew cannot be confirmed yet — the taxonomy is
            # admin-governed, so admitting the word is a separate, deliberate act (F-003/FR-12).
            premature = admin.post(
                f"{API_V1_PREFIX}/files/{note['id']}/tags/{claimed['id']}/confirm"
            )
            admitted = admin.post(f"{API_V1_PREFIX}/tags/{claimed['id']}/approve")
            confirmed = admin.post(
                f"{API_V1_PREFIX}/files/{note['id']}/tags/{claimed['id']}/confirm"
            )
            content = admin.get(f"{API_V1_PREFIX}/files/{identifier}/content")

        # -------------------------------------------------------------------- the thumbnails
        for size, response in thumbnails.items():
            assert response.status_code == 200, f"{size}px: {response.text}"
            assert response.headers["content-type"] == "image/webp"
        assert snapped.status_code == 200
        assert snapped.content == thumbnails[512].content

        # A row can be painted before any image arrives, from the listing alone (F-028/FR-5).
        row = next(one for one in listed if one["id"] == identifier)
        assert row["has_thumbnail"] is True
        assert row["placeholder_hash"]

        # -------------------------------------------------------------------- the pages
        described = descriptor.json()
        assert described["pages"] == 2
        assert described["pages_url"]
        assert queued.status_code in {200, 202}
        assert page_two.status_code == 200
        assert page_two.headers["content-type"] == "image/webp"

        # -------------------------------------------------------------------- the text
        by_page = {span["anchor"]["page"]: span for span in found}
        assert by_page[1]["extractor"] == pdf_text.EXTRACTOR_ID
        assert "fixture page one" in by_page[1]["text"]

        if HAS_ENGINE:
            assert by_page[2]["extractor"] == ocr.EXTRACTOR_ID
            assert "xylophone marmalade" in by_page[2]["text"]
            assert by_page[2]["confidence"] > 0.8
            assert searchable is not None
            assert searchable.status_code == 200
            enriched = pymupdf.open(stream=searchable.content)
            assert "xylophone marmalade" in enriched[1].get_text()
        else:
            assert sorted(by_page) == [1]

        # -------------------------------------------------------------------- the tag
        # Quarantined on arrival, because no vocabulary of ours knew the word — and a person
        # saying yes is what turns a machine's guess into a curation decision (ADR-0004).
        assert claimed["status"] == "suggested"
        assert claimed["source"]["extractor"] == reference.EXTRACTOR_ID
        # Quarantine is enforced, not advisory: confirming an unadmitted word is refused, with a
        # typed conflict rather than a silent success.
        assert premature.status_code == 409
        assert admitted.status_code == 200
        assert admitted.json()["status"] == "active"
        assert confirmed.status_code == 200
        assert confirmed.json()["provenance"] == "confirmed"

        # -------------------------------------------------------------------- and the original
        # Every step above read this file, rendered it, OCR'd it and tagged it. None of them
        # touched it (02 § invariant 2).
        assert content.content == document
        assert hashlib.sha256(content.content).hexdigest() == created["content_hash"]


def _children(client: Any, folder_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{API_V1_PREFIX}/folders/{folder_id}/children")
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


def _descend(client: Any, workspace: str, *names: str) -> str:
    """Walk down from a workspace's root by name, the way a person clicking through does."""
    folder_id = str(client.get(f"{API_V1_PREFIX}/workspaces/{workspace}").json()["root_folder"])
    for name in names:
        entries = _children(client, folder_id)
        matched = [entry for entry in entries if entry["name"] == name]
        assert matched, f"no {name!r} among {[entry['name'] for entry in entries]}"
        folder_id = str(matched[0]["id"])
    return folder_id


def _thumbnail(file_id: str, size: int) -> str:
    return f"{API_V1_PREFIX}/files/{file_id}/thumbnail?size={size}"


def _await_page(client: Any, file_id: str, page: int) -> Any:
    """Ask for a page until it is there — the second request is the one that serves bytes."""
    deadline = time.monotonic() + 60.0
    response = None
    while time.monotonic() < deadline:
        response = client.get(f"{API_V1_PREFIX}/files/{file_id}/preview/pages/{page}")
        if response.status_code == 200:
            return response
        time.sleep(1.0)
    last = response.status_code if response is not None else "no response"
    raise AssertionError(f"page {page} never arrived (last: {last})")


def _proposed(client: Any, file_id: str) -> dict[str, Any]:
    """The first machine claim on this file, whichever extractor got there first."""
    deadline = time.monotonic() + 60.0
    last: Any = None
    while time.monotonic() < deadline:
        tags = client.get(f"{API_V1_PREFIX}/files/{file_id}/tags").json()
        machine = [one for one in tags if one["provenance"] == "auto"]
        if machine:
            return dict(machine[0])
        last = tags
        time.sleep(1.0)
    raise AssertionError(f"no extractor proposed a tag; the file had {last}")
