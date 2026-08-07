# Infra Overview: provider-vs-gateway error and latency triage

## Context

The operator gets two user complaints against the LLM gateway stack (nginx load-balancer → LiteLLM → provider APIs) and today cannot answer either from the Infra Overview dashboard:

1. "Gateway is laggy / everything is slow" - no panel attributes latency to a layer (nginx vs LiteLLM vs provider).
2. "I get API Error in Claude - your gateway's fault!" - no panel shows load-balancer HTTP errors at all.
   The ClickHouse error panels can't say whether a failure was a provider-returned error (429/529/5xx from Anthropic/OpenAI) or a gateway-internal one.

The data mostly already exists but isn't surfaced:

- nginx access logs in Loki carry `backend`, `status`, `request_time`, `upstream_time` per request (`services/load-balancer/nginx.conf`, `log_format backend_logfmt`).
  No panel filters `status >= 400` or reads the timing fields.
- LiteLLM's StandardLoggingPayload for failures carries `error_information.error_code` (HTTP status), `error_information.error_class`, `custom_llm_provider`, and `api_base` (proof the request reached the provider).
  All of these sit unparsed in `ingest_raw.raw_payload_full`; only `error_type` is extracted into `agent_events.calculated_payload` today.

Attribution ground truth (verified in `services/load-balancer/nginx.conf`):

- `backend=litellm` (port 4000, no fallback) + 5xx → LiteLLM unreachable → gateway outage.
- `backend=anthropic-proxy` / `openai-proxy` (ports 4001/4002) - LiteLLM answered.
  A 502/503/504 from LiteLLM never appears here because `proxy_intercept_errors` reroutes it to the fallback location before logging.
- `backend=anthropic-proxy-fallback` / `openai-proxy-fallback` - nginx gave up on LiteLLM and hit the real provider; the logged status is the provider's own answer.
- In `agent_events` failures: `api_base` present → provider-side error; `api_base` empty → gateway-internal failure.

User decisions: enrich the worker extraction (no schema migration - `calculated_payload` is already a JSON column), backfill history via the reparse service, add dashboard panels for both cases.

## Step 1 - Worker enrichment (`services/_common/src/ingest_parsing.py`)

Add helper `_failure_attribution(payload) -> dict` next to the existing `_error_type()`:

```python
error_information = payload.get("error_information") or {}
api_base = payload.get("api_base") or ""
llm_provider = payload.get("custom_llm_provider") or error_information.get("llm_provider") or ""
fields = {
    "error_code": error_information.get("error_code") or "",
    "error_class": error_information.get("error_class") or "",
    "llm_provider": llm_provider,
    "api_base": api_base,
    "failure_origin": "provider" if api_base else "gateway",
}
return {k: v for k, v in fields.items() if v}
```

Notes:

- `llm_provider` prefers top-level `custom_llm_provider`: the real captured 429 fixture (`services/_common/tests/captures/failure.json`) has an empty `error_information.llm_provider` even for a genuine provider error.
- Do NOT reuse `_provider_for_model` (model-name regex, coarser classification for `agent_usage.provider`).
- Call site: the existing `status == "failure"` branch in `_event_row()` - after the `error_type` extraction, `calculated_payload.update(_failure_attribution(payload))`.
- No `_EVENT_COLUMNS` / schema change.

Tests in `services/_common/tests/test_ingest_parsing.py` following the existing `load_capture("failure")` pattern:

1. Real fixture → `error_code == "429"`, `error_class == "BaseLLMException"`, `llm_provider == "anthropic"`, `api_base == "https://api.anthropic.com/v1/messages"`, `failure_origin == "provider"`.
2. Mutated fixture (`dict(payload, api_base="", custom_llm_provider="")`) → `failure_origin == "gateway"`, no `llm_provider`/`api_base` keys.

## Step 2 - Reparse backfill scoping (`services/reparse/src/reparse.py`, `docker-compose.yml`, `Makefile`)

`reparse_event()` already calls `_event_row()`, so Step 1 reaches the backfill automatically.
Add a `status` filter so the run only upserts failure rows:

- `reparse(session_id="", status="")`: add `AND ({status:String} = '' OR JSONExtractString(raw_payload_full, 'status') = {status:String})` to the paging query, mirroring the existing `session_id` parameter exactly; add `--status` CLI arg + `STATUS` env fallback in `main()`.
- `docker-compose.yml`: add `STATUS: ${STATUS:-}` to the `metrics-reparse` environment block (next to `SESSION_ID`).
- `Makefile`: thread `STATUS` through the `reparse` / `reparse-all` targets (mirrors the existing `reparse-dlq` `STAGE` pattern).
  Makefile/compose edits go through dev-ops.
- Test in `services/reparse/tests/test_reparse.py` using the existing fake-client/monkeypatch pattern: query parameters include `status`, defaulting to `""`.

Operator flow after deploy: `make reparse-all STATUS=failure`, then run the `OPTIMIZE TABLE agent_events FINAL` hint the tool prints.
`agent_events` is ReplacingMergeTree and the panels don't use `FINAL`, so counts show transient duplicates until merge.

## Step 3 - Dashboard panels (`services/grafana/dashboards-health/infra_overview.json`)

All edits via dashboards-expert (skill `dashboard-panels`, universal conventions only - this is not agents_overview.json; `query-performance-sync` explicitly does not apply).
Read the file via `parse_dashboard.py` where it works, direct Read for RowsLayout-nested parts (see skill `grafana-dashboard-parsing`).
Current max panel id 63 → new ids start at 64.
Reuse the existing `$backend` Loki variable.
Every panel gets the two-line `Goal:`/`Description:` description; encode the attribution rules from Context in these descriptions so operators can read the triage logic on the dashboard itself.

### (a) New sub-tab "HTTP errors" under the "Load balancer" tab

Sibling of Access log / Error log / Provider fallback; answers "does the balancer serve errors at all?".

- panel-64 "Backend 4xx rate by backend" (timeseries):
  `sum by (backend) (rate({container="receipt-goblin-load-balancer", stream="stdout", backend=~"$backend"} | logfmt | status >= 400 | status < 500 [$__interval]))`
- panel-65 "Backend 5xx rate by backend" - same with `status >= 500`.
- panel-66 "Recent backend errors" (logs): same selector with `| logfmt | status >= 400`.
- `status` is not an indexed label (verified: `services/alloy/config.alloy` promotes only `backend`) - `| logfmt` at query time is required.

### (b) Extend the "Error rate" sub-tab (panels 55/56) with a second row

The "whose error is it" triage:

- panel-67 "Failure origin over time (provider vs gateway)" (ClickHouse timeseries): count of `agent_events` failures grouped by `JSONExtractString(calculated_payload, 'failure_origin')` per 5-min bucket.
- panel-68 "Failures by error_code / provider" (table): group failures by `error_code`, `llm_provider`, `error_type`.
- panel-69 "Recent failures" (drill-down table): timestamp, llm_provider, error_code, error_type, error_class, failure_origin, api_base, session_id, litellm_call_id; `ORDER BY timestamp DESC LIMIT 200`.
- Important: do NOT copy panels 55/56's `INNER JOIN agent_usage` - usage rows exist only for successes, the join silently drops failures.
  Query `agent_events` alone; there is no model column there, `llm_provider`/`error_code` are the attribution keys.

### (c) Extend the "Latency" sub-tab (panels 57/58) with a second row

The "where is it slow" triage:

- panel-70 "nginx request_time p95 by backend" (Loki): `quantile_over_time(0.95, {...} | logfmt | unwrap request_time [$__interval]) by (backend)`, unit `s`.
- panel-71 "nginx upstream_time p95 by backend" - same with `unwrap upstream_time`.
- Reading: gap between 70 and 71 = nginx-added overhead; 71 vs the existing LiteLLM Prometheus histogram (panel 41) and `agent_events.latency_ms` (panels 57/58) shows whether the time is inside LiteLLM/provider.
  Note in the description: lines with `upstream_time="-"` (nothing proxied) are silently skipped by `unwrap` - expected, not missing data.

ClickHouse `rawSql`: real newlines/indentation per convention; baseline perf run for each new query via `query_perf.py` / query-perf-runner (new panels need a baseline, not a before/after).

## Execution / delegation shape

Per the session delegation policy, implementation is handed to a Sonnet `claude` subagent (or the specialist agents directly):

- Steps 1-2 code+tests, then `runner-linter` (`make lint`) and `runner-test` (`make test-services`).
- Step 3 via dashboards-expert.
- compose/Makefile via dev-ops; deploy of the worker change (rebuild+recreate worker) via dev-ops.

## Verification

1. Unit: `uv run pytest services/_common/tests/test_ingest_parsing.py -k failure` and the reparse test; then full `make test-services` + `make lint` via the runner agents.
2. Live failure end-to-end: after dev-ops rebuilds worker, fire a deliberate bad request through the gateway (nonexistent model - the `llm_exceptions` recipe from skill `litellm-alerting-mechanics`).
   Then via `mcp__dev__query` confirm the new keys land: `SELECT JSONExtractString(calculated_payload, 'failure_origin'), ... FROM agent_events WHERE status='failure' AND timestamp >= now() - INTERVAL 15 MINUTE` (poll - ingest is async).
3. Backfill: `make reparse-all STATUS=failure`, run the printed `OPTIMIZE TABLE ... FINAL`, re-check the same query over a wide window.
4. New ClickHouse panel queries: run each rawSql with `$__timeFilter` substituted per skill `grafana-query-macros` via `mcp__dev__query` + `mcp__dev__profile_query` baseline.
5. Loki panels: check in Grafana Explore with `$backend` → `.*` (requires the `observability` profile running); confirm 4xx/5xx series appear after the deliberate bad request in step 2.

## Risks / open items

- Only one real failure capture exists (a provider-side 429); the `failure_origin="gateway"` branch is unit-tested via a synthetic payload only.
  When a real gateway-internal failure is captured, validate the assumption that its payload has empty `api_base`.
- On ports 4001/4002 a LiteLLM 502/503/504 is invisible in the access log (intercepted pre-logging); gateway outage there shows up as fallback-backend traffic instead.
  The existing "Providers fallback" panels remain the signal for that, and the new panel descriptions must say so.
- Alloy's stderr port→backend inference doesn't cover ports 4001/4002 - known, out of scope (new panels use the stdout stream which is labeled correctly).
