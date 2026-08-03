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

COPY services/mcp-dev/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/_common/src/ ./common/
COPY services/_common/queue.yml ./common/queue.yml
COPY services/mcp-dev/src/ ./src/
COPY services/mcp-dev/config.yml .

EXPOSE 8001

CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8001"]
