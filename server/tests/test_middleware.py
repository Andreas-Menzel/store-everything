"""Middleware behaviour that HTTP-level tests cannot reach."""

from __future__ import annotations

import pytest
from starlette.types import Message, Receive, Scope, Send

from store_everything.middleware import RequestContextMiddleware, SecurityHeadersMiddleware


async def _no_messages() -> Message:  # pragma: no cover - never awaited in these tests
    raise AssertionError("receive() should not be called")


async def _discard(message: Message) -> None:  # pragma: no cover - nothing is sent
    raise AssertionError("send() should not be called")


@pytest.mark.parametrize(
    "middleware_class",
    [RequestContextMiddleware, SecurityHeadersMiddleware],
)
@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
@pytest.mark.asyncio
async def test_non_http_scopes_pass_straight_through(
    middleware_class: type[RequestContextMiddleware] | type[SecurityHeadersMiddleware],
    scope_type: str,
) -> None:
    """Startup and WebSocket traffic must not be treated as a request-response pair.

    Rewriting headers or minting a request id for these scopes would break the app's
    ability to start at all — and WebSockets arrive with F-012.
    """
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    await middleware_class(inner)({"type": scope_type}, _no_messages, _discard)

    assert seen == [scope_type]
