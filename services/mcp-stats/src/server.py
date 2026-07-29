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
