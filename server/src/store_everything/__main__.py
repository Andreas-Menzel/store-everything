"""Process entrypoint: `python -m store_everything`, or the `store-everything` command.

Three subcommands. `serve` (the default) runs the API; `worker` runs the operation loop that
executes queued work (12-reliability.md); `create-admin` creates the first administrator on
an instance that has none, for operators who would rather not put a password in the
environment (07-identity-permissions-sharing.md § users).

The API and the worker are separate processes on purpose: background work must never be able
to starve request handling of CPU, and the two scale independently
(10-deployment-and-operations.md § topology).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys

import uvicorn

from store_everything import bootstrap, handlers, passwords
from store_everything.config import Settings, load_settings
from store_everything.db import create_engine
from store_everything.log import configure_logging
from store_everything.runner import Runner


def serve(settings: Settings) -> None:
    uvicorn.run(
        "store_everything.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        # `X-Forwarded-*` is honoured only from explicitly configured proxy addresses;
        # a spoofed client IP would poison rate limiting and audit records (ADR-0009).
        proxy_headers=settings.trust_proxy_headers,
        forwarded_allow_ips=settings.forwarded_allow_ips if settings.trust_proxy_headers else None,
        # Logging is configured above; uvicorn must not install its own handlers.
        log_config=None,
        access_log=False,
    )


_logger = logging.getLogger(__name__)


async def _work(settings: Settings) -> int:
    """Run the operation loop until the process is asked to stop.

    SIGTERM stops claiming and lets in-flight work finish; it is an optimization for restart
    speed, not a correctness mechanism. A `kill -9` here is equally safe — the leases simply
    expire and another worker reclaims (ADR-0010).
    """
    engine = create_engine(settings)
    runner = Runner(engine, settings, handlers.registry())

    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, runner.stop)

    try:
        # Nothing here touches a table until the schema exists: on a fresh install the stack
        # comes up before migrations are applied, and a worker that crashed instead of
        # waiting would restart-loop through the whole first-run window.
        if not await runner.wait_until_ready():
            return 0
        await handlers.install_schedules(engine, settings)
        await runner.run_forever()
    finally:
        await engine.dispose()
    return 0


def work(settings: Settings) -> int:
    return asyncio.run(_work(settings))


async def _create_admin(settings: Settings, email: str, password: str) -> int:
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            user = await bootstrap.create_first_admin(connection, email=email, password=password)
            await connection.commit()
    finally:
        await engine.dispose()

    if user is None:
        print(
            "This instance already has accounts; ask an administrator to create another.",
            file=sys.stderr,
        )
        return 1

    print(f"Created administrator {user.email}.")
    return 0


def create_admin(settings: Settings, email: str) -> int:
    """Prompt for a password (never echoed, never taken from argv) and create the admin.

    A password passed as an argument would land in the shell history and in the process
    list, so it is prompted for even when that is less convenient.
    """
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        print("The passwords do not match.", file=sys.stderr)
        return 1

    try:
        passwords.check_policy(password)
    except passwords.WeakPasswordError as weak:
        print(str(weak), file=sys.stderr)
        return 1

    return asyncio.run(_create_admin(settings, email, password))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="store-everything", description=__doc__)
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("serve", help="run the API service (default)")
    subcommands.add_parser("worker", help="run the operation loop")
    admin = subcommands.add_parser("create-admin", help="create the first administrator")
    admin.add_argument("email", help="the administrator's email address")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if arguments.command == "create-admin":
        return create_admin(settings, arguments.email)

    if arguments.command == "worker":
        return work(settings)

    serve(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
