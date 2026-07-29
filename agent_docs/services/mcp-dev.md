# `mcp-dev`

Dev-only, unauthenticated FastMCP server (doesn't exist in `ENVIRONMENT=production` - see `agent_docs/services/load-balancer.md`'s `MCP_DEV_PORT` note), registered at `http://localhost:8001/mcp` in `.mcp.json`.
Connects as its own `mcp` ClickHouse role (unrestricted read - see `agent_docs/services/clickhouse.md`), so app-level SQL validation below is the only thing preventing an unauthorized table read or write/DDL statement - there's no separate read-only DB user.

## `src/server.py`

Two tools: `query(sql, max_rows)` (arbitrary SELECT/WITH) and `profile_query(sql)` (same validation, returns `memory_usage_bytes`/`read_rows`/`read_bytes`/`query_duration_ms` from `system.query_log` instead of result rows).
`_validate_readonly_sql`/`_strip_sql_comments`/`_referenced_tables` are security-sensitive - treat any change as such.
`_strip_sql_comments` must run before every other check: an unstripped `--`/`/* */` comment can otherwise plant a fake allowlist table name or hide a forbidden keyword from the regex checks that follow (confirmed exploitable - a payload like `SELECT 1) UNION ALL SELECT * FROM secret_table -- agent_events` used to pass validation, the trailing comment supplying the required allowlist token while the real `FROM` target went unchecked).
Every `FROM`/`JOIN` target must be in `_ALLOWED_TABLES` - not just "some allowlisted name appears somewhere in the query", which used to let a query join an allowlisted table together with an arbitrary out-of-allowlist one.
**Never loosen `_validate_readonly_sql` in this file.**

## `config.yml`

`query`'s SQL validation rules, loaded at import time:

- `allowed_tables` - every table this stack writes to, plus a narrow `query_log` exception for query-performance introspection.
- `forbidden_keywords` - `INSERT`/`UPDATE`/`DELETE`/`ALTER`/`DROP`/etc, word-boundary matched anywhere, subqueries/CTEs included, not just the first keyword.
- `forbidden_table_functions` - `REMOTE`/`URL`/`FILE`/`S3`/etc, anything reading outside the DB.
- `max_rows_hard_cap` - 1000.
