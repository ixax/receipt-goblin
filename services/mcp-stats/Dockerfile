# Build context is the repo root (not this directory), so it can also bake
# in services/_common: `docker build -t mcp-stats -f services/mcp-stats/Dockerfile .`.
# mcp-stats is prod-facing (ships alongside litellm/grafana) - fixed
# statistics tools only, no arbitrary SQL. See services/mcp-dev for the
# dev-only sibling (query/profile_query, arbitrary read-only SQL, no auth).
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

COPY services/mcp-stats/requirements.lock .
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
COPY services/mcp-stats/src/ ./src/

EXPOSE 8002

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8002"]
