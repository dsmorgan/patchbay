# patchbay web UI + poller image (one image, two services — see
# docker-compose.example.yml). Config via env file mounted at /data/.env,
# SQLite model at /data/patchbay.db.
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir '.[web]'

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
