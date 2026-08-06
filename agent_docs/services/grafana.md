# `grafana`

Dashboards, reading ClickHouse directly (`grafana` ClickHouse role, see `agent_docs/services/clickhouse.md`).

## `config.yml` / `docker-entrypoint.sh`

Grafana's own tunables (`install_plugins`, `auth_anonymous_enabled`, `auth_anonymous_org_role`) live in `services/grafana/config.yml`, not `docker-compose.yml`.
`docker-entrypoint.sh` reads it and translates it into `GF_*` env vars via `yml_get`, a `sed`-based flat `key: value` reader, not a real YAML parser - relies on this file's shape staying fully under our control.
The entrypoint also renders the ClickHouse datasource from `provisioning-templates/datasources/clickhouse.yml.template` via `sed` (no `envsubst`/`gettext` dependency, not guaranteed present in the base image), substituting `CLICKHOUSE_HOST`/`PORT`/`USER`/`PASSWORD`/`DATABASE` (all required, always set by container start).
Prometheus/Loki datasources carry no secrets/per-env values (fixed internal Docker DNS names), so unlike ClickHouse's they're plain static files, copied through as-is, not templated.

## Panel rendering (`grafana-renderer`)

`/render` and `/render/d-solo` turn a panel into a PNG, which Grafana itself cannot do - it delegates to the `grafana-renderer` sidecar (`services/grafana-renderer/Dockerfile`, stock `grafana/grafana-image-renderer`).
Wiring is `rendering_server_url`/`rendering_callback_url` in `config.yml`, translated to `GF_RENDERING_*` by the entrypoint like every other setting there - deliberately not the in-process image-renderer plugin, which would drag headless Chromium into the Grafana image itself.
The sidecar is internal-only: no published port, no load-balancer route, nothing outside the compose network reaches it.
`/render` answers 500 while the sidecar is down or its URL is blank, which is the expected state on a stack that never brought it up.
Applying a change here needs the sidecar built and Grafana restarted so the entrypoint re-reads `config.yml`.

## Dashboards

All five ship as `dashboard.grafana.app/v2beta1` schema JSON; `metadata.name` is the uid (stable URL).
All follow the same panel/build conventions - read the `dashboard-panels` skill before editing any dashboard's panels, don't treat each file as bespoke.
`dashboards-expert` owns every panel in every dashboard directly, including `agents_overview.json`'s Dynamic Text pair (panel-76/77) - see that agent's own frontmatter for the two on-demand skills (`dynamictext-panel-queries`, `dynamictext-panel-design-system`) it reads for Dynamic-Text-specific query/styling work.

- `services/grafana/dashboards/agents_overview.json` - "Agents Overview" dashboard, uid `agents-overview`.
  Use the `dashboard-parser` agent for current tab/panel structure, don't hardcode a count here - it drifts.
- `services/grafana/dashboards-health/clickhouse.json` - uid `clickhouse-health`, ClickHouse process/replication health panels, fed by Prometheus (`observability` profile, see `agent_docs/services/observability.md`).
- `services/grafana/dashboards-health/query_performance.json` - uid `query-performance`, per-panel ClickHouse query cost for `agents_overview.json`, sourced from `system.query_log` via the `log_comment` tag set by `tag_panel_queries.py`.
  Mirrors `agents_overview.json`'s tab structure.
- `services/grafana/dashboards-health/docker_containers.json` - uid `docker-containers`, "Host"/"Containers" tabs: per-container CPU/memory/network/disk/process metrics (cAdvisor) plus whole-host metrics (node_exporter), fed by Prometheus (`observability` profile).
- `services/grafana/dashboards-health/infra_overview.json` - uid `infra-overview`, up/down status, request rate/latency, and worker queue health for every service added by the observability migration, fed by Prometheus.
  Conceptually the dashboard behind the infra-health alert rules (`services/grafana/provisioning/alerting/rules.yml`).
  Its "Load balancer" tab's "Access log"/"Error log" sub-tabs are Logs panels fed by Loki instead - see `agent_docs/services/load-balancer.md`'s "Access/error logs" section.
  Its "LiteLLM" and "Ingest DLQ" tabs are fed directly by ClickHouse (`agent_events`/`agent_usage`/`litellm_alerts`/`ingest_dlq`), not Prometheus - available without the `observability` profile.
  "LiteLLM" holds LiteLLM's own native alerts (budget/outage/exception/hang signals) alongside our derived reliability metrics (error rate, latency, spend); "Ingest DLQ" triages rejected ingest rows.
  Conceptually also the dashboard behind the llm-alerts alert rules (`services/grafana/provisioning/alerting/rules.yml`).
