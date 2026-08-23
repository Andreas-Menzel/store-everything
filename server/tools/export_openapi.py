"""Write the OpenAPI documents that every generated client is built from.

Committed so that an API change is visible in the diff of the change that causes it, and so
client generation never depends on a running server (08-api-principles.md § API-first).

**Two documents, two audiences** (ADR-0020): `openapi.json` is what a client of this product
calls, and `openapi-extractor.json` is the contract an extractor image implements — separately
versioned, and the artefact a third-party author reads instead of pointing a generator at
somebody's instance.

    python -m tools.export_openapi            # rewrite both documents
    python -m tools.export_openapi --check    # fail if either is out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from store_everything.api.extractor_api.router import extractor_api_document
from store_everything.app import create_app
from store_everything.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "openapi.json"
EXTRACTOR_OPENAPI_PATH = REPO_ROOT / "openapi-extractor.json"


def build_document() -> dict[str, Any]:
    """Build the user-facing document from fixed settings.

    The result depends only on the code, never on the environment of whoever exports it.
    No database is contacted while a schema is built.
    """
    settings = Settings(
        database_url=SecretStr("postgresql://schema:schema@localhost:5432/schema"),
        api_docs_enabled=True,
        log_level="CRITICAL",
    )
    return create_app(settings).openapi()


def build_extractor_document() -> dict[str, Any]:
    """Build the extractor contract. Independent of settings — it has no optional routes."""
    return extractor_api_document()


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed document is out of date",
    )
    args = parser.parse_args(argv)

    documents = (
        (OPENAPI_PATH, render(build_document())),
        (EXTRACTOR_OPENAPI_PATH, render(build_extractor_document())),
    )

    if args.check:
        stale = [
            path
            for path, expected in documents
            if (path.read_text(encoding="utf-8") if path.exists() else "") != expected
        ]
        for path in stale:
            print(f"{path} is out of date with the code. Run: make openapi", file=sys.stderr)
        return 1 if stale else 0

    for path, expected in documents:
        path.write_text(expected, encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
