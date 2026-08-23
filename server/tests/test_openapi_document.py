"""The committed contracts must match the code that serves them."""

from __future__ import annotations

import re
from typing import Any, cast

import pytest
from tools.export_openapi import (
    EXTRACTOR_OPENAPI_PATH,
    OPENAPI_PATH,
    build_document,
    build_extractor_document,
    render,
)

from store_everything.api.extractor_api.router import EXTRACTOR_API_PREFIX
from store_everything.app import API_VERSION
from store_everything.tables import EXTRACTOR_API_VERSION


def test_committed_document_is_current() -> None:
    """Guards the generated clients: they are built from this file, not from the app."""
    assert OPENAPI_PATH.exists(), "openapi.json is missing — run `make openapi`"

    assert OPENAPI_PATH.read_text(encoding="utf-8") == render(build_document()), (
        "openapi.json is out of date with the code — run `make openapi`"
    )


def test_committed_extractor_contract_is_current() -> None:
    """The artefact a third-party extractor author generates from (ADR-0020)."""
    assert EXTRACTOR_OPENAPI_PATH.exists(), "openapi-extractor.json is missing — run `make openapi`"

    assert EXTRACTOR_OPENAPI_PATH.read_text(encoding="utf-8") == render(
        build_extractor_document()
    ), "openapi-extractor.json is out of date with the code — run `make openapi`"


def test_the_two_contracts_are_disjoint() -> None:
    """One document per audience, and neither leaks into the other.

    The user-facing document is what a client of this product calls, and an extractor is not
    one: its routes are mounted `include_in_schema=False`, so this asserts the consequence
    rather than the mechanism. The reverse direction matters just as much — an extractor image
    must not be generated against endpoints that expect a user credential.
    """
    user_facing = set(cast(dict[str, object], build_document()["paths"]))
    extractor_facing = set(cast(dict[str, object], build_extractor_document()["paths"]))

    assert extractor_facing, "the extractor contract describes no endpoints"
    assert user_facing & extractor_facing == set()
    assert not any(path.startswith(EXTRACTOR_API_PREFIX) for path in user_facing)
    assert all(path.startswith(EXTRACTOR_API_PREFIX) for path in extractor_facing)


def test_the_extractor_contract_carries_its_own_version() -> None:
    """Versioned independently of `/api/v1` and of the app's SemVer (05 § principles)."""
    document = build_extractor_document()

    assert document["info"]["version"] == EXTRACTOR_API_VERSION
    assert document["info"]["version"] != API_VERSION


def test_export_is_deterministic() -> None:
    """Two exports must be byte-identical, or the drift check would be noise."""
    assert render(build_document()) == render(build_document())


def test_document_describes_the_public_and_authenticated_surface() -> None:
    paths = build_document()["paths"]

    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/api/v1/openapi.json" in paths


def _operation_ids(document: dict[str, object]) -> list[str]:
    paths = cast(dict[str, dict[str, dict[str, Any]]], document["paths"])
    return [
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if "operationId" in operation
    ]


def test_operation_ids_are_unique() -> None:
    """A collision makes the document invalid and silently breaks client generation."""
    ids = _operation_ids(build_document())

    assert len(ids) == len(set(ids)), sorted(ids)


def test_operation_ids_are_readable() -> None:
    """They become function names in every generated client.

    Asserted as a property rather than as a list of names: a list would have to be edited
    every time an endpoint is added, and an assertion nobody can read stops being read.
    What must never come back is FastAPI's default shape (`healthz_healthz_get`).
    """
    ids = _operation_ids(build_document())

    assert len(ids) > 10, "expected the identity surface to be published"
    for operation_id in ids:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", operation_id), operation_id
        assert not re.search(r"_(get|post|patch|put|delete)$", operation_id), operation_id

    # Anchors: one from each router, so a wholesale renaming still fails here.
    assert {"healthz", "openapi_schema", "login", "list_users"} <= set(ids)


def test_the_contract_version_is_the_api_major_not_the_app_release() -> None:
    """08-api-principles.md keeps the version lines independent.

    Embedding the app's SemVer here would rewrite openapi.json and every generated
    client on each release, and would tell clients a version they cannot act on.
    """
    assert build_document()["info"]["version"] == API_VERSION
    assert API_VERSION == "1"


@pytest.mark.fr("F-011/FR-2")
def test_no_operation_offers_to_change_or_remove_an_event() -> None:
    """The API half of append-only, read off the published contract.

    The database refuses an `UPDATE` (`test_identity_api.py`), which is the half that survives a
    bug in the code. This is the half that has to survive phase 4: when FR-5's query API arrives,
    a `DELETE` added beside it would pass every other test in this suite. Asserted against the
    contract rather than the route table because the contract is what a client is offered.
    """
    paths = cast(dict[str, dict[str, object]], build_document()["paths"])

    def mutating(needle: str) -> set[tuple[str, str]]:
        return {
            (path, method)
            for path, operations in paths.items()
            if needle in path
            for method in operations
            if method in {"post", "put", "patch", "delete"}
        }

    # The search finds a mutating operation when there is one, so the empty result below is a
    # statement about the API and not about this comprehension.
    assert mutating("files") != set()
    assert mutating("event") == set()
