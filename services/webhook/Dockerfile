# Prod image - self-contained, no bind mounts assumed. Build context is the
# repo root (not this directory) so it can also bake in
# services/clickhouse/migrations for the `migrate` role below - build/run
# standalone from the repo root with
# `docker build -t webhook -f services/webhook/Dockerfile . && docker run -p 8000:8000 webhook`.
# docker-compose.dev.yml still builds this same image for local dev, but
# mounts ./src over /app/src and overrides `command:` to add `--reload` for
# the `webhook` service - deps stay baked into the image (no reinstall on
# every container start), only the source is live.
# This one image serves four compose services - webhook/webhook-worker/
# webhook-reparse/clickhouse-migrate - which role runs is picked at
# container start by docker-entrypoint.sh via the APP_ROLE env var, not by a
# per-service `command:` override.
FROM python:3.12-slim

WORKDIR /app

COPY services/webhook/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/webhook/src/ ./src/
COPY services/webhook/config.yml .
COPY --chmod=0755 services/webhook/docker-entrypoint.sh .
# Only used by the `migrate` role (see docker-entrypoint.sh /
# src/migrate.py's MIGRATIONS_DIR), but harmless to bake into the other
# three roles' copy of this same image.
COPY services/clickhouse/migrations/ ./migrations/

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
