---
name: clickhouse-sql
description: >
  ClickHouse SQL gotchas and sanctioned tool/script pointers for this repo's agent-tracking stack.
  TRIGGER - read before writing or debugging any ClickHouse SQL query against this stack.
  v2.1.0
---

## Gotchas

`GOTCHAS.md` holds findings - grep it by keyword (`RE2`, `JOIN ON`, `CTE`, `ARRAY JOIN`, `query_perf`, `mask_string_literals`) instead of reading it wholesale.
Add new findings there in the same terse symptom-cause-fix style.

## Sanctioned tools/scripts for this stack

- `mcp__dev__query` - the only sanctioned way to run a SELECT/WITH against live data (read-only by server-side validation, `mcp-dev` service).
  Accepts a single SQL string or a list of independent strings to batch.
  Never `docker exec .../clickhouse-client` or any other direct connection - see AGENTS.md's base ClickHouse-access rule.
- `mcp__dev__profile_query` - same validation as `query`, but returns cost metrics (`memory_usage_bytes`/`read_rows`/`read_bytes`/`query_duration_ms`) instead of result rows, for comparing two versions of a query.
- `services/grafana/scripts/query_perf.py` - resolve/save-run/diff/report tooling for tracking a dashboard query's cost over time.
  Driven by `sql-expert`, executed by `query-perf-runner`.
  Read its own docstring before guessing at flags.
- `services/grafana/scripts/parse_dashboard.py` - the only supported way to read `services/grafana/dashboards/agents_overview.json`'s panel structure (via the `dashboard-parser` agent) - don't hand-parse that file's large JSON inline.

## Escalation

If a query behaves inexplicably and nothing here, in `GOTCHAS.md`, or in `services/clickhouse/schema.sql` explains it, that's exactly what `sql-expert` exists for - it owns ClickHouse DBA-level investigation for this stack.
Escalate once the obvious explanations (typo, wrong column, wrong table) are ruled out, rather than burning a long debugging cycle rediscovering a version-specific quirk solo.
