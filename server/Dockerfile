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

# The runtime never installs packages: the virtualenv is built in the builder stage and
# copied in whole. Removing pip takes its vendored dependency tree with it — the only
# source of CVEs in this image, none of it reachable from the app — and removes one way
# to mutate a running container.
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip \
           /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.*/ensurepip \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

# Runs unprivileged: the service needs no root capability, and the source tree it
# will later write to is mounted with explicit ownership.
RUN useradd --create-home --uid 10001 app

# The app-owned storage area (03 § storage layout) has to exist in the image *with the right
# owner*: Docker copies a mount point's ownership into a fresh named volume, so creating it
# here is what makes the volume writable by an unprivileged process. Without it the container
# starts fine and then cannot write a single version — which `store-everything fs-check`
# reports rather than discovering at the first upload.
RUN mkdir -p /var/lib/store-everything/versions /var/lib/store-everything/derived \
    && chown -R app:app /var/lib/store-everything

COPY --from=builder --chown=app:app /app /app

WORKDIR /app
USER app
EXPOSE 8000

# Logs go to stdout; the platform owns retention (10-deployment-and-operations.md).
CMD ["python", "-m", "store_everything"]
