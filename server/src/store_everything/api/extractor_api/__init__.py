"""The extractor-facing API (`extractor-api/v1`) — the plugin boundary of ADR-0002."""

from __future__ import annotations

from store_everything.tables import EXTRACTOR_API_VERSION

#: Where the contract lives. Here rather than in `router.py` so that the modules serving the
#: endpoints can build URLs into it without importing the router that imports them.
EXTRACTOR_API_PREFIX = f"/extractor-api/{EXTRACTOR_API_VERSION}"
