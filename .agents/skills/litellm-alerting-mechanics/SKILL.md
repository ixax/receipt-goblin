---
name: litellm-alerting-mechanics
description: >
  Exact trigger mechanics per LiteLLM alert_type, and the litellm_alerts ClickHouse verification query, for testing services/litellm/config.yaml's native alerting.
  TRIGGER - read before triggering or verifying any LiteLLM alert_type.
  Owner of the how, not the whether-to-attempt policy (that stays in litellm-test-alerting.md).
  v1.0.0
---

Access: `LITELLM_MASTER_KEY`/`LITELLM_BASE_URL` give admin API access (`/key/generate`, `/key/delete`, chat completions) - same credential `services/webhook/src/server.py`'s `_virtual_key_is_valid` uses.
Auth header: `x-litellm-api-key: Bearer <LITELLM_MASTER_KEY>` (working pattern: `services/litellm/scripts/test-models.sh`).
`litellm` must already be running - never start/restart it to run this.

## Triggering each alert_type

- `budget_alerts`: `POST /key/generate` with near-zero `max_budget` (e.g. `0.000001`); one cheap real completion through the new key (`claude-haiku-4-5`, short prompt) to exceed it.
  Always `POST /key/delete` the throwaway key afterward - success, failure, or partial error alike.
- `llm_exceptions`: a deliberately malformed request (nonexistent `model_name`, or invalid params) triggering a real provider/proxy exception.
  No cleanup - nothing was created.
- `llm_too_slow` / `llm_requests_hanging`: need a real call running past `alerting_threshold` (300s, `services/litellm/config.yaml`) before LiteLLM's watchdog fires - 5+ minutes of wall clock and real tokens.

## Verifying a triggered alert

Poll via `mcp__clickhouse__query` (never a direct connection - ClickHouse reads always go through the MCP tool):

```sql
SELECT * FROM litellm_alerts
WHERE received_at >= now() - INTERVAL 15 MINUTE
ORDER BY received_at DESC
```

Alerting isn't synchronous with the trigger - poll a few times with short sleeps before concluding it didn't fire.
Match the new row's `event` column against the triggered `alert_types` name.
