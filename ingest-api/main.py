"""Ingest API for the local AI agent cost/efficiency tracking stack.

Receives events and usage reports from Claude Code hooks running on the host
and writes them into ClickHouse. Kept intentionally thin: validation only,
no business logic (cost calculation happens at query time in Grafana).
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, List, Optional, Union

import clickhouse_connect
from fastapi import FastAPI, Header, Request
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest-api")

# Defaults live in docker-compose.yml (single source of truth); these vars
# are always set by the time this container starts, so no fallback here.
CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]

app = FastAPI(title="agent-tracking-ingest-api")

_client = None


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


def now_ms() -> datetime:
    return datetime.now(timezone.utc)


class EventIn(BaseModel):
    timestamp: Optional[datetime] = None
    session_id: str = ""
    trace_id: str = ""
    parent_session_id: str = ""
    turn_id: int = 0
    sequence_id: int = 0
    event_type: str = ""
    tool_name: str = ""
    agent_name: str = ""
    agent_version: str = ""
    skill_name: str = ""
    skill_version: str = ""
    status: str = ""
    latency_ms: Optional[int] = None
    raw_payload: Any = Field(default_factory=dict)


class UsageIn(BaseModel):
    timestamp: Optional[datetime] = None
    session_id: str = ""
    trace_id: str = ""
    turn_id: int = 0
    model: str = ""
    agent_name: str = ""
    agent_version: str = ""
    skill_name: str = ""
    skill_version: str = ""
    mcp_tool_name: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str = ""
    service_tier: str = ""
    speed: str = ""
    cache_creation_1h_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0


class MessageIn(BaseModel):
    timestamp: Optional[datetime] = None
    session_id: str = ""
    trace_id: str = ""
    turn_id: int = 0
    agent_name: str = ""
    agent_version: str = ""
    skill_name: str = ""
    skill_version: str = ""
    prompt_text: str = ""
    response_text: str = ""


class AgentRegistryIn(BaseModel):
    agent_name: str
    version: str
    description: str = ""
    source_file: str = ""


class SkillRegistryIn(BaseModel):
    skill_name: str
    version: str
    description: str = ""
    source_file: str = ""


def _user_id(x_user_id: Optional[str]) -> str:
    return x_user_id or "unknown-user"


@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    logger.info("%s %s -> %s (user=%s)", request.method, request.url.path,
                response.status_code, request.headers.get("x-user-id", "-"))
    return response


@app.get("/health")
def health():
    try:
        get_client().command("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.post("/ingest/event")
def ingest_event(body: Union[EventIn, List[EventIn]], x_user_id: Optional[str] = Header(default=None)):
    events = body if isinstance(body, list) else [body]
    user_id = _user_id(x_user_id)
    rows = []
    for e in events:
        rows.append([
            e.timestamp or now_ms(),
            user_id,
            e.session_id,
            e.trace_id or e.session_id,
            e.parent_session_id,
            e.turn_id,
            e.sequence_id,
            e.event_type,
            e.tool_name,
            e.agent_name,
            e.agent_version,
            e.skill_name,
            e.skill_version,
            e.status,
            e.latency_ms,
            json.dumps(e.raw_payload, default=str) if not isinstance(e.raw_payload, str) else e.raw_payload,
        ])
    get_client().insert(
        "agent_events",
        rows,
        column_names=[
            "timestamp", "user_id", "session_id", "trace_id", "parent_session_id",
            "turn_id", "sequence_id", "event_type", "tool_name", "agent_name",
            "agent_version", "skill_name", "skill_version", "status", "latency_ms",
            "raw_payload",
        ],
    )
    return {"inserted": len(rows)}


@app.post("/ingest/usage")
def ingest_usage(body: Union[UsageIn, List[UsageIn]], x_user_id: Optional[str] = Header(default=None)):
    entries = body if isinstance(body, list) else [body]
    user_id = _user_id(x_user_id)
    rows = []
    for u in entries:
        rows.append([
            u.timestamp or now_ms(),
            user_id,
            u.session_id,
            u.trace_id or u.session_id,
            u.turn_id,
            u.model,
            u.agent_name,
            u.agent_version,
            u.skill_name,
            u.skill_version,
            u.mcp_tool_name,
            u.input_tokens,
            u.output_tokens,
            u.cache_creation_tokens,
            u.cache_read_tokens,
            u.stop_reason,
            u.service_tier,
            u.speed,
            u.cache_creation_1h_tokens,
            u.cache_creation_5m_tokens,
            u.web_search_requests,
            u.web_fetch_requests,
        ])
    get_client().insert(
        "agent_usage",
        rows,
        column_names=[
            "timestamp", "user_id", "session_id", "trace_id", "turn_id", "model",
            "agent_name", "agent_version", "skill_name", "skill_version", "mcp_tool_name",
            "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
            "stop_reason", "service_tier", "speed",
            "cache_creation_1h_tokens", "cache_creation_5m_tokens",
            "web_search_requests", "web_fetch_requests",
        ],
    )
    return {"inserted": len(rows)}


@app.post("/ingest/message")
def ingest_message(body: Union[MessageIn, List[MessageIn]], x_user_id: Optional[str] = Header(default=None)):
    messages = body if isinstance(body, list) else [body]
    user_id = _user_id(x_user_id)
    rows = []
    for m in messages:
        rows.append([
            m.timestamp or now_ms(),
            user_id,
            m.session_id,
            m.trace_id or m.session_id,
            m.turn_id,
            m.agent_name,
            m.agent_version,
            m.skill_name,
            m.skill_version,
            m.prompt_text,
            m.response_text,
        ])
    get_client().insert(
        "agent_messages",
        rows,
        column_names=[
            "timestamp", "user_id", "session_id", "trace_id", "turn_id",
            "agent_name", "agent_version", "skill_name", "skill_version",
            "prompt_text", "response_text",
        ],
    )
    return {"inserted": len(rows)}


@app.post("/registry/agent")
def register_agent(body: AgentRegistryIn):
    get_client().insert(
        "agent_registry",
        [[body.agent_name, body.version, body.description, body.source_file, now_ms()]],
        column_names=["agent_name", "version", "description", "source_file", "registered_at"],
    )
    return {"registered": body.agent_name, "version": body.version}


@app.post("/registry/skill")
def register_skill(body: SkillRegistryIn):
    get_client().insert(
        "skill_registry",
        [[body.skill_name, body.version, body.description, body.source_file, now_ms()]],
        column_names=["skill_name", "version", "description", "source_file", "registered_at"],
    )
    return {"registered": body.skill_name, "version": body.version}
