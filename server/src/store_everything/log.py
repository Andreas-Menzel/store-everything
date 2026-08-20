"""Structured JSON logging to stdout.

The app never writes or rotates log files — persistence is the platform's concern. Every
line carries the request id, which is the only bridge between a client-visible error and
its internal cause (10-deployment-and-operations.md § logging).

Logs never contain secrets, tokens, file contents, search queries, or result snippets.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, override

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    | {"message", "asctime", "taskName"}
    # uvicorn attaches an ANSI-coloured duplicate of the message; it belongs on a terminal,
    # not in a structured log record.
    | {"color_message"}
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: the shape every log consumer can rely on."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Install the JSON formatter on the root logger and quiet uvicorn's own access log.

    Access logging is ours (`middleware.RequestContextMiddleware`) so that every request
    line carries the request id in the same structured shape.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Alembic narrates itself at INFO: plugin registration once at import, and two lines
    # per schema-version read — which the readiness probe performs every few seconds.
    # None of it is operator-relevant inside the service; migrations run from the CLI keep
    # their own logging (see migrations/env.py).
    logging.getLogger("alembic").setLevel(logging.WARNING)

    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers.clear()
    uvicorn_access.propagate = False
    uvicorn_access.disabled = True

    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
