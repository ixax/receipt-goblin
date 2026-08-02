# Build context is the repo root (not this directory), so it can also bake
# in services/_common and services/clickhouse/migrations:
# `docker build -t migrate -f services/migrate/Dockerfile . && docker run migrate`.
# Split out of services/webhook/Dockerfile's one-image/APP_ROLE design -
# see plans/webhook-worker-split.md. CLI-only, one-shot, not started by a
# plain `docker compose up` (profiles: tools on the compose side).
FROM python:3.12-slim

WORKDIR /app

COPY services/migrate/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/_common/src/ ./common/
COPY services/_common/queue.yml ./common/queue.yml
COPY services/migrate/src/ ./src/
# Only used by this role, but this is the only role that needs it.
COPY services/clickhouse/migrations/ ./migrations/

CMD ["python", "-m", "src.migrate"]
