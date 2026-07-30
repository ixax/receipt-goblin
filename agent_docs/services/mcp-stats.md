# `mcp-stats`

Prod FastMCP server, registered at `http://localhost:8002/mcp` in `.mcp.json` (with an `Authorization: Bearer ${LITELLM_VIRTUAL_KEY}` header).
Connects as its own `mcp_stats` ClickHouse role (narrow `agent_usage`/`agent_events` read only - see `agent_docs/services/clickhouse.md`).

## `src/server.py`

One tool, `me(session_id)` - reports cost/tokens for the given session (must be the live `CLAUDE_CODE_SESSION_ID` env var), a global last-30-days rollup, and that session's 5 most expensive operations (`agent_usage` rows, `LEFT JOIN agent_events` by `litellm_call_id`).
Every request except `/health`/`/metrics` (unauthenticated - no virtual key to send from a docker healthcheck or Prometheus scrape) must carry a valid LiteLLM virtual key, checked via `common.litellm_auth.virtual_key_is_valid` against `/key/info`, the same check `services/webhook/src/server.py` uses for its own authenticated routes.
`_operation_label` picks a human-readable label per row: command/skill/agent/mcp-tool name if the call itself was one of those, else falls back to `agent_events.calculated_type` (spawned a subagent, ran a tool, answered plainly, etc).
A bare `llm:<model>` without that join is nearly always `claude-sonnet-5` repeated with no useful signal.
