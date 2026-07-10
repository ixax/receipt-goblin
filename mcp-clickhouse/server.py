"""MCP server exposing read access to the agent-tracking ClickHouse database.

Runs as its own docker-compose service, alongside `ingest-api` (write-only)
and `grafana`. Claude Code talks to it over Streamable HTTP (see `.mcp.json`
at the project root), instead of `docker exec`-ing into the ClickHouse
container the way the `whatsup` skill used to.

Two tools: `whatsup`, which only ever runs its three fixed queries, and
`query`, which accepts arbitrary SQL from the model but is validated in
`_validate_readonly_sql` below (SELECT/WITH only, single statement, no
DDL/DML keywords, no system tables, no remote/file/URL table functions).
There is no separate read-only ClickHouse user (docker-compose.yml uses one
shared user for ingest-api/mcp-clickhouse/grafana - see its comments), so
this code-level validation is the only thing standing between `query` and
a write/DDL statement - keep it strict rather than convenient.
"""
import os
import re

import clickhouse_connect
from mcp.server.fastmcp import FastMCP

# Defaults live in docker-compose.yml (single source of truth); these vars
# are always set by the time this container starts, so no fallback here.
CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]

mcp = FastMCP("clickhouse", host="0.0.0.0", port=8001)

_client = None

# The tables this stack actually writes to - anything else (system.*,
# information_schema.*, a typo'd table name) is out of scope for `query`.
_ALLOWED_TABLES = {
    "agent_events", "agent_usage", "agent_messages",
    "agent_registry", "skill_registry", "model_pricing",
}

# Word-boundary matched against the uppercased query - catches these
# anywhere (subqueries, CTEs), not just as the first keyword.
_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "TRUNCATE", "CREATE",
    "GRANT", "REVOKE", "ATTACH", "DETACH", "KILL", "RENAME", "OPTIMIZE",
    "SYSTEM", "EXCHANGE", "RESTORE", "BACKUP", "SET",
)

# ClickHouse table functions that read from outside the database itself
# (arbitrary files/URLs/other DBs) - always forbidden regardless of the
# keyword list above, since they're function calls, not keywords.
_FORBIDDEN_TABLE_FUNCTIONS = (
    "REMOTE", "REMOTESECURE", "CLUSTER", "CLUSTERALLREPLICAS", "URL",
    "FILE", "S3", "HDFS", "MYSQL", "POSTGRESQL", "ODBC", "JDBC", "INPUT",
)

_MAX_ROWS_HARD_CAP = 1000


def _validate_readonly_sql(sql: str) -> str:
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if ";" in stripped:
        raise ValueError("Only a single statement is allowed (no ';' inside the query).")
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", stripped):
        raise ValueError("Only SELECT/WITH queries are allowed.")

    upper = stripped.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            raise ValueError(f"'{kw}' is not allowed in read-only queries.")
    for fn in _FORBIDDEN_TABLE_FUNCTIONS:
        if re.search(rf"\b{fn}\s*\(", upper):
            raise ValueError(f"Table function '{fn}(...)' is not allowed.")
    if re.search(r"\b(SYSTEM|INFORMATION_SCHEMA|MYSQL)\s*\.", upper):
        raise ValueError("Access to system/information_schema/mysql databases is not allowed.")

    referenced = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", stripped))
    if referenced.isdisjoint(_ALLOWED_TABLES):
        raise ValueError(
            f"Query must reference at least one of the known tables: {sorted(_ALLOWED_TABLES)}"
        )

    return stripped


def get_client():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=CLICKHOUSE_HOST,
            port=CLICKHOUSE_PORT,
            username=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            database=CLICKHOUSE_DATABASE,
        )
    return _client


@mcp.tool()
def whatsup(hours: int = 24) -> dict:
    """Report total token usage/cost and the top 5 spenders over the last
    N hours (default 24) from the agent-tracking ClickHouse database."""
    client = get_client()

    tokens_row = client.query(
        "SELECT sum(input_tokens + output_tokens) FROM agent_usage "
        "WHERE timestamp >= now() - INTERVAL %(hours)s HOUR",
        parameters={"hours": hours},
    ).result_rows[0]
    total_tokens = tokens_row[0] or 0

    cost_row = client.query(
        "SELECT sum(u.input_tokens * p.price_in_per_mtok / 1e6 "
        "+ u.output_tokens * p.price_out_per_mtok / 1e6) "
        "FROM agent_usage u "
        "ASOF LEFT JOIN model_pricing p ON u.model = p.model AND u.timestamp >= p.effective_from "
        "WHERE u.timestamp >= now() - INTERVAL %(hours)s HOUR",
        parameters={"hours": hours},
    ).result_rows[0]
    total_cost = cost_row[0]

    top_rows = client.query(
        "SELECT u.user_id, "
        "sum(u.input_tokens * p.price_in_per_mtok / 1e6 + u.output_tokens * p.price_out_per_mtok / 1e6) AS cost, "
        "sum(u.input_tokens + u.output_tokens) AS tokens "
        "FROM agent_usage u "
        "ASOF LEFT JOIN model_pricing p ON u.model = p.model AND u.timestamp >= p.effective_from "
        "WHERE u.timestamp >= now() - INTERVAL %(hours)s HOUR "
        "GROUP BY u.user_id ORDER BY cost DESC LIMIT 5",
        parameters={"hours": hours},
    ).result_rows

    top_spenders = [
        {"user_id": row[0], "cost": row[1], "tokens": row[2]} for row in top_rows
    ]

    return {
        "hours": hours,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "cost_has_gaps": total_cost is None and total_tokens > 0,
        "top_spenders": top_spenders,
    }


@mcp.tool()
def query(sql: str, max_rows: int = 200) -> dict:
    """Run a read-only SQL query against the agent-tracking ClickHouse tables
    (agent_events, agent_usage, agent_messages, agent_registry,
    skill_registry, model_pricing). Only a single SELECT/WITH statement is
    allowed - no DDL/DML, no system tables, no remote/file/URL table
    functions. Results are capped at max_rows (default 200, hard cap 1000);
    set truncated=True in the response means there were more rows than that.
    Prefer aggregating/filtering in the query itself over relying on this
    cap, since rows beyond it are silently dropped, not sampled."""
    validated = _validate_readonly_sql(sql)
    capped_rows = max(1, min(max_rows, _MAX_ROWS_HARD_CAP))

    client = get_client()
    try:
        result = client.query(
            f"SELECT * FROM ({validated}) AS _query_result LIMIT {capped_rows + 1}",
            settings={"max_execution_time": 10},
        )
    except Exception as exc:
        return {"error": str(exc)}

    rows = result.result_rows
    truncated = len(rows) > capped_rows
    if truncated:
        rows = rows[:capped_rows]

    return {
        "columns": result.column_names,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
