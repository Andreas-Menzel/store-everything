# Core service

The API service and ingestion orchestrator ([ADR-0012](../decisions/ADR-0012-python-fastapi-core-stack.md)).
Run every command from this directory; `make` targets in the repository root wrap them.

```bash
uv sync --all-groups      # install (creates .venv, uses the committed lockfile)
uv run ruff check .       # lint
uv run ruff format .      # format
uv run pyright            # types
uv run pytest             # tests (integration tests start a throwaway PostgreSQL)
uv run alembic upgrade head   # apply migrations
uv run python -m store_everything   # serve
```

Configuration comes from the environment, prefixed `SE_` — see [`.env.example`](../.env.example).
`SE_DATABASE_URL` is required; there is deliberately no default datastore.

Integration tests and `alembic` need Docker running and a reachable PostgreSQL respectively.
Tests that need a database are marked `integration`; `uv run pytest -m "not integration"` skips them.
