---
name: trace-debugging_v1.0.1
description: >
  Troubleshooting the chain of agent calls via session_id/trace_id/turn_id.
  Use this when investigating the latency of a specific step, the order of events in a session, or when debugging a specific trace in agent_events/agent_usage.
---

## Chain tracing / identity

Rows come from LiteLLM's `StandardLoggingPayload` webhook, one row per LLM
call, not per CLI lifecycle event - there's no `turn_id` granularity from
this source (always `0`). `session_id` comes from the
`x-claude-code-session-id` request header when present (stable across an
orchestrator and its subagents), falling back to `trace_id` or the call's own
`litellm_call_id`. `user_id` is the LiteLLM virtual key's team/key alias
(`user_api_key_team_alias`/`user_api_key_alias`), falling back to
`"unknown-user"` if neither is set on the key.

```sql
SELECT timestamp, event_type, tool_name, agent_name, skill_name, status
FROM agent_events WHERE session_id = '<session-id>' ORDER BY timestamp;
```

`agent_events.latency_ms` is LiteLLM's own `response_time` for that call, in
milliseconds - just call latency, not tool execution or permission-wait time
(those were hook-era concepts tied to CLI lifecycle events, which this source
doesn't see). `agent_usage` also carries `stop_reason`/cache-tier breakdown -
see README "Per-request signals on `agent_usage`" for what each means and
where it comes from.

`session_git_branch` is the one table not sourced from LiteLLM - joined on
`session_id`, one row per session (snapshotted once at `SessionStart`, see
README "Git branch"):

```sql
SELECT git_branch FROM session_git_branch WHERE session_id = '<session-id>';
```