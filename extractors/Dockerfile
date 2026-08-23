# syntax=docker/dockerfile:1
#
# The official extractors' image. It is the shape every extractor image follows, and the only
# thing a reader should have to look past to see that shape is the analysis tooling a real one
# adds — here libvips and PDFium, for `preview-gen`.
#
# One image, several entrypoints: the SDK, the reference double and `preview-gen` share a package,
# so a service block picks which one runs (`command: se-preview-gen`). That trade holds while the
# analysis tooling is a couple of wheels; the first extractor that needs system packages of its
# own (OCR) is the point to split this into one image per extractor rather than to keep piling
# apt installs onto everybody's image.
#
# The container baseline of 05 § container requirements, in the order the layers appear:
# non-root, no package installer at runtime, and no capability it does not need. What it does
# *not* get is the network to anything but the extractor API — that is the compose topology's
# job, not the image's (ADR-0021).
#
# Base images are pinned by tag here, matching `server/Dockerfile`. Spec 05 asks for digests,
# and neither image does that yet; it is one change for both, tracked separately rather than
# claimed here.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: they change far less often than the source. `--extra preview` is what
# brings the imaging stack in — the SDK itself keeps exactly one dependency so a third-party
# author inherits nothing from us but `httpx`.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-install-project --no-dev --extra preview

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra preview


FROM python:3.13-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SE_CORE_URL=http://api:8000 \
    SE_REFERENCE_MODE=verify

# The runtime never installs packages: the virtualenv is built above and copied in whole, and
# removing pip takes its vendored dependency tree — and one way to mutate a running container —
# with it. The same reasoning as the core image, and the same enforcement in CI.
RUN rm -rf /usr/local/lib/python3.*/site-packages/pip \
           /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.*/ensurepip \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

RUN useradd --create-home --uid 10001 extractor

COPY --from=builder --chown=extractor:extractor /app /app

WORKDIR /app
USER extractor

# Nothing listens: dispatch is poll-based, so this container has no port and needs no inbound
# route at all (ADR-0020). That is what lets it live on a network with no gateway.
#
# `preview-gen` by default because it is the one a real instance runs; the reference double is
# selected by overriding the command, which is what `compose.extractor-example.yaml` does.
CMD ["se-preview-gen"]
