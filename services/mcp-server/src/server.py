"""MCP server exposing read access to the agent-tracking ClickHouse database.

Claude Code talks to it over Streamable HTTP (see `.mcp.json`).
`query`/`profile_query` accept arbitrary SQL from the model, validated in
`_validate_readonly_sql` (SELECT/WITH only, no DDL/DML, no system tables
except `system.query_log` - a narrow, deliberate exception for
query-performance introspection, everything else under `system.*` stays
blocked - no remote/file/URL functions, every FROM/JOIN target in
`_ALLOWED_TABLES` - not just "some allowed name appears somewhere in the
query", which used to let a query join an allowlisted table together with
an arbitrary out-of-allowlist one). There is no separate read-only
ClickHouse user, so this validation is the only thing preventing a
write/DDL statement or an unauthorized table read - keep it strict rather
than convenient, and treat any change to
`_validate_readonly_sql`/`_strip_sql_comments`/`_referenced_tables` as
security-sensitive.
"""
import os
import re
import threading
import time
from pathlib import Path

import clickhouse_connect
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

# Defaults live in docker-compose.yml; always set by container start, no fallback needed.
CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_MCP_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_MCP_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]
MCP_SERVER_PORT = os.environ["MCP_SERVER_PORT"]

# The mcp SDK's DNS-rebinding protection defaults to allowed_hosts=[] (rejects
# every Host header) once transport_security is left unset - it used to be
# opt-in, now it's opt-out. Clients reach this container through load-balancer
# (nginx's `proxy_set_header Host $host` strips the port, so the Host header
# arriving here is bare "localhost"/"127.0.0.1"); the ":*" wildcard entries
# cover any client that connects with the port still on the Host header
# (e.g. straight to the container, bypassing nginx).
mcp = FastMCP(
    "clickhouse",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            f"mcp-server:{MCP_SERVER_PORT}",
        ],
    ),
)

# Served directly as uvicorn's top-level app, not mounted under FastAPI:
# mcp SDK's streamable_http_app() has a known bug when mounted as a sub-app
# (session manager never initializes, requests 404/507 - python-sdk#1367).
app = mcp.streamable_http_app()
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


async def health(request: Request) -> JSONResponse:
    try:
        # get_client().command(...) is a blocking network call - offloaded
        # to a thread so a slow/unreachable ClickHouse stalls only this
        # request, not the whole event loop (every other concurrent request
        # this worker is handling).
        await run_in_threadpool(get_client().command, "SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)


app.add_route("/health", health, methods=["GET"])

_client = None
# Guards _client's check-then-set below: without it, two concurrent first
# calls (plausible under FastMCP's multi-threaded tool dispatch) could each
# see _client is None, each construct their own clickhouse_connect client,
# and race to assign the module-level reference - the loser's client leaks
# (never closed, its connection never reused, just abandoned).
_client_lock = threading.Lock()

# Allow/deny lists for read-only SQL validation live in config.yml.
_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())

_ALLOWED_TABLES = set(_config["allowed_tables"])
_FORBIDDEN_KEYWORDS = tuple(_config["forbidden_keywords"])
_FORBIDDEN_TABLE_FUNCTIONS = tuple(_config["forbidden_table_functions"])
_MAX_ROWS_HARD_CAP = _config["max_rows_hard_cap"]


def _strip_sql_comments(sql: str) -> str:
    """Removes `--` line comments and `/* */` block comments, respecting
    single-quoted string literals (a `--`/`/*` inside a string is left
    alone, and a doubled `''` inside a string doesn't end it early). Must
    run before every check below, not after: an unstripped comment can
    otherwise plant a fake allowlist table name or hide a forbidden
    keyword from the regex checks that follow (confirmed exploitable - a
    payload like `SELECT 1) UNION ALL SELECT * FROM secret_table --
    agent_events` used to pass validation, the trailing comment supplying
    the required allowlist token while the real FROM target went
    unchecked, and once wrapped in `query()`'s `SELECT * FROM (...) AS
    _query_result LIMIT n`, that same `--` swallowed the wrapper's own
    `AS _query_result LIMIT n` too, so the row cap never applied either)."""
    out = []
    i, n = 0, len(sql)
    in_string = False
    while i < n:
        ch = sql[i]
        if in_string:
            out.append(ch)
            if ch == "'":
                if sql[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and sql[i + 1:i + 2] == "-":
            nl = sql.find("\n", i)
            i = nl if nl != -1 else n
            continue
        if ch == "/" and sql[i + 1:i + 2] == "*":
            end = sql.find("*/", i + 2)
            i = end + 2 if end != -1 else n
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# Table name(s) immediately following FROM/JOIN, including a comma-
# separated list (old-style implicit joins - `FROM a, b`) and an optional
# `schema.` qualifier. Deliberately does not match `FROM (subquery)` - `(`
# isn't a valid identifier-start character, so a derived table correctly
# contributes nothing here; whatever real table its own inner FROM/JOIN
# eventually bottoms out on still gets matched by this same regex applied
# to the full query text, since re.finditer covers the whole string.
_TABLE_REFS_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+"
    r"((?:[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?\s*,\s*)*"
    r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)",
    re.IGNORECASE,
)



# CTE definitions - `name AS (`. Only this syntax (identifier, then AS,
# then an opening paren) is used to introduce a CTE; a derived-table alias
# reads the other way round (`) AS name`), so this can't collide with a
# `FROM (subquery) AS x` alias.
_CTE_NAME_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", re.IGNORECASE)


def _cte_names(sql: str) -> set:
    return {m.group(1) for m in _CTE_NAME_RE.finditer(sql)}


def _referenced_tables(sql: str) -> set:
    tables = set()
    for match in _TABLE_REFS_RE.finditer(sql):
        for ident in match.group(1).split(","):
            ident = ident.strip()
            if ident:
                tables.add(ident.rsplit(".", 1)[-1])
    return tables - _cte_names(sql)


def _validate_readonly_sql(sql: str) -> str:
    stripped = _strip_sql_comments(sql).strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if ";" in stripped:
        raise ValueError("Only a single statement is allowed (no ';' inside the query).")
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", stripped):
        raise ValueError("Only SELECT/WITH queries are allowed.")

    upper = stripped.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if kw == "SYSTEM":
            # Handled separately below: SYSTEM <command> (FLUSH LOGS, RELOAD
            # DICTIONARY, ...) stays forbidden, but system.query_log is a
            # narrow, deliberate exception to the system-tables ban (see the
            # module docstring) - the negative lookahead here is what tells
            # the two apart, so "SYSTEM" can't just run through the generic
            # keyword loop like the rest of _FORBIDDEN_KEYWORDS.
            continue
        if re.search(rf"\b{kw}\b", upper):
            raise ValueError(f"'{kw}' is not allowed in read-only queries.")
    for fn in _FORBIDDEN_TABLE_FUNCTIONS:
        if re.search(rf"\b{fn}\s*\(", upper):
            raise ValueError(f"Table function '{fn}(...)' is not allowed.")
    if re.search(r"\bSYSTEM\b(?!\s*\.)", upper):
        raise ValueError("'SYSTEM' is not allowed in read-only queries.")
    if re.search(r"\b(INFORMATION_SCHEMA|MYSQL)\s*\.", upper):
        raise ValueError("Access to information_schema/mysql databases is not allowed.")
    for m in re.finditer(r"\bSYSTEM\s*\.\s*([A-Z_][A-Z0-9_]*)", upper):
        if m.group(1) != "QUERY_LOG":
            raise ValueError(
                f"'system.{m.group(1).lower()}' is not allowed - only "
                "system.query_log is allowlisted among system.* tables."
            )

    # Every FROM/JOIN target must be in the allowlist - not just "at least
    # one token somewhere in the query matches an allowed name" (the prior
    # check, which let a query join an allowed table together with an
    # arbitrary out-of-allowlist one and still pass, since there's no
    # separate read-only ClickHouse user backing this - see this module's
    # docstring).
    referenced = _referenced_tables(stripped)
    if not referenced:
        raise ValueError(
            f"Query must reference at least one of the known tables: {sorted(_ALLOWED_TABLES)}"
        )
    disallowed = referenced - _ALLOWED_TABLES
    if disallowed:
        raise ValueError(
            f"Query references table(s) not in the allowlist: {sorted(disallowed)}. "
            f"Allowed: {sorted(_ALLOWED_TABLES)}"
        )

    return stripped


def get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # re-check: another thread may have won the race while we waited
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

    # agent_usage.cost is LiteLLM's own cache-pricing-aware response_cost.
    # A prior manual price-table JOIN overcounted cost under prompt caching
    # (priced every input token at full rate) - don't reintroduce it.
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
    on this cap, since rows beyond it are silently dropped, not sampled.
    The response includes execution_time_ms, the server-side query duration."""
    validated = _validate_readonly_sql(sql)
    capped_rows = max(1, min(max_rows, _MAX_ROWS_HARD_CAP))

    client = get_client()
    start = time.perf_counter()
    try:
        result = client.query(
            f"SELECT * FROM ({validated}) AS _query_result LIMIT {capped_rows + 1}",
            settings={"max_execution_time": 10},
        )
    except Exception as exc:
        return {"error": str(exc), "execution_time_ms": round((time.perf_counter() - start) * 1000, 1)}
    execution_time_ms = round((time.perf_counter() - start) * 1000, 1)

    rows = result.result_rows
    truncated = len(rows) > capped_rows
    if truncated:
        rows = rows[:capped_rows]

    return {
        "columns": result.column_names,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "execution_time_ms": execution_time_ms,
    }


@mcp.tool()
def profile_query(sql: str) -> dict:
    """Run a read-only SQL query and report its real ClickHouse execution
    cost instead of result rows: memory_usage_bytes, read_rows, read_bytes,
    query_duration_ms. Same SELECT/WITH-only validation as `query`. Use
    this (not `query`) when the point is comparing how expensive two
    versions of a query are, e.g. before/after a rewrite - `query`'s
    execution_time_ms is wall-clock only and says nothing about memory or
    rows scanned.

    read_rows/read_bytes/query_duration_ms come straight back in
    ClickHouse's own response summary (no extra round trip). memory_usage_bytes
    needs system.query_log, which only becomes queryable after a
    SYSTEM FLUSH LOGS - this tool does that automatically, but the lookup is
    best-effort: if it fails (e.g. a permissions issue), memory_usage_bytes
    comes back null with a `memory_usage_warning` explaining why, rather
    than failing the whole call - the other three numbers are still valid
    either way."""
    validated = _validate_readonly_sql(sql)
    client = get_client()
    start = time.perf_counter()
    try:
        summary = client.command(f"{validated} FORMAT Null", settings={"max_execution_time": 10})
    except Exception as exc:
        return {"error": str(exc), "execution_time_ms": round((time.perf_counter() - start) * 1000, 1)}
    execution_time_ms = round((time.perf_counter() - start) * 1000, 1)
    info = summary.summary
    query_id = info.get("query_id", "")

    memory_usage_bytes = None
    memory_usage_warning = None
    try:
        client.command("SYSTEM FLUSH LOGS")
        log_result = client.query(
            "SELECT memory_usage FROM system.query_log "
            "WHERE query_id = {query_id:String} AND type = 'QueryFinish' "
            "ORDER BY event_time DESC LIMIT 1",
            parameters={"query_id": query_id},
        )
        if log_result.result_rows:
            memory_usage_bytes = log_result.result_rows[0][0]
        else:
            memory_usage_warning = "no matching system.query_log row after SYSTEM FLUSH LOGS"
    except Exception as exc:
        memory_usage_warning = str(exc)

    result = {
        "memory_usage_bytes": memory_usage_bytes,
        "read_rows": int(info.get("read_rows", 0)),
        "read_bytes": int(info.get("read_bytes", 0)),
        "query_duration_ms": round(int(info.get("elapsed_ns", 0)) / 1e6, 1),
        "execution_time_ms": execution_time_ms,
    }
    if memory_usage_warning:
        result["memory_usage_warning"] = memory_usage_warning
    return result
