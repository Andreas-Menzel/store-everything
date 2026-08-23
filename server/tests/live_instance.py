"""A real instance on a real port, with a real worker — for the tests that need sockets.

Everything else in this suite drives the application in-process, which is faster and enough. Two
things are not: the extractor SDK is deliberately **synchronous** (an extractor's work is
blocking, so an event loop would be a tax on every author — `extractors/src/se_extractor`), and a
synchronous client cannot speak to an ASGI application in the test's own loop. And the
conformance kit is a command-line tool pointed at an instance, which is the thing it should be
tested as.

So this runs the honest arrangement: uvicorn in one thread, a worker in another, both with their
own event loops, and the test talking to them over HTTP like anything else would. It is the only
place in the suite where a test is a client rather than a caller.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import uvicorn

from store_everything import handlers
from store_everything.app import create_app
from store_everything.config import Settings
from store_everything.db import create_engine
from store_everything.runner import Runner

#: Long enough for a loaded laptop, short enough that a hung server fails the test rather than
#: the suite.
_STARTUP_TIMEOUT = 30.0
_SHUTDOWN_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class LiveInstance:
    """Where to reach the instance under test."""

    base_url: str


@contextmanager
def live_instance(settings: Settings) -> Generator[LiveInstance]:
    """An instance serving HTTP with a worker running behind it.

    The worker is what makes this a *usable* instance rather than an API in front of a queue
    nothing drains: creating a workspace is an operation, and without a worker it never becomes
    active (10 § topology).
    """
    application = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=0, log_level="critical", lifespan="on")
    )

    serving = threading.Thread(target=server.run, name="live-api", daemon=True)
    serving.start()
    port = _await_port(server)

    stop = threading.Event()
    working = threading.Thread(target=_work, args=(settings, stop), name="live-worker", daemon=True)
    working.start()

    try:
        yield LiveInstance(base_url=f"http://127.0.0.1:{port}")
    finally:
        stop.set()
        server.should_exit = True
        serving.join(timeout=_SHUTDOWN_TIMEOUT)
        working.join(timeout=_SHUTDOWN_TIMEOUT)


def _await_port(server: uvicorn.Server) -> int:
    """The port the kernel chose, once the socket is actually listening."""
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if server.started and server.servers:
            sockets = server.servers[0].sockets
            if sockets:
                return int(sockets[0].getsockname()[1])
        time.sleep(0.02)
    raise TimeoutError("the live instance did not start listening")


def _work(settings: Settings, stop: threading.Event) -> None:
    """One worker, in its own loop, ended by the event rather than by cancellation."""

    async def loop() -> None:
        engine = create_engine(settings)
        runner = Runner(engine, settings, handlers.registry(settings))
        try:
            await handlers.install_schedules(engine, settings)
            claiming = asyncio.create_task(runner.run_forever())
            # A `threading.Event` from another loop cannot be awaited, so it is polled — which
            # is what crossing two event loops costs, and 50 ms of it is nobody's bottleneck.
            while not stop.is_set():  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            runner.stop()
            await asyncio.wait_for(claiming, timeout=_SHUTDOWN_TIMEOUT)
        finally:
            await engine.dispose()

    asyncio.run(loop())
