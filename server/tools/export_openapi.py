"""Write the OpenAPI document that every generated client is built from.

The document is committed so that an API change is visible in the diff of the change
that causes it, and so client generation never depends on a running server
(08-api-principles.md § API-first).

    python -m tools.export_openapi            # rewrite openapi.json
    python -m tools.export_openapi --check    # fail if it is out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from store_everything.app import create_app
from store_everything.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "openapi.json"


def build_document() -> dict[str, Any]:
    """Build the document from fixed settings.

    The result depends only on the code, never on the environment of whoever exports it.
    No database is contacted while a schema is built.
    """
    settings = Settings(
        database_url=SecretStr("postgresql://schema:schema@localhost:5432/schema"),
        api_docs_enabled=True,
        log_level="CRITICAL",
    )
    return create_app(settings).openapi()


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

    expected = render(build_document())

    if args.check:
        current = OPENAPI_PATH.read_text(encoding="utf-8") if OPENAPI_PATH.exists() else ""
        if current != expected:
            print(
                f"{OPENAPI_PATH} is out of date with the code. Run: make openapi",
                file=sys.stderr,
            )
            return 1
        return 0

    OPENAPI_PATH.write_text(expected, encoding="utf-8")
    print(f"wrote {OPENAPI_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
