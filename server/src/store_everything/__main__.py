"""Process entrypoint: `python -m store_everything`, or the `store-everything` command.

Two subcommands. `serve` (the default) runs the API; `create-admin` creates the first
administrator on an instance that has none, for operators who would rather not put a
password in the environment (07-identity-permissions-sharing.md § users).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

import uvicorn

from store_everything import bootstrap, passwords
from store_everything.config import Settings, load_settings
from store_everything.db import create_engine
from store_everything.log import configure_logging


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
    admin = subcommands.add_parser("create-admin", help="create the first administrator")
    admin.add_argument("email", help="the administrator's email address")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if arguments.command == "create-admin":
        return create_admin(settings, arguments.email)

    serve(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
