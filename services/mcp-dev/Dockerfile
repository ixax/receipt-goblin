# Build context is the repo root (not this directory), so it can also bake
# in services/_common: `docker build -t mcp-dev -f services/mcp-dev/Dockerfile .`.
# mcp-dev is dev-only (query/profile_query, arbitrary read-only SQL) -
# docker-compose.dev.yml is the only compose file that builds/runs this
# image, mounting ./src (and services/_common/src) over /app/src (/app/common)
# and adding `--reload`, so deps stay baked in and only the source is live.
# See services/mcp-stats for the prod-facing sibling (fixed statistics
# tools, real auth).
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY services/mcp-dev/requirements.lock .
# requirements.lock pins the full transitive tree (regenerate with
# `make lock`), so pip resolves nothing here - that resolver pass, not the
# downloads, was most of a cold build's time. --mount then keeps pip's
# wheel cache across builds, so a lock change re-downloads only what
# actually moved. Both need BuildKit (docker-buildx) - see README
# "Prerequisites: Docker via Colima".
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.lock

COPY services/_common/src/ ./common/
COPY services/_common/queue.yml ./common/queue.yml
COPY services/mcp-dev/src/ ./src/
COPY services/mcp-dev/config.yml .

EXPOSE 8001

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8001"]
