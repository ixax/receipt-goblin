---
name: litellm-test-alerting
description: >
  End-to-end test of LiteLLM's native alerting (services/litellm/config.yaml's general_settings.alerting: ["webhook"], the 7 alert_types, the litellm_alerts ClickHouse table fed by services/webhook/src/ingest.py's ingest_litellm_alert()).
  Called explicitly by name, never proactively.
  Attempts budget_alerts and llm_exceptions by default; llm_too_slow/llm_requests_hanging only on explicit opt-in to the 5+ minute wait; never outage_alerts/db_exceptions without the user's explicit permission obtained by the caller first; reports failed_tracking_spend as not independently testable.
  v1.1.2
tools: Bash, Read, mcp__clickhouse__query, Skill
model: claude-haiku-4-5
---

Trigger an alert condition against the live `litellm` proxy, then confirm a matching row landed in ClickHouse's `litellm_alerts` table.

Access: `LITELLM_MASTER_KEY`/`LITELLM_BASE_URL` give admin API access (`/key/generate`, `/key/delete`, chat completions) - same credential `services/webhook/src/server.py`'s `_virtual_key_is_valid` uses.
Auth header: `x-litellm-api-key: Bearer <LITELLM_MASTER_KEY>` (working pattern: `services/litellm/scripts/test-models.sh`).
`litellm` must already be running - you never start/restart it.

The 7 `alert_types` (`services/litellm/config.yaml`) split into four groups.
Test only what the caller asked for; "test everything"/no scope = the two safe defaults, skip the slow pair with a stated reason, and only explain (never attempt) the infra-risk pair.

## Safe, default

- `budget_alerts`: `POST /key/generate` with near-zero `max_budget` (e.g. `0.000001`); one cheap real completion through the new key (`claude-haiku-4-5`, short prompt) to exceed it.
  Always `POST /key/delete` the throwaway key afterward - success, failure, or partial error alike.
- `llm_exceptions`: a deliberately malformed request (nonexistent `model_name`, or invalid params) triggering a real provider/proxy exception.
  No cleanup - nothing was created.

## Slow, opt-in only

- `llm_too_slow` / `llm_requests_hanging`: need a real call running past `alerting_threshold` (300s, `services/litellm/config.yaml`) before LiteLLM's watchdog fires - 5+ minutes of wall clock and real tokens.
  Without explicit opt-in, skip and say why in the report.

## Infra-risk - never attempt on your own initiative

- `outage_alerts` / `db_exceptions`: require degrading shared infrastructure other live sessions may be using (marking a real deployment "down", or breaking `litellm-db`'s Postgres connection).
  Never attempt these yourself, however the request is phrased - tell the invoker they need the user's explicit permission first (same standing rule as restarting `litellm`, see AGENTS.md).
  With permission confirmed: design the least-disruptive method fresh each time (scoped, temporary, reverted immediately, touching no other service) - there's no fixed mechanism to reuse.

## Not testable

- `failed_tracking_spend`: fires only on an internal spend-tracking write failure; no safe deliberate trigger.
  Report "not independently testable", don't invent a risky way.

## Verifying a triggered alert

Poll via `mcp__clickhouse__query` (never a direct connection - ClickHouse reads always go through the MCP tool):

```sql
SELECT * FROM litellm_alerts
WHERE received_at >= now() - INTERVAL 15 MINUTE
ORDER BY received_at DESC
```

Alerting isn't synchronous with the trigger - poll a few times with short sleeps before concluding it didn't fire.
Match the new row's `event` column against the triggered `alert_types` name.

## Reporting back

Per attempted type: PASS (row found, `event`/`event_message` matched) or FAIL (no row after reasonable polling, or the trigger itself errored unexpectedly).
Per skipped type: one line why (opt-in not requested / needs permission / not testable).
No raw `raw_payload`/full query output - summarize `event`/`event_group`/`spend`/`max_budget`/`event_message` only.
