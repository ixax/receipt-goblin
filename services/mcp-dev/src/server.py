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
`_validate_readonly_sql`/`_strip_sql_comments`/`_mask_string_literals`/
`_walk_sql`/`_referenced_tables` as security-sensitive.
"""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from prometheus_fastapi_instrumentator import Instrumentator

from common.logging_config import create_logger
from common.mcp_common import ClickHouseClientFactory, build_transport_security, make_health_route

# Defaults live in docker-compose.yml; always set by container start, no fallback needed.
CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_MCP_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_MCP_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]
MCP_DEV_PORT = os.environ["MCP_DEV_PORT"]

logger = create_logger("mcp_dev.server")

mcp = FastMCP(
    "dev",
    transport_security=build_transport_security(f"mcp-dev:{MCP_DEV_PORT}"),
)

# Served directly as uvicorn's top-level app, not mounted under FastAPI:
# mcp SDK's streamable_http_app() has a known bug when mounted as a sub-app
# (session manager never initializes, requests 404/507 - python-sdk#1367).
app = mcp.streamable_http_app()
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

_ch_factory = ClickHouseClientFactory(
    host=CLICKHOUSE_HOST,
    port=CLICKHOUSE_PORT,
    username=CLICKHOUSE_USER,
    password=CLICKHOUSE_PASSWORD,
    database=CLICKHOUSE_DATABASE,
)
get_client = _ch_factory.get_client

app.add_route("/health", make_health_route(get_client, logger), methods=["GET"])

# Allow/deny lists for read-only SQL validation live in config.yml.
_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())

_ALLOWED_TABLES = set(_config["allowed_tables"])
_FORBIDDEN_KEYWORDS = tuple(_config["forbidden_keywords"])
_FORBIDDEN_TABLE_FUNCTIONS = tuple(_config["forbidden_table_functions"])
_MAX_ROWS_HARD_CAP = _config["max_rows_hard_cap"]
_MAX_CONCURRENT_QUERIES = _config.get("max_concurrent_queries", 2)
_MAX_BATCH_QUERIES = _config.get("max_batch_queries", 10)
_MAX_EXECUTION_TIME_DEFAULT_S = _config.get("max_execution_time_default_s", 10)
_MAX_EXECUTION_TIME_HARD_CAP_S = _config.get("max_execution_time_hard_cap_s", 10)

# Bounds real concurrent load on ClickHouse across all four tools - both a
# batch call's internal fan-out and unrelated single-query calls arriving at
# the same time.
# FastMCP runs sync tool callables off the event loop thread (see
# ClickHouseClientFactory's threading.Lock comment, which already
# anticipates concurrent tool dispatch), so a threading.Semaphore correctly
# bounds real concurrency here.
_ch_semaphore = threading.Semaphore(_MAX_CONCURRENT_QUERIES)


def _walk_sql(sql: str, on_string_char, on_other_char, on_comment_start=None):
    """Shared core for `_strip_sql_comments`/`_mask_string_literals`.
    Walks `sql` once, tracking single-quoted string-literal state (a
    doubled `''` inside a string doesn't end it early) so both callers
    agree on exactly which characters are "inside a string" without
    duplicating that state machine.
    For each character, calls `on_string_char(ch)` if it's part of a
    string literal's content/quotes, else `on_other_char(ch)`.
    If `on_comment_start` is given, it's consulted at each non-string
    position to let the caller skip over `--`/`/* */` comments (returns
    the index to resume at, or None if `ch` doesn't start a comment
    there).
    Comment stripping only makes sense once, in `_strip_sql_comments`, so
    masking doesn't pass this and simply treats comment markers as
    ordinary characters (masking runs on already-comment-stripped text
    anyway)."""
    i, n = 0, len(sql)
    in_string = False
    while i < n:
        ch = sql[i]
        if in_string:
            on_string_char(ch)
            if ch == "'":
                if sql[i + 1:i + 2] == "'":
                    on_string_char("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            on_string_char(ch)
            i += 1
            continue
        if on_comment_start is not None:
            resume = on_comment_start(sql, i)
            if resume is not None:
                i = resume
                continue
        on_other_char(ch)
        i += 1


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

    def _comment_start(s, i):
        ch = s[i]
        if ch == "-" and s[i + 1:i + 2] == "-":
            nl = s.find("\n", i)
            return nl if nl != -1 else len(s)
        if ch == "/" and s[i + 1:i + 2] == "*":
            end = s.find("*/", i + 2)
            return end + 2 if end != -1 else len(s)
        return None

    _walk_sql(sql, out.append, out.append, _comment_start)
    return "".join(out)


def _mask_string_literals(sql: str) -> str:
    """Returns `sql` with the *contents* of every single-quoted string
    literal replaced by a neutral placeholder character (`x`), preserving
    the original length and the position of every non-string character -
    including the quote characters themselves and any doubled `''` escape.
    Every keyword/semicolon/table-function/SYSTEM/information_schema/
    table-reference check in `_validate_readonly_sql` must run against
    this masked text, not the raw (comment-stripped) text.
    `_strip_sql_comments` is quote-aware, but plain regex/substring
    checks downstream of it are not, so a literal `;` or a forbidden
    keyword spelled out as plain text inside a string (e.g. `'please
    DELETE this row manually'`, or HTML-entity output like
    `'&amp;lt;span&amp;gt;'` - both of which occur in real dashboard
    panels, see agents_overview.json panels 76/99) used to cause
    false-positive rejections.
    Only masks for validation purposes - the real query sent to
    ClickHouse always uses the original unmasked text, never this one."""
    out = []

    def _mask_char(ch):
        # Keep the quote characters themselves (needed for \b-style regexes
        # that anchor on surrounding punctuation/whitespace), mask content.
        out.append(ch if ch == "'" else "x")

    _walk_sql(sql, _mask_char, out.append)
    return "".join(out)


# Table name(s) immediately following FROM/JOIN, including a comma-
# separated list (old-style implicit joins - `FROM a, b`) and an optional
# `schema.` qualifier.
# Deliberately does not match `FROM (subquery)` - `(` isn't a valid
# identifier-start character, so a derived table correctly contributes
# nothing here; whatever real table its own inner FROM/JOIN eventually
# bottoms out on still gets matched by this same regex applied to the
# full query text, since re.finditer covers the whole string.
# The `ARRAY\s+JOIN\b` alternative (matched but not captured - group(1)
# is None for it) exists so ClickHouse's `ARRAY JOIN <array-expression>`
# clause (e.g. `ARRAY JOIN arrayMap(x -> ..., ...)`) doesn't fall through
# to the plain `JOIN` branch below and get its array-expression function
# name (e.g. `arrayMap`) misparsed as a joined table.
# Confirmed live on agents_overview.json panel 76 ("Trace"), which
# rejected with "table arrayMap not in allowlist" despite arrayMap being
# a function call, not a table reference.
_TABLE_REFS_RE = re.compile(
    r"\bARRAY\s+JOIN\b"
    r"|\b(?:FROM|JOIN)\s+"
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
        if match.group(1) is None:
            continue
        for ident in match.group(1).split(","):
            ident = ident.strip()
            if ident:
                tables.add(ident.rsplit(".", 1)[-1])
    return tables - _cte_names(sql)


def _validate_readonly_sql(sql: str) -> str:
    stripped = _strip_sql_comments(sql).strip()

    # Every check below runs against the quote-masked text, never the raw
    # `stripped` text.
    # String-literal *contents* can legitimately contain `;`, forbidden
    # keywords, forbidden table-function names, or
    # `system.`/`information_schema.`-looking text as plain data (see
    # `_mask_string_literals`'s docstring), and none of that should trip
    # validation.
    # Only the trailing-semicolon strip and the final return value use
    # `stripped` itself, so the query actually sent to ClickHouse is
    # always the real, unmasked text.
    masked = _mask_string_literals(stripped)
    if masked.endswith(";"):
        stripped = stripped[:-1].strip()
        masked = _mask_string_literals(stripped)
    if ";" in masked:
        raise ValueError("Only a single statement is allowed (no ';' inside the query).")
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", masked):
        raise ValueError("Only SELECT/WITH queries are allowed.")

    upper = masked.upper()
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
    referenced = _referenced_tables(masked)
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


def _do_query(sql: str, max_rows: int, max_duration_s: int) -> dict:
    capped_rows = max(1, min(max_rows, _MAX_ROWS_HARD_CAP))
    capped_duration_s = max(1, min(max_duration_s, _MAX_EXECUTION_TIME_HARD_CAP_S))

    client = get_client()
    start = time.perf_counter()
    try:
        with _ch_semaphore:
            result = client.query(
                f"SELECT * FROM ({sql}) AS _query_result LIMIT {capped_rows + 1}",
                settings={"max_execution_time": capped_duration_s},
            )
    except Exception as exc:
        logger.warning("query() failed: %s", exc)
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


def _do_query_safe(sql: str, max_rows: int, max_duration_s: int) -> dict:
    try:
        validated = _validate_readonly_sql(sql)
        return _do_query(validated, max_rows, max_duration_s)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def query(sql: str | list[str], max_rows: int = 200, max_duration_s: int = _MAX_EXECUTION_TIME_DEFAULT_S) -> dict:
    """Run one or several read-only SQL queries against the agent-tracking ClickHouse tables
    (agent_events, agent_usage, agent_messages).
    sql can be a single SQL string, or a list of independent SQL strings to run as one batch instead of looping single calls - up to max_batch_queries per call (an empty list, or one over that cap, returns a top-level {"error": "..."} without touching ClickHouse).
    Only a single SELECT/WITH statement is allowed per query - no DDL/DML, no system tables, no remote/file/URL table functions.
    Always returns {"results": [...]}, one entry per input query in input order (a single sql string is treated as a one-item batch): {"status": "ok", "result": {...}} on success, or {"status": "error", "error": "<message>"} on failure - one bad query in a batch doesn't abort the rest.
    Each successful result has columns/rows/row_count/truncated/execution_time_ms; results are capped at max_rows (default 200, hard cap 1000, applied per query) - truncated=True means there were more rows than that.
    Prefer aggregating/filtering in the query itself over relying on this cap, since rows beyond it are silently dropped, not sampled.
    max_duration_s sets ClickHouse's max_execution_time for this call (default 10s), clamped server-side to a hard cap - raise it for a known-slow query you're profiling/debugging rather than letting it silently time out.
    Real concurrency against ClickHouse is throttled server-side (shared with `profile_query`), so a large batch - or several callers at once - can't overload the local instance."""
    queries = [sql] if isinstance(sql, str) else sql
    if not queries:
        return {"error": "sql must be a non-empty string or list of strings."}
    if len(queries) > _MAX_BATCH_QUERIES:
        return {"error": f"Too many queries ({len(queries)}); max_batch_queries is {_MAX_BATCH_QUERIES}."}

    with ThreadPoolExecutor(max_workers=min(len(queries), _MAX_CONCURRENT_QUERIES)) as pool:
        raw_results = list(pool.map(lambda s: _do_query_safe(s, max_rows, max_duration_s), queries))

    results = [
        {"status": "error", **r} if "error" in r else {"status": "ok", "result": r}
        for r in raw_results
    ]
    return {"results": results}


def _do_profile(sql: str, max_duration_s: int) -> dict:
    capped_duration_s = max(1, min(max_duration_s, _MAX_EXECUTION_TIME_HARD_CAP_S))
    client = get_client()
    start = time.perf_counter()
    try:
        with _ch_semaphore:
            summary = client.command(f"{sql} FORMAT Null", settings={"max_execution_time": capped_duration_s})
    except Exception as exc:
        logger.warning("profile_query() failed: %s", exc)
        return {"error": str(exc), "execution_time_ms": round((time.perf_counter() - start) * 1000, 1)}
    execution_time_ms = round((time.perf_counter() - start) * 1000, 1)
    info = summary.summary
    query_id = info.get("query_id", "")

    memory_usage_bytes = None
    memory_usage_warning = None
    try:
        with _ch_semaphore:
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
        logger.warning("profile_query() memory_usage lookup failed: %s", exc)
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


@mcp.tool()
def profile_query(sql: str, max_duration_s: int = _MAX_EXECUTION_TIME_DEFAULT_S) -> dict:
    """Run a read-only SQL query and report its real ClickHouse execution cost instead of result rows: memory_usage_bytes, read_rows, read_bytes, query_duration_ms.
    Same SELECT/WITH-only validation as `query`.
    Use this (not `query`) when the point is comparing how expensive two versions of a query are, e.g. before/after a rewrite - `query`'s execution_time_ms is wall-clock only and says nothing about memory or rows scanned.

    read_rows/read_bytes/query_duration_ms come straight back in ClickHouse's own response summary (no extra round trip).
    memory_usage_bytes needs system.query_log, which only becomes queryable after a SYSTEM FLUSH LOGS - this tool does that automatically, but the lookup is best-effort: if it fails (e.g. a permissions issue), memory_usage_bytes comes back null with a `memory_usage_warning` explaining why, rather than failing the whole call - the other three numbers are still valid either way.
    max_duration_s sets ClickHouse's max_execution_time for this call (default 10s), clamped server-side to a hard cap - raise it for a known-slow query you're profiling rather than letting it silently time out."""
    validated = _validate_readonly_sql(sql)
    return _do_profile(validated, max_duration_s)
