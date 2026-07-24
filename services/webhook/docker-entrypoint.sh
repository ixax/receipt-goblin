#!/bin/sh
# Picks which of this image's four roles to run via the APP_ROLE env var -
# webhook/webhook-worker/webhook-reparse/clickhouse-migrate all build this
# same image (see Dockerfile) and only differ in which process starts, so
# the choice lives here as a runtime env var instead of a compose-level
# `command:` override (see docker-compose.yml's per-service `environment:
# APP_ROLE: ...`).
set -eu

# docker-compose.dev.yml still overrides `command:` for the `webhook`
# service to add uvicorn's --reload flag - Docker appends that override as
# args to this entrypoint, so honor it verbatim instead of the APP_ROLE
# dispatch below.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

case "${APP_ROLE:-server}" in
  server)
    exec uvicorn src.server:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec python -m src.worker
    ;;
  reparse)
    exec python -m src.reparse
    ;;
  migrate)
    exec python -m src.migrate
    ;;
  *)
    echo "docker-entrypoint.sh: unknown APP_ROLE '${APP_ROLE}' (expected server|worker|reparse|migrate)" >&2
    exit 1
    ;;
esac
