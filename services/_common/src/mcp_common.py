"""Shared plumbing for the mcp-dev/mcp-stats FastMCP servers: a lazily-built,
per-thread ClickHouse client, the /health route that pings it, and the
DNS-rebinding-protection transport_security settings both servers need.
"""
import threading

import clickhouse_connect
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse


class ClickHouseClientFactory:
    def __init__(self, host: str, port: int, username: str, password: str, database: str):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._database = database
        # One client per thread, not one shared instance.
        # clickhouse_connect's Client tracks in-flight query/session state on the instance itself.
        # Two threads sharing one Client and querying at the same moment hit ClickHouse's own "Attempt to execute concurrent queries within the same session" error.
        # Confirmed by actually running concurrent queries through a shared client during mcp-dev's array-input query() rollout.
        # threading.local gives each worker thread (FastMCP's tool dispatch pool, and query()'s own internal ThreadPoolExecutor for a batch) its own lazily-built client instead.
        self._local = threading.local()

    def get_client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            client = clickhouse_connect.get_client(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                database=self._database,
            )
            self._local.client = client
        return client


def make_health_route(get_client, logger=None):
    async def health(request: Request) -> JSONResponse:
        try:
            # get_client().command(...) is a blocking network call - offloaded
            # to a thread so a slow/unreachable ClickHouse stalls only this
            # request, not the whole event loop (every other concurrent request
            # this worker is handling).
            await run_in_threadpool(get_client().command, "SELECT 1")
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            if logger is not None:
                logger.exception("health check failed")
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)

    return health


def build_transport_security(host_port: str) -> TransportSecuritySettings:
    # The mcp SDK's DNS-rebinding protection defaults to allowed_hosts=[] (rejects
    # every Host header) once transport_security is left unset - it used to be
    # opt-in, now it's opt-out. Clients reach this container through load-balancer
    # (nginx's `proxy_set_header Host $host` strips the port, so the Host header
    # arriving here is bare "localhost"/"127.0.0.1"); the ":*" wildcard entries
    # cover any client that connects with the port still on the Host header
    # (e.g. straight to the container, bypassing nginx).
    return TransportSecuritySettings(
        allowed_hosts=[
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            host_port,
        ],
    )
