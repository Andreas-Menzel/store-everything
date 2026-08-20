# syntax=docker/dockerfile:1

# Build stage: resolve and install exactly what the lockfile pins.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: they change far less often than the source, so this layer
# survives most rebuilds.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


# Runtime stage: no build tools, no uv, no dev dependencies.
FROM python:3.13-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SE_HOST=0.0.0.0 \
    SE_PORT=8000

# Runs unprivileged: the service needs no root capability, and the source tree it
# will later write to is mounted with explicit ownership.
RUN useradd --create-home --uid 10001 app

COPY --from=builder --chown=app:app /app /app

WORKDIR /app
USER app
EXPOSE 8000

# Logs go to stdout; the platform owns retention (10-deployment-and-operations.md).
CMD ["python", "-m", "store_everything"]
