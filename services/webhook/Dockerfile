# Prod image, self-contained. Build context is the repo root (not this
# directory) so it can also bake in services/clickhouse/migrations:
# `docker build -t webhook -f services/webhook/Dockerfile . && docker run -p 8000:8000 webhook`.
# docker-compose.dev.yml reuses this image for local dev, mounting ./src
# over /app/src and adding `--reload`.
# One image serves four compose services - webhook/webhook-worker/
# webhook-reparse/clickhouse-migrate - the role is picked at container start
# by docker-entrypoint.sh via APP_ROLE, not a per-service `command:`.
FROM python:3.12-slim

WORKDIR /app

COPY services/webhook/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/webhook/src/ ./src/
COPY services/webhook/config.yml .
COPY --chmod=0755 services/webhook/docker-entrypoint.sh .
# Only used by the `migrate` role, but harmless to bake into the others.
COPY services/clickhouse/migrations/ ./migrations/

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
