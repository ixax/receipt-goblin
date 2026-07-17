"""MCP server exposing read access to the agent-tracking ClickHouse database.

Runs as its own docker-compose service, alongside `webhook` (write-only)
and `grafana`. Claude Code talks to it over Streamable HTTP (see `.mcp.json`
at the project root), instead of `docker exec`-ing into the ClickHouse
container.

Two tools: `whatsup`, which only ever runs its three fixed queries, and
`query`, which accepts arbitrary SQL from the model but is validated in
`_validate_readonly_sql` below (SELECT/WITH only, single statement, no
DDL/DML keywords, no system tables, no remote/file/URL table functions).
There is no separate read-only ClickHouse user (docker-compose.yml uses one
shared user for webhook/mcp-server/grafana - see its comments), so
this code-level validation is the only thing standing between `query` and
a write/DDL statement - keep it strict rather than convenient.
"""
import os
import re
from pathlib import Path

import clickhouse_connect
import yaml
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# Defaults live in docker-compose.yml (single source of truth); these vars
# are always set by the time this container starts, so no fallback here.
CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]

mcp = FastMCP("clickhouse")

# Standalone ASGI app, run via uvicorn (see Dockerfile / docker-compose.yml) -
# NOT mounted under a separate FastAPI app: the official mcp SDK's
# streamable_http_app() has a known bug when mounted as a sub-app (session
# manager never initializes, requests 404/507 -
# https://github.com/modelcontextprotocol/python-sdk/issues/1367). Serving it
# directly as uvicorn's top-level `app` sidesteps that entirely.
app = mcp.streamable_http_app()


async def health(request: Request) -> JSONResponse:
    try:
        get_client().command("SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)


app.add_route("/health", health, methods=["GET"])

_client = None

# Read-only SQL validation rules - see config.yml (allow/deny lists live
# there now, since that's the file you actually edit to tune them).
_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())

_ALLOWED_TABLES = set(_config["allowed_tables"])
_FORBIDDEN_KEYWORDS = tuple(_config["forbidden_keywords"])
_FORBIDDEN_TABLE_FUNCTIONS = tuple(_config["forbidden_table_functions"])
_MAX_ROWS_HARD_CAP = _config["max_rows_hard_cap"]


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

    # LiteLLM already computes an accurate per-call cost (cache-pricing-aware)
    # in its own response_cost, stored verbatim as agent_usage.cost - no
    # ASOF JOIN against a manually-maintained price table needed or wanted
    # (that table used to exist and silently overcounted cost by several
    # times whenever prompt caching was in play, since it priced every
    # input token at full rate with no cache discount).
    cost_row = client.query(
        "SELECT sum(cost) FROM agent_usage "
        "WHERE timestamp >= now() - INTERVAL %(hours)s HOUR",
        parameters={"hours": hours},
    ).result_rows[0]
    total_cost = cost_row[0]

    top_rows = client.query(
        "SELECT user_id, sum(cost) AS cost, sum(input_tokens + output_tokens) AS tokens "
        "FROM agent_usage "
        "WHERE timestamp >= now() - INTERVAL %(hours)s HOUR "
        "GROUP BY user_id ORDER BY cost DESC LIMIT 5",
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
    (agent_events, agent_usage, agent_messages). Only a single SELECT/WITH
    statement is allowed - no DDL/DML, no system tables, no remote/file/URL
    table functions. Results are capped at max_rows (default 200, hard cap
    1000); set truncated=True in the response means there were more rows
    than that. Prefer aggregating/filtering in the query itself over relying
    on this cap, since rows beyond it are silently dropped, not sampled."""
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
