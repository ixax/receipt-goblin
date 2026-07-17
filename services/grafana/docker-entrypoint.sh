#!/bin/sh
# Renders the ClickHouse datasource provisioning YAML from a template using
# sed (no envsubst/gettext dependency - not guaranteed present in the base
# image), then hands off to Grafana's own entrypoint.
set -eu

# Defaults live in docker-compose.yml (single source of truth); these vars
# are always set by the time this container starts, so no fallback here.
: "${CLICKHOUSE_HOST:?}" "${CLICKHOUSE_PORT:?}" "${CLICKHOUSE_USER:?}" \
  "${CLICKHOUSE_PASSWORD:?}" "${CLICKHOUSE_DATABASE:?}"

# Grafana's own settings (plugin install, anonymous auth) come from
# config.yml, not docker-compose.yml - translate the flat `key: value`
# lines into the GF_* env vars Grafana's Docker image expects. Not a real
# YAML parser - relies on this file's shape being fully under our control.
CONFIG=/etc/grafana/config.yml
yml_get() {
    sed -n "s/^$1: *\"\{0,1\}\([^\"]*\)\"\{0,1\}\$/\1/p" "$CONFIG"
}
export GF_INSTALL_PLUGINS="$(yml_get install_plugins)"
export GF_AUTH_ANONYMOUS_ENABLED="$(yml_get auth_anonymous_enabled)"
export GF_AUTH_ANONYMOUS_ORG_ROLE="$(yml_get auth_anonymous_org_role)"

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
