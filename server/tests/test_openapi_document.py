"""The committed contract must match the code that serves it."""

from __future__ import annotations

from typing import Any, cast

from tools.export_openapi import OPENAPI_PATH, build_document, render


def test_committed_document_is_current() -> None:
    """Guards the generated clients: they are built from this file, not from the app."""
    assert OPENAPI_PATH.exists(), "openapi.json is missing — run `make openapi`"

    assert OPENAPI_PATH.read_text(encoding="utf-8") == render(build_document()), (
        "openapi.json is out of date with the code — run `make openapi`"
    )


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
    """They become function names in every generated client."""
    assert set(_operation_ids(build_document())) == {"healthz", "readyz", "openapi_schema"}
