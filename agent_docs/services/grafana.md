# `grafana`

Dashboards, reading ClickHouse directly (`grafana` ClickHouse role, see `agent_docs/services/clickhouse.md`).

## `config.yml` / `docker-entrypoint.sh`

Grafana's own tunables (`install_plugins`, `auth_anonymous_enabled`, `auth_anonymous_org_role`) live in `services/grafana/config.yml`, not `docker-compose.yml`.
`docker-entrypoint.sh` reads it and translates it into `GF_*` env vars via `yml_get`, a `sed`-based flat `key: value` reader, not a real YAML parser - relies on this file's shape staying fully under our control.
The entrypoint also renders the ClickHouse datasource from `provisioning-templates/datasources/clickhouse.yml.template` via `sed` (no `envsubst`/`gettext` dependency, not guaranteed present in the base image), substituting `CLICKHOUSE_HOST`/`PORT`/`USER`/`PASSWORD`/`DATABASE` (all required, always set by container start).
Prometheus/Loki datasources carry no secrets/per-env values (fixed internal Docker DNS names), so unlike ClickHouse's they're plain static files, copied through as-is, not templated.

## Dashboards

All six ship as `dashboard.grafana.app/v2beta1` schema JSON; `metadata.name` is the uid (stable URL).
All follow the same panel/build conventions - read the `dashboard-panels` skill before editing any dashboard's panels, don't treat each file as bespoke.
The one named exception is `agents_overview.json`'s panel-76/77 pair and its dashboard-specific tier, per that skill's own two-tier split (dynamictext-panel-builder vs. dashboard-panels-builder agent boundary) - not re-litigated here.

- `services/grafana/dashboards/agents_overview.json` - "Agents Overview" dashboard, uid `agents-overview`. Use the `dashboard-parser` agent for current tab/panel structure, don't hardcode a count here - it drifts.
- `services/grafana/dashboards-health/clickhouse.json` - uid `clickhouse-health`, ClickHouse process/replication health panels, fed by Prometheus (`observability` profile, see `agent_docs/services/observability.md`).
- `services/grafana/dashboards-health/query_performance.json` - uid `query-performance`, per-panel ClickHouse query cost for `agents_overview.json`, sourced from `system.query_log` via the `log_comment` tag set by `tag_panel_queries.py`. Mirrors `agents_overview.json`'s tab structure.
- `services/grafana/dashboards-health/docker_containers.json` - uid `docker-containers`, "Host"/"Containers" tabs: per-container CPU/memory/network/disk/process metrics (cAdvisor) plus whole-host metrics (node_exporter), fed by Prometheus (`observability` profile).
- `services/grafana/dashboards-health/infra_overview.json` - uid `infra-overview`, up/down status, request rate/latency, and worker queue health for every service added by the observability migration, fed by Prometheus. Conceptually the dashboard behind the infra-health alert rules (`services/grafana/provisioning/alerting/rules.yml`).
- `services/grafana/dashboards-health/litellm_alerting.json` - uid `litellm-alerting`, LiteLLM native alerts (budget/outage/exception/hang signals) alongside our own derived reliability metrics (error rate, latency, spend, ingest pipeline health). Fed directly by ClickHouse (`agent_events`/`agent_usage`), not Prometheus - available without the `observability` profile. Conceptually the dashboard behind the llm-alerts alert rules (`services/grafana/provisioning/alerting/rules.yml`).
