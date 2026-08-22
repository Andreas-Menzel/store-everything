# syntax=docker/dockerfile:1

# The web client, built from the same commit as the API that serves it
# (10-deployment-and-operations.md § topology, F-027/FR-1). One image and one origin is what
# makes the session cookie same-site by construction, so these two cannot be deployed apart.
FROM node:22-bookworm-slim AS web

ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:$PATH" \
    CI=1

WORKDIR /build

# Manifests and the lockfile first: dependencies change far less often than the source, so
# this layer survives most rebuilds.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY web/package.json web/
COPY packages/api-client/package.json packages/api-client/
RUN corepack enable && pnpm install --frozen-lockfile --filter @store-everything/web...

# The generated client is committed, so the app is built against the contract in the
# repository rather than whatever a build-time generation step would produce.
COPY packages/ packages/
COPY web/ web/
RUN pnpm --filter @store-everything/web run build


# Build stage: resolve and install exactly what the lockfile pins.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: they change far less often than the source, so this layer
# survives most rebuilds.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=server/uv.lock,target=uv.lock \
    --mount=type=bind,source=server/pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=server/README.md,target=README.md \
    uv sync --locked --no-install-project --no-dev

COPY server /app
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

# The same reasoning for the workspace storage, which holds the users' own file trees
# (03 § storage layout). A managed workspace cannot be created at all if this is root-owned.
RUN mkdir -p /srv/store-everything && chown -R app:app /srv/store-everything

# The client the API serves at its own origin. Its own directory rather than inside the
# application tree: what is served to a browser and what runs the service are different kinds
# of thing, and `SE_WEB_ROOT` names this one (F-027/FR-1).
COPY --from=web --chown=app:app /build/web/dist /srv/store-everything-web

COPY --from=builder --chown=app:app /app /app

WORKDIR /app
USER app
EXPOSE 8000

# Logs go to stdout; the platform owns retention (10-deployment-and-operations.md).
CMD ["python", "-m", "store_everything"]
