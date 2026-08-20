"""Store Everything — core API service and ingestion orchestrator."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:  # pragma: no cover - trivial packaging fallback
    __version__ = version("store-everything")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = ["__version__"]
