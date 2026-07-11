---
name: whatsup
description: Report token/cost spend and top spenders from the last 24h, from ClickHouse
version: 2.0.0
---

# whatsup

Report the last 24 hours of spend from the local agent-tracking stack.
Reads go through the `mcp-clickhouse` MCP server, not `docker exec` -
`ingest-api` is still write-only, but `mcp-clickhouse` is the dedicated
read path (see `README.md` → "MCP server (`mcp-clickhouse`)").

Call the `mcp__clickhouse__whatsup` tool with `hours: 24`. If the call
fails (connection refused, timeout), say the `mcp-clickhouse` service
isn't reachable - point at `docker compose ps` - instead of letting the
error pass silently.

The tool returns JSON shaped like:

```json
{
  "hours": 24,
  "total_tokens": 571486,
  "total_cost": 4.95318,
  "cost_has_gaps": false,
  "top_spenders": [
    {"user_id": "host-user", "cost": 4.95318, "tokens": 571486}
  ]
}
```

Then present a short report, not the raw tool output:

```
Last 24h:
  Tokens: <total_tokens>
  Cost:   $<total_cost, 2 decimals>

Top spenders:
  1. <user_id> - $<cost> (<tokens> tokens)
  2. ...
```

If `cost_has_gaps` is `true`, or any `top_spenders[].cost` is `null` (no
matching `model_pricing` row for one or more usage rows), still report the
tokens number and add a one-line note that some usage has no matching
price - see `README.md` → "Schema" for how to add one.
If `total_tokens` is `0`, say there's no usage in the last 24h plainly
instead of printing an empty report.
