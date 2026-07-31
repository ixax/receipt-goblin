# Build context is the repo root (not this directory), so it can also bake
# in services/_common:
# `docker build -t worker -f services/worker/Dockerfile . && docker run worker`.
# Split out of services/webhook/Dockerfile's one-image/APP_ROLE design -
# see plans/webhook-worker-split.md.
FROM python:3.12-slim

WORKDIR /app

COPY services/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/_common/src/ ./common/
COPY services/worker/src/ ./src/

EXPOSE 9200

CMD ["python", "-m", "src.worker"]
