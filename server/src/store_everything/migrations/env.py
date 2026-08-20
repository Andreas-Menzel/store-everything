"""Alembic environment.

Migrations run synchronously — they are a startup/maintenance step, not request work —
even though the service itself talks to PostgreSQL asynchronously.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from store_everything.config import load_settings
from store_everything.db import configured_url
from store_everything.tables import metadata

config = context.config
target_metadata = metadata

# Only when Alembic was invoked from its CLI: a programmatic config (the readiness check,
# the test suite) carries no file name and must not reconfigure the service's logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    """Prefer the URL handed in programmatically; fall back to the environment."""
    return configured_url(config) or load_settings().sqlalchemy_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
