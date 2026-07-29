# LiteLLM alerting → dashboard → Grafana alert rules

## Context

Today the stack deliberately does **not** use LiteLLM's own alerting webhook — only the `generic_api` (`metrics_webhook`) callback, which ships the full `StandardLoggingPayload` for every call into `agent_events`/`agent_usage` via `webhook`/`webhook-worker`. That data already lets us compute error rate, latency percentiles, and tool-failure rate, but it can't reconstruct signals LiteLLM tracks internally and doesn't expose per-call: budget/spend threshold crossings, deployment outages, DB exceptions, hung/too-slow requests flagged by LiteLLm's own logic.

Grafana's unified alerting is already provisioned (`services/grafana/provisioning/alerting/rules.yml`) but scoped only to infra up/down checks (7 rules, `infra-health` group, Prometheus+blackbox), with an explicit top-of-file note that **no contact point is wired up yet** — alerts fire into the default receiver only. The user wants to keep deferring the notification-channel decision, and instead: (1) start also capturing LiteLLM's native alert events, (2) surface both that and our existing derived metrics on one new dashboard, (3) add Grafana alert rules driven by that dashboard's queries, still with no contact point — same "fires into the void for now" state as `infra-health`, just extended to LLM-level signals.

Decisions already made with the user:
- **Enable LiteLLM's native alerting webhook** as a second data source (`general_settings.alerting: ["webhook"]`), in addition to existing derived metrics from `agent_events`/`agent_usage`.
- **New dashboard lives in `services/grafana/dashboards-health/`** (ops/alerting home, alongside `infra_overview.json`), not `services/grafana/dashboards/` (cost/efficiency home).

## Architecture

```
LiteLLM (general_settings.alerting: [webhook])
        │  POST JSON (budget_alerts, llm_exceptions, llm_too_slow,
        │             outage_alerts, db_exceptions, failed_tracking_spend, ...)
        ▼
webhook: POST /api/v1/litellm-alert  ──►  ingest_litellm_alert()  ──►  ClickHouse: litellm_alerts (direct insert)
                                                                              │
agent_events / agent_usage (existing pipeline, unchanged)  ─────────────────►│  (queried directly, not copied)
                                                                              ▼
                                                        services/grafana/dashboards-health/litellm_alerting.json
                                                                              │
                                                                              ▼
                                                services/grafana/provisioning/alerting/rules.yml
                                                        new group `llm-alerts` (still no contact point)
```

### 1. LiteLLM config (`services/litellm/config.yaml`)

Add to `general_settings`:
```yaml
alerting: ["webhook"]
alert_types:
  - llm_exceptions
  - llm_too_slow
  - llm_requests_hanging
  - outage_alerts
  - db_exceptions
  - budget_alerts
  - failed_tracking_spend
alerting_threshold: 300   # seconds, matches llm_too_slow/hanging trigger
```
(Deliberately excludes management-event types — key/team/user lifecycle, `new_model_added`, `spend_reports`/`daily_reports`/`fallback_reports` digests — those aren't reliability signals and would just add noise to a triage dashboard; can be added later if wanted.)

`WEBHOOK_URL` env var (LiteLLM's own alerting webhook target, distinct from the existing `callback_settings.metrics_webhook.endpoint`) → same internal DNS pattern as the current metrics endpoint, pointed at the new route below.

This is a `litellm` container config change — per `AGENTS.md`, restarting/recreating `litellm` needs asking the user first even for config-only changes, every time, since it's live shared infra other sessions may be routing through right now.

### 2. New webhook route (`services/webhook/src/server.py`)

`POST /api/v1/litellm-alert` — same trust model as `/api/v1/metrics` (internal-network-only, no `Authorization` check; LiteLLM's generic alerting webhook has no header-auth mechanism to check against anyway, per the fetched docs). Parses the JSON body and calls a new `ingest_litellm_alert(payload)`.

Ingestion pattern: **direct-to-ClickHouse insert**, not queued through Redis — follows the existing low-volume pattern already used for `/api/v1/session-git-branch` / `/api/v1/plan-proposal` (`ingest_git_branch`/`ingest_plan_proposal` in `clickhouse_ingest.py`), since alert events are rare (budget crossings, exceptions, outages) compared to per-call traffic, unlike `/api/v1/metrics` which needed the Redis buffer specifically to survive high request volume.

### 3. New ClickHouse table (`services/clickhouse/schema.sql` + a migration)

Read the `clickhouse-migration` skill before touching this. New table `litellm_alerts`, modeled on `ingest_dlq`'s shape (raw-payload-preserving, no TTL, half-year `PARTITION BY` on its time column per the established convention):

```sql
CREATE TABLE IF NOT EXISTS litellm_alerts
(
    received_at   DateTime64(3) DEFAULT now64(3),
    event         LowCardinality(String) DEFAULT '',       -- e.g. budget_crossed, threshold_crossed, llm_exceptions
    event_group   LowCardinality(String) DEFAULT '',       -- customer/key/team/proxy/internal_user, when present
    key_alias     String DEFAULT '',
    team_id       String DEFAULT '',
    user_id       String DEFAULT '',
    spend         Nullable(Float64),
    max_budget    Nullable(Float64),
    event_message String DEFAULT '',
    raw_payload   String DEFAULT '' CODEC(ZSTD(3))         -- full JSON, since alert shape varies by type
)
ENGINE = MergeTree
PARTITION BY concat(toString(toYear(received_at)), '-H', toString(intDiv(toMonth(received_at) - 1, 6) + 1))
ORDER BY (received_at);
```
`raw_payload` exists because the fetched LiteLLM docs only document the budget-event shape in full; `llm_exceptions`/`outage_alerts`/`db_exceptions` payloads likely carry different fields — store the full body and add first-class columns for whatever's actually observed once real payloads are captured (same "capture real payloads, don't guess the shape" approach already used for `StandardLoggingPayload`/`services/webhook/tests/captures`).

### 4. New dashboard (`services/grafana/dashboards-health/litellm_alerting.json`)

Follow `infra_overview.json`'s `RowsLayout` schema/conventions (this folder isn't in `dashboard-parser`'s scope — that agent only understands `agents_overview.json`'s `TabsLayout`, so build/read this one with plain `Read`/`Edit`/Bash-python, same as `infra_overview.json` already is). Delegate actual panel construction to the `dashboard-panels-builder` agent (in scope: any dashboard JSON except `agents_overview.json`'s panel-76/77), which reads the `dashboard-panels` skill's universal conventions first.

Panel groups:
- **LiteLLM native alerts** — table panel over `litellm_alerts`, recent rows, filterable by `event`.
- **Error rate** — timeseries, `agent_events.status = 'failure'` ratio over time, by model/provider (reuses the same kind of query as `agents_overview.json`'s existing "Error rate by tool"/"API-level failure reasons over time" panels — reference those for the SQL pattern, don't re-derive from scratch).
- **Latency** — p95/p99 `latency_ms` timeseries by model (same pattern as the existing "Latency percentiles" panel).
- **Spend/budget** — `agent_usage.cost` trend, cross-referenced against any `litellm_alerts` budget events on the same timeline.
- **Ingest pipeline health** — `ingest_dlq` row count/recent rows, finally giving that "triage/alerting feed" (currently unconsumed per its own schema comment) an actual consumer.

Every panel's `rawSql` gets tested against real data (`mcp__dev__query`, through the agent) before being written, and any non-trivial SQL goes through the `clickhouse-sql` skill first, per that agent's own standing rules.

### 5. New Grafana alert rules (`services/grafana/provisioning/alerting/rules.yml`)

Add a second `groups` entry, `llm-alerts` (same file, same "no contact point yet" state as `infra-health` — keep the top-of-file comment accurate, it already says contact points come later). Candidate rules, each backed by a ClickHouse datasource query mirroring the dashboard panel it corresponds to:
- Error rate over threshold (e.g. >5% failures in a rolling window).
- p95 latency over threshold.
- Any `litellm_alerts` row with `event` indicating budget/outage/exception in the last N minutes (presence-based, not threshold-based).
- `ingest_dlq` non-empty in the last N minutes (pipeline itself failing to ingest).

Exact thresholds are a judgment call to make with the user during implementation (not guessed here) — start conservative and expect tuning once real data is visible on the new dashboard.

## Key files

- `services/litellm/config.yaml` — `general_settings.alerting`/`alert_types`/`alerting_threshold`, `WEBHOOK_URL`.
- `services/webhook/src/server.py` — new `POST /api/v1/litellm-alert` route.
- `services/webhook/src/clickhouse_ingest.py` — new `ingest_litellm_alert()`, alongside existing `ingest_git_branch`/`ingest_plan_proposal`.
- `services/clickhouse/schema.sql` + new file under `services/clickhouse/migrations/` — `litellm_alerts` table.
- `services/grafana/dashboards-health/litellm_alerting.json` — new dashboard.
- `services/grafana/provisioning/alerting/rules.yml` — new `llm-alerts` group.

## Delegation / process notes for implementation

- `clickhouse-migration` skill before the migration file.
- `dashboard-panels-builder` agent for the new dashboard's panels (not `dashboard-parser`, which only handles `agents_overview.json`).
- `dev-ops` agent for any rebuild/recreate this needs (`webhook`, `clickhouse-migrate`), and **ask the user first** before any `litellm` restart/recreate, even though this is a config-only change — per `AGENTS.md`'s explicit rule and incident history.
- `webhook-test-runner` agent to run `make test` after any `clickhouse_ingest.py`/`server.py` change.
- Capture a few real LiteLLM alert payloads (`CAPTURE_ENABLED`-style, or ad hoc logging) before finalizing `litellm_alerts`' first-class columns beyond `raw_payload` — the docs only fully describe the budget-event shape.

## Verification

1. `webhook-test-runner` agent: `make test` passes after `clickhouse_ingest.py`/`server.py` changes.
2. Trigger a real LiteLLM alert (e.g. a deliberately low per-key budget crossed in a test key) and confirm a row lands in `litellm_alerts` via `mcp__dev__query` (never a direct ClickHouse connection, per the base rule).
3. Open `litellm_alerting.json` in Grafana and confirm every panel renders (no query errors) against real data.
4. Confirm the new `llm-alerts` rule group appears under Grafana's Alerting > Alert rules, evaluates without error, and (as expected for now) has no contact point delivering anywhere — matching `infra-health`'s current state.
