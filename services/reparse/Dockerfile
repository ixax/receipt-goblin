# Build context is the repo root (not this directory), so it can also bake
# in services/_common:
# `docker build -t reparse -f services/reparse/Dockerfile . && docker run reparse`.
# Split out of services/webhook/Dockerfile's one-image/APP_ROLE design -
# see plans/webhook-worker-split.md. CLI-only, no HTTP API, not started by
# a plain `docker compose up` (profiles: tools on the compose side).
FROM python:3.12-slim

WORKDIR /app

COPY services/reparse/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/_common/src/ ./common/
COPY services/_common/queue.yml ./common/queue.yml
COPY services/reparse/src/ ./src/
COPY services/reparse/config.yml .

CMD ["python", "-m", "src.reparse"]
