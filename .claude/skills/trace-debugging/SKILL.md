---
name: trace-debugging
description: >
  Troubleshooting the chain of agent calls via session_id/trace_id/turn_id/sequence_id.
  Use this when investigating the latency of a specific step, the order of events in a session, or when debugging a specific trace in agent_events/agent_usage.
version: 1.0.0
---

## Chain tracing / identity

Every row carries `session_id`, `trace_id` (parent's `session_id` for
subagent trees), `parent_session_id`, `turn_id` (increments per
`UserPromptSubmit`), `sequence_id` (increments per event in a turn).
`X-User-Id` is the logged-in Claude account email (`oauthAccount.emailAddress`
in the global `~/.claude.json`, undocumented/internal, best-effort), falling
back to `"{hostname}-{username}"` from `hooks/common.py:get_user_id()` if
that's unavailable.

```sql
SELECT turn_id, sequence_id, timestamp, event_type, tool_name, agent_name, skill_name, status
FROM agent_events WHERE session_id = '<session-id>' ORDER BY turn_id, sequence_id;
```

`agent_events.latency_ms` is overloaded by `event_type`: tool execution
time on `PostToolUse`/`PostToolUseFailure`, permission-prompt wait time on
`PreToolUse`/`PermissionDenied`, and turn duration (`UserPromptSubmit` ->
`Stop`) on `Stop`/`StopFailure` - all three reuse the same generic
`mark_tool_start`/`pop_tool_latency_ms` timer in `common.py`, just keyed
differently. `agent_usage` also carries `stop_reason`/`service_tier`/
`speed`/cache-tier breakdown/`web_search_requests`/`web_fetch_requests` -
see README "Per-request signals on `agent_usage`" for what each means and
where it comes from.