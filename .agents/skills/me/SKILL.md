---
name: me
description: >
  Report cost/token spend for the current session plus the last 30 days, and the top 5 most expensive operations in this session.
  Never triggers proactively - invoked explicitly by typing /me.
  v2.0.2
---

Report cost/tokens for the current session, the last 30 days across all sessions/users, and the 5 most expensive operations in this session.
Reads go through the `mcp-stats` MCP server, not `docker exec` - `webhook` is still write-only, but `mcp-stats` is the dedicated read path for fixed statistics (see `README.md`, "MCP servers (`mcp-dev`, `mcp-stats`)").

The `mcp__stats__me` tool has no way to know which session is "current" on its own - it requires a `session_id` argument.
First run a shell command to read the `CLAUDE_CODE_SESSION_ID` environment variable (e.g. `echo $CLAUDE_CODE_SESSION_ID` via Bash), then call `mcp__stats__me` with that value as `session_id`.

If the call fails (connection refused, timeout, 401), say the `mcp-stats` service isn't reachable/authorized - point at `docker compose ps`/checking `LITELLM_VIRTUAL_KEY` - instead of letting the error pass silently.

The tool returns JSON shaped like:

```json
{
  "session_id": "abc-123",
  "session": {"cost": 1.23, "input_tokens": 50000, "output_tokens": 8000},
  "last_30_days": {"cost": 340.5, "input_tokens": 12000000, "output_tokens": 900000},
  "top_operations": [
    {"label": "/whatsup", "cost": 0.45, "input_tokens": 20000, "output_tokens": 2000, "timestamp": "2026-07-29T10:15:00.123"},
    {"label": "agent:sql-expert", "cost": 0.30, "input_tokens": 15000, "output_tokens": 1500, "timestamp": "..."}
  ]
}
```

Then present a short report, not the raw tool output:

```
This session:
  Cost:   $<session.cost, 2 decimals>
  Tokens: <input_tokens> in / <output_tokens> out

Last 30 days:
  Cost:   $<last_30_days.cost, 2 decimals>
  Tokens: <input_tokens> in / <output_tokens> out

Top 5 most expensive operations this session:
  1. <label> - $<cost> (<input_tokens> in / <output_tokens> out)
  2. ...
```

If `top_operations` is empty, say there's no usage yet in this session instead of printing an empty list.
If `session.cost` is null or `0`, say plainly there's been no spend yet this session rather than printing a zeroed-out report.
