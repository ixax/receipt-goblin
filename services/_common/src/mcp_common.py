"""Shared plumbing for the mcp-dev/mcp-stats FastMCP servers: a lazily-built,
thread-safe ClickHouse client, the /health route that pings it, and the
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
        self._client = None
        # Guards _client's check-then-set below: without it, two concurrent first
        # calls (plausible under FastMCP's multi-threaded tool dispatch) could each
        # see _client is None, each construct their own clickhouse_connect client,
        # and race to assign the instance attribute - the loser's client leaks
        # (never closed, its connection never reused, just abandoned).
        self._lock = threading.Lock()

    def get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:  # re-check: another thread may have won the race while we waited
                    self._client = clickhouse_connect.get_client(
                        host=self._host,
                        port=self._port,
                        username=self._username,
                        password=self._password,
                        database=self._database,
                    )
        return self._client


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
