#!/bin/sh
# nginx's own base-image entrypoint (/docker-entrypoint.sh) runs every
# executable script under /docker-entrypoint.d/ in name order before
# starting nginx - this is that image's supported extension point, so this
# script doesn't need to replace ENTRYPOINT itself or reimplement anything
# the base image already does (signal handling, its own conf.d envsubst
# templating, etc.).
#
# index.html.template (baked in at build time, see Dockerfile) hardcodes
# paths but not ports - published ports are only known at `docker compose
# up` time (.env/shell), so they're substituted here, once, on every
# container start, from the env vars docker-compose.yml's `load-balancer`
# service passes in (see its `environment:` block).
set -eu

envsubst '${WEBHOOK_PORT} ${LITELLM_PORT} ${GRAFANA_PORT} ${MCP_SERVER_PORT} ${CLICKHOUSE_HTTP_PORT} ${PROMETHEUS_PORT} ${LANGFUSE_PORT}' \
  < /etc/nginx/html-template/index.html.template \
  > /usr/share/nginx/html/index.html
