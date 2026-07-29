"""MCP server exposing pre-built statistics over the agent-tracking
ClickHouse database.

Claude Code talks to it over Streamable HTTP (see `.mcp.json`). Unlike
`mcp-dev` (arbitrary read-only SQL, dev-only, no auth), this ships in
production alongside `litellm`/`grafana`, so every request (other than
`/health`/`/metrics`) must carry a valid LiteLLM virtual key - see
`_virtual_key_is_valid` below, the same check `services/webhook/src/server.py`
already uses for its own authenticated routes.
"""
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import clickhouse_connect
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Defaults live in docker-compose.yml; always set by container start, no fallback needed.
CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_MCP_STATS_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_MCP_STATS_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]
MCP_STATS_PORT = os.environ["MCP_STATS_PORT"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]

# The mcp SDK's DNS-rebinding protection defaults to allowed_hosts=[] (rejects
# every Host header) once transport_security is left unset - it used to be
# opt-in, now it's opt-out. Clients reach this container through load-balancer
# (nginx's `proxy_set_header Host $host` strips the port, so the Host header
# arriving here is bare "localhost"/"127.0.0.1"); the ":*" wildcard entries
# cover any client that connects with the port still on the Host header
# (e.g. straight to the container, bypassing nginx).
mcp = FastMCP(
    "stats",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            f"mcp-stats:{MCP_STATS_PORT}",
        ],
    ),
)


def _virtual_key_is_valid(key: str) -> bool:
    # Checks the caller's key against LiteLLM's own /key/info instead of
    # inventing a signing scheme - same check services/webhook/src/server.py
    # uses for its own authenticated routes.
    if not key:
        return False
    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/key/info?key={key}",
        # LiteLLM's litellm_key_header_name is x-litellm-api-key (see AGENTS.md);
        # plain Authorization: Bearer here is rejected as malformed.
        headers={"x-litellm-api-key": f"Bearer {LITELLM_MASTER_KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            info = json.load(resp).get("info") or {}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return False
    if info.get("blocked"):
        return False
    expires = info.get("expires")
    if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
        return False
    return True


class AuthMiddleware(BaseHTTPMiddleware):
    # /health and /metrics stay unauthenticated - the docker healthcheck and
    # Prometheus scraping have no virtual key to send. Everything else (the
    # MCP JSON-RPC endpoint itself) requires one.
    _EXEMPT_PATHS = {"/health", "/metrics"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._EXEMPT_PATHS:
            return await call_next(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not _virtual_key_is_valid(token):
            return JSONResponse({"detail": "invalid or missing virtual key"}, status_code=401)
        return await call_next(request)


# Served directly as uvicorn's top-level app, not mounted under FastAPI:
# mcp SDK's streamable_http_app() has a known bug when mounted as a sub-app
# (session manager never initializes, requests 404/507 - python-sdk#1367).
app = mcp.streamable_http_app()
app.add_middleware(AuthMiddleware)
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


def _operation_label(row: dict) -> str:
    # One human-readable label per row - command/skill/agent/mcp-tool name
    # if this call was made BY one of those (i.e. it's a subagent/skill/
    # command's own turn), else fall back to agent_events.calculated_type
    # (joined in by litellm_call_id) to say what the call actually DID -
    # spawned a subagent, ran a tool, answered plainly, etc. A bare
    # "llm:<model>" without this join is nearly always "claude-sonnet-5"
    # repeated with no useful signal, since most turns are plain model
    # calls from agent_usage's own columns alone.
    if row["command_name"]:
        return f"/{row['command_name']}"
    if row["skill_name"]:
        return f"skill:{row['skill_name']}"
    if row["agent_name"]:
        return f"agent:{row['agent_name']}"
    if row["mcp_tool_name"]:
        return f"mcp:{row['mcp_tool_name']}"
    calculated_type = row["calculated_type"]
    if calculated_type == "agent_spawn" and row["spawned_agent"]:
        return f"spawn:{row['spawned_agent']}"
    if calculated_type == "tool_call" and row["tool_name"]:
        return f"tool:{row['tool_name']}"
    if calculated_type == "llm_answer":
        return "conversation reply"
    if calculated_type == "ask_user_question":
        return "ask user question"
    if calculated_type:
        return calculated_type
    return f"llm:{row['model']}"


@mcp.tool()
def me(session_id: str) -> dict:
    """Report cost/token spend for the current Claude Code session plus the
    last 30 days, and the 5 most expensive operations in this session.

    session_id must be the value of the CLAUDE_CODE_SESSION_ID environment
    variable (read it via a shell command first, then pass it here - this
    tool has no other way to know which session is "current").

    Returns:
      session: {cost, input_tokens, output_tokens} summed over every
        agent_usage row for this session_id (the whole session so far, not
        just the last N hours).
      last_30_days: same three fields, summed across all usage (every
        session/user) in the last 30 days - same semantics whatsup used to
        report for cost/tokens, just fixed to a 30-day window in this tool.
      top_operations: up to 5 rows from this session, ordered by cost
        descending - each one a single agent_usage row (one LiteLLM call),
        labeled by whichever of command/skill/agent/mcp-tool triggered it,
        or (joined from agent_events by litellm_call_id) what the call
        itself did - spawned a subagent, ran a tool, answered plainly, etc.
        (see _operation_label) - falling back to "llm:<model>" only when
        none of that is available.
    """
    client = get_client()

    session_row = client.query(
        "SELECT sum(cost), sum(input_tokens), sum(output_tokens) FROM agent_usage "
        "WHERE session_id = {session_id:String}",
        parameters={"session_id": session_id},
    ).result_rows[0]

    month_row = client.query(
        "SELECT sum(cost), sum(input_tokens), sum(output_tokens) FROM agent_usage "
        "WHERE timestamp >= now() - INTERVAL 30 DAY",
    ).result_rows[0]

    top_result = client.query(
        "SELECT au.timestamp, au.model, au.agent_name, au.skill_name, au.command_name, "
        "au.mcp_tool_name, au.cost, au.input_tokens, au.output_tokens, "
        "ae.tool_name, ae.calculated_type, "
        "JSONExtractString(ae.calculated_payload, 'subagent_type') AS spawned_agent "
        "FROM agent_usage AS au "
        "LEFT JOIN agent_events AS ae "
        "ON au.litellm_call_id = ae.litellm_call_id AND au.session_id = ae.session_id "
        "WHERE au.session_id = {session_id:String} "
        "ORDER BY au.cost DESC LIMIT 5",
        parameters={"session_id": session_id},
    )
    top_operations = []
    for r in top_result.result_rows:
        row = dict(zip(top_result.column_names, r))
        top_operations.append({
            "label": _operation_label(row),
            "cost": row["cost"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "timestamp": row["timestamp"].isoformat(),
        })

    return {
        "session_id": session_id,
        "session": {
            "cost": session_row[0],
            "input_tokens": session_row[1] or 0,
            "output_tokens": session_row[2] or 0,
        },
        "last_30_days": {
            "cost": month_row[0],
            "input_tokens": month_row[1] or 0,
            "output_tokens": month_row[2] or 0,
        },
        "top_operations": top_operations,
    }
