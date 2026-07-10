#!/bin/sh
# Renders the ClickHouse datasource provisioning YAML from a template using
# sed (no envsubst/gettext dependency - not guaranteed present in the base
# image), then hands off to Grafana's own entrypoint.
set -eu

# Defaults live in docker-compose.yml (single source of truth); these vars
# are always set by the time this container starts, so no fallback here.
: "${CLICKHOUSE_HOST:?}" "${CLICKHOUSE_PORT:?}" "${CLICKHOUSE_USER:?}" \
  "${CLICKHOUSE_PASSWORD:?}" "${CLICKHOUSE_DATABASE:?}"

TEMPLATE=/etc/grafana/provisioning-templates/datasources/clickhouse.yml.template
OUT_DIR=/etc/grafana/provisioning/datasources
mkdir -p "$OUT_DIR"

sed \
  -e "s|__CLICKHOUSE_HOST__|${CLICKHOUSE_HOST}|g" \
  -e "s|__CLICKHOUSE_PORT__|${CLICKHOUSE_PORT}|g" \
  -e "s|__CLICKHOUSE_USER__|${CLICKHOUSE_USER}|g" \
  -e "s|__CLICKHOUSE_PASSWORD__|${CLICKHOUSE_PASSWORD}|g" \
  -e "s|__CLICKHOUSE_DATABASE__|${CLICKHOUSE_DATABASE}|g" \
  "$TEMPLATE" > "$OUT_DIR/clickhouse.yml"

exec /run.sh "$@"
