# `grafana`

Dashboards, reading ClickHouse directly (`grafana` ClickHouse role, see `agent_docs/services/clickhouse.md`).

## `config.yml` / `docker-entrypoint.sh`

Grafana's own tunables (`install_plugins`, `auth_anonymous_enabled`, `auth_anonymous_org_role`) live in `services/grafana/config.yml`, not `docker-compose.yml` - `docker-entrypoint.sh` reads it and translates it into `GF_*` env vars itself (`yml_get`, a `sed`-based flat `key: value` reader, not a real YAML parser - relies on this file's shape staying fully under our control).
The entrypoint also renders the ClickHouse datasource from `provisioning-templates/datasources/clickhouse.yml.template` via `sed` (no `envsubst`/`gettext` dependency - not guaranteed present in the base image), substituting `CLICKHOUSE_HOST`/`PORT`/`USER`/`PASSWORD`/`DATABASE` (all required, no fallback - always set by container start).
Prometheus/Loki datasources carry no secrets/per-env values (fixed internal Docker DNS names), so unlike ClickHouse's they're plain static files, copied through as-is, not templated.

## Dashboards

`services/grafana/dashboards/agents_overview.json` - "Agents Overview" dashboard, `dashboard.grafana.app/v2beta1` schema, uid `agents-overview` (stable URL).
Use the `dashboard-parser` agent for current tab/panel structure - don't hardcode a count here, it drifts.
`services/grafana/dashboards-health/` - `clickhouse.json`/`query_performance.json`/`docker_containers.json`, the "Host"/"Containers"/"ClickHouse Health" reliability panels fed by the `observability` profile (see `agent_docs/services/observability.md`).
