"""Pure-ASGI middleware: request correlation and content security headers.

Written against the raw ASGI interface rather than Starlette's `BaseHTTPMiddleware`,
which buffers responses and interferes with streaming — and this service will stream file
bytes and Range responses (08-api-principles.md § downloads).
"""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from store_everything.log import request_id_var
from store_everything.problems import internal_error_response

REQUEST_ID_HEADER = "x-request-id"

_logger = logging.getLogger(__name__)

_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
)


def new_request_id() -> str:
    """Mint a request id.

    Always server-generated, never taken from a client header: an attacker-chosen
    correlation id would let log lines be forged or collided.
    """
    return f"req_{uuid4().hex}"


class RequestContextMiddleware:
    """Assign a request id, expose it, log one line per request, and contain failures.

    Unexpected exceptions are converted here — inside the request-id scope — so the `500`
    problem carries an `instance` the operator can grep for.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers.append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                _logger.exception("unhandled exception")
                if response_started:
                    # Headers are already on the wire; the connection is the only signal left.
                    raise
                status = 500
                await internal_error_response()(scope, receive, send_wrapper)
        finally:
            _logger.info(
                "request",
                extra={
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """Content security headers owned by the app; transport headers live at the edge."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS:
                    headers.append(name, value)
            await send(message)

        await self.app(scope, receive, send_wrapper)
