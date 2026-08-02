# `mcp-dev`

Dev-only, unauthenticated FastMCP server (doesn't exist in `ENVIRONMENT=production` - see `agent_docs/services/load-balancer.md`'s `MCP_DEV_PORT` note), registered at `http://localhost:8001/mcp` in `.mcp.json`.
Connects as its own `mcp` ClickHouse role (unrestricted read - see `agent_docs/services/clickhouse.md`).
App-level SQL validation below is the only thing preventing an unauthorized table read or write/DDL statement - there's no separate read-only DB user.

## `src/server.py`

Two tools, sharing the same validation and a module-level throttle:

- `query(sql, max_rows)` - `sql` is a single SELECT/WITH string, or a list of independent SQL strings run as one batch.
  Always returns `{"results": [...]}`, one entry per input query in input order (a single string is a one-item batch): `{"status": "ok", "result": {...}}` on success (the per-query `columns`/`rows`/`row_count`/`truncated`/`execution_time_ms` dict) or `{"status": "error", "error": "<message>"}` on failure - one bad query doesn't abort the rest.
  Up to `max_batch_queries` queries per call; an empty list or too many returns a top-level `{"error": "..."}` without touching ClickHouse.
  The tool's own docstring is the single source of truth for this shape - don't duplicate it per-consumer, it's read fresh by whatever agent calls the tool.
- `profile_query(sql)` - same validation, returns `memory_usage_bytes`/`read_rows`/`read_bytes`/`query_duration_ms` from `system.query_log` instead of result rows.
  No batch form: these queries are individually heavy by design (perf profiling), so batching them against a 2-slot semaphore buys nothing over calling `profile_query` once per query - `query-perf-runner` still loops single calls.

`_validate_readonly_sql`/`_strip_sql_comments`/`_referenced_tables` are security-sensitive - treat any change as such.
`_strip_sql_comments` must run before every other check: an unstripped `--`/`/* */` comment can otherwise plant a fake allowlist table name or hide a forbidden keyword from the regex checks that follow.
Confirmed exploitable - a payload like `SELECT 1) UNION ALL SELECT * FROM secret_table -- agent_events` used to pass validation, the trailing comment supplying the required allowlist token while the real `FROM` target went unchecked.
Every `FROM`/`JOIN` target must be in `_ALLOWED_TABLES`, not just "some allowlisted name appears somewhere in the query" - that used to let a query join an allowlisted table together with an arbitrary out-of-allowlist one.
Never loosen `_validate_readonly_sql` in this file.

The single-query bodies (past validation) live in `_do_query`/`_do_profile`, called from `query`'s per-item fan-out and from `profile_query` directly.
A module-level `threading.Semaphore(_MAX_CONCURRENT_QUERIES)` (`_ch_semaphore`) wraps the actual blocking ClickHouse calls inside those two helpers.
It's shared by both tools, not just within one `query` batch - the goal is bounding real concurrent load on ClickHouse regardless of whether it comes from a batch's internal fan-out or from two unrelated agents calling the server at the same moment.
This works because FastMCP already runs sync tool callables off the event loop thread (see `ClickHouseClientFactory`'s own threading.Lock comment, which anticipates concurrent tool dispatch), so a `threading.Semaphore` correctly bounds real concurrency either way.

`query` validates the batch itself first (non-empty, at most `max_batch_queries`) and returns a top-level `{"error": "..."}` without touching ClickHouse if that check fails.
Otherwise each item runs through `_do_query_safe` - a thin wrapper that catches the validation `ValueError` `_do_query` doesn't catch today (by design), so one bad query in a batch doesn't abort the rest.
Items fan out via a `ThreadPoolExecutor(max_workers=min(len(queries), _MAX_CONCURRENT_QUERIES))` - the semaphore, not the pool size, is the real enforcement point, the pool just avoids spawning more idle threads than can ever run at once.

Passing an array instead of looping single `query` calls is preferred whenever an agent needs several independent, individually-cheap queries per task (e.g. `loadtest-sql` firing N timing iterations per widget).
The server-side throttle replaces any hand-written "keep it to N in flight" convention, so callers no longer need to self-limit.

## `config.yml`

`query`'s SQL validation rules, loaded at import time:

- `allowed_tables` - every table this stack writes to, plus a narrow `query_log` exception for query-performance introspection.
- `forbidden_keywords` - `INSERT`/`UPDATE`/`DELETE`/`ALTER`/`DROP`/etc, word-boundary matched anywhere, subqueries/CTEs included, not just the first keyword.
- `forbidden_table_functions` - `REMOTE`/`URL`/`FILE`/`S3`/etc, anything reading outside the DB.
- `max_rows_hard_cap` - 1000.
- `max_concurrent_queries` - 2, the shared semaphore size bounding real ClickHouse concurrency across both tools.
- `max_batch_queries` - 10, hard cap on queries per `query` call when `sql` is a list.
