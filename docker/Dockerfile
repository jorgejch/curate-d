# syntax=docker/dockerfile:1.19
#
# ONE image for the API and both worker profiles. Not just to save build and pull cost:
# DBOS pins in-flight workflows to an application version, so an API and a worker running
# different image tags is a correctness hazard, not untidiness. One image means one tag,
# one image policy, and no way for the two to drift apart.
#
# The manifests choose behaviour with `command:` and CURATE_D_WORKER_PROFILE. The image
# does not decide.
#
# rclone is deliberately absent: it runs as a sidecar with its own credentials, and the
# whole point of the worker split is that the process decoding untrusted file content
# cannot reach the process holding provider tokens. Baking rclone in would put the binary
# next to the decoders.

ARG TESSERACT_VERSION=5.5.*

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# ---------------------------------------------------------------------------
# Dependencies. Split from the application layer so a source change does not
# reinstall the world.
# ---------------------------------------------------------------------------
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Locked, no dev group, no project: this layer is only the third-party tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---------------------------------------------------------------------------
# Runtime.
# ---------------------------------------------------------------------------
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ARG TESSERACT_VERSION

# tesseract backs the local OCR tier. libpq is needed even with psycopg[binary],
# because the binary wheel does not cover every libpq entry point psycopg uses.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install --no-install-recommends --yes \
        tesseract-ocr=${TESSERACT_VERSION} \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root, and the filesystem is read-only at runtime (see the manifests). Anything
# that needs to write gets an explicit volume.
RUN groupadd --gid 65532 curated \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin curated

WORKDIR /app
COPY --from=builder --chown=65532:65532 /app /app

# The dev image tag, passed by CI for dev builds only; empty in a release image. It makes
# each dev build a distinct DBOS application version (runtime/dbos_app.py).
ARG CURATE_D_BUILD_REVISION=""

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CURATE_D_BUILD_REVISION=${CURATE_D_BUILD_REVISION}

USER 65532:65532

# Every workload sets `command:` explicitly, so this default is only what you get from a
# bare `docker run`. Kept as the API because that is the one worth poking at by hand; a
# worker started without CURATE_D_WORKER_PROFILE refuses to run anyway.
EXPOSE 8000
CMD ["python", "-m", "curate_d.api"]
