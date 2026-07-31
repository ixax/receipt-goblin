# Build context is the repo root (not this directory), so it can also bake
# in services/_common (for common.logging_config only - this service needs
# no shared config, see plans/webhook-worker-split.md):
# `docker build -t loadtest -f services/loadtest/Dockerfile . && docker run loadtest`.
# Split out of services/webhook/Dockerfile's one-image/APP_ROLE design.
# CLI-only, no HTTP API, not started by a plain `docker compose up`
# (profiles: tools on the compose side).
FROM python:3.12-slim

WORKDIR /app

COPY services/loadtest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/_common/src/ ./common/
COPY services/loadtest/src/ ./src/

CMD ["python", "-m", "src.loadtest"]
