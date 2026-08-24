"""The conformance kit, against every extractor this repository ships.

The [ROADMAP](../../ROADMAP.md)'s phase-2 exit criterion asks for the kit to be green "against
every official extractor image *and* the reference extractor". The reference extractor has its own
file (`test_reference_extractor.py`, where the kit also validates itself); this is the other half.

It is not a formality. The image checks assert that an extractor registers, that its manifest is
*coherent* — no output kind declared without the names that go with it, no rendition nobody owns —
that it claims a file it said it accepts and finishes the job, and that it leaves alone a type it
never claimed. Those are precisely the mistakes a manifest edit makes, and they are invisible until
something asks.

A check the kit cannot run here reports `skip` with its reason rather than a pass nobody earned:
`tesseract-ocr` routes on a predicate that only another extractor's result can satisfy, and no
fixture upload can fake that.
"""

from __future__ import annotations

from typing import Any

import pytest
from se_extractor import basic_metadata, pdf_pages, pdf_text, preview_gen, text_plain
from se_extractor import tesseract_ocr as ocr
from se_extractor.conformance import Conformance, run_checks

from store_everything.config import Settings
from tests.identity_helpers import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.live_helpers import active_workspace, provision, running, signed_in
from tests.live_instance import live_instance

pytestmark = [pytest.mark.integration]

#: Every extractor this repository ships, with the handler its image runs. `MANIFEST` rather than
#: `build_manifest()` for OCR: what is being checked is the manifest as committed, and the engine's
#: version is not what these checks are about.
OFFICIAL = (
    (preview_gen.EXTRACTOR_ID, preview_gen.MANIFEST, preview_gen.handle),
    (pdf_pages.EXTRACTOR_ID, pdf_pages.MANIFEST, pdf_pages.handle),
    (basic_metadata.EXTRACTOR_ID, basic_metadata.MANIFEST, basic_metadata.handle),
    (pdf_text.EXTRACTOR_ID, pdf_text.MANIFEST, pdf_text.handle),
    (text_plain.EXTRACTOR_ID, text_plain.MANIFEST, text_plain.handle),
    (ocr.EXTRACTOR_ID, ocr.MANIFEST, ocr.handle),
)


@pytest.mark.parametrize(
    ("identifier", "manifest", "handler"), OFFICIAL, ids=[one[0] for one in OFFICIAL]
)
def test_the_conformance_kit_passes_against_every_official_extractor(
    identity_settings: Settings,
    identifier: str,
    manifest: dict[str, Any],
    handler: Any,
) -> None:
    with (
        live_instance(identity_settings) as instance,
        signed_in(instance.base_url) as admin,
    ):
        active_workspace(admin, f"Conformance {identifier}")
        token = provision(admin, identifier)
        with (
            running(instance.base_url, token, manifest, handler),
            Conformance(
                instance.base_url,
                email=ADMIN_EMAIL,
                password=ADMIN_PASSWORD,
                timeout=45.0,
            ) as conformance,
        ):
            report = run_checks(conformance, extractor_id=identifier, protocol=False)

    failures = [check for check in report.checks if check.outcome == "fail"]
    assert not failures, f"{identifier}:\n{report.render()}"
    # The two every image must actually pass; the other checks may legitimately skip.
    passed = {check.name for check in report.checks if check.outcome == "pass"}
    assert {"the-extractor-registers", "its-manifest-is-coherent"} <= passed, report.render()
