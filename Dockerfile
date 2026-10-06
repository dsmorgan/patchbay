# patchbay web UI + poller image (one image, two services — see
# docker-compose.example.yml). Config via env file mounted at /data/.env,
# SQLite model at /data/patchbay.db.
#
# Dependencies come from uv.lock, never resolved at build time: a commit
# builds the same image next month as today, and a merged Dependabot PR is
# the only way a library version changes. --locked refuses a lockfile that
# has drifted from pyproject.toml, so a dependency edit that skipped
# `uv lock` fails the build instead of quietly floating.
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.13-slim

COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, in their own layer, so an edit under src/ rebuilds in
# seconds instead of re-downloading fastapi, uvicorn, and pyvmomi every
# time. --no-install-project leaves patchbay itself out, so this layer
# depends on the lockfile alone.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra web --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra web --no-editable
ENV PATH="/app/.venv/bin:$PATH"

# bake the build identity in (shown in the UI header): pass
# --build-arg GIT_SHA=$(git rev-parse --short HEAD) at build time
ARG GIT_SHA=dev
ENV PATCHBAY_ENV=/data/.env \
    PATCHBAY_DB=/data/patchbay.db \
    PATCHBAY_BUILD=$GIT_SHA
VOLUME /data
EXPOSE 8080

# the web UI by default; the poller service overrides the command
CMD ["patchbay", "web", "--host", "0.0.0.0", "--port", "8080"]
