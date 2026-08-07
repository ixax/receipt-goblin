"""
Receives LiteLLM's generic_api webhook payloads and hands them to
queue.enqueue()/enqueue_raw() - a fast, DB-free push onto Redis.
webhook-worker (worker.py) is what actually parses/inserts into ClickHouse,
in batches - see AGENTS.md.
"""

import asyncio

from common.config.litellm import LITELLM_BASE_URL, LITELLM_MASTER_KEY
from common.ingest_db import clickhouse_alive
from common.litellm_auth import team_alias, virtual_key_info, virtual_key_is_valid
from common.logging_config import create_logger
from common.usage_envelope import UsageEnvelopeError, normalize_usage_envelope
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from .config import APP_VERSION
from .queue import enqueue_raw, enqueue_side, enqueue_usage_events, get_async_redis

logger = create_logger("webhook.server")

app = FastAPI(
    title="receipt-goblin webhook",
    description="Ingest entry point for LiteLLM's generic_api webhook "
    "payloads and session-metadata hooks. Enqueues onto Redis; "
    "webhook-worker does the actual ClickHouse writes.",
    version=APP_VERSION,
)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

bearer_scheme = HTTPBearer(auto_error=False)
MAX_USAGE_EVENTS_PER_REQUEST = 100


class HealthResponse(BaseModel):
    status: str
    detail: str | None = None


class AckResponse(BaseModel):
    status: str


class GitBranchPayload(BaseModel):
    session_id: str
    git_branch: str
    git_repo: str = ""


class PlanProposalPayload(BaseModel):
    session_id: str
    plan_text: str


# /api/v1/metrics and /api/v1/litellm-alert deliberately stay on raw
# Request/bytes/dict (see their route comments below) instead of a Pydantic
# request model - these dicts document the expected shape for /docs only,
# via openapi_extra, without binding FastAPI to actually parse the body.
_METRICS_EXAMPLE = {
    "event_name": "spend_logs",
    "data": {"request_id": "example-id", "model": "gpt-4", "spend": 0.05},
}
_LITELLM_ALERT_EXAMPLE = {
    "type": "budget_exceeded",
    "budget_threshold": 100,
    "current_spend": 105,
}
_USAGE_EVENT_EXAMPLE = {
    "schema_version": 1,
    "source": "claude_desktop",
    "event_id": "req_example",
    "session_id": "session-example",
    "timestamp": "2026-08-06T14:06:52Z",
    "model": "claude-opus-5",
    "client_version": "2.1.221",
    "entrypoint": "claude-desktop",
    "stop_reason": "end_turn",
    "tool_name": "",
    "agent_id": "",
    "usage": {
        "input_tokens": 2,
        "output_tokens": 24,
        "cache_creation_tokens": 39921,
        "cache_read_tokens": 33693,
        "cache_creation_1h_tokens": 39921,
        "cache_creation_5m_tokens": 0,
    },
}


def _example_request_body(example: dict) -> dict:
    return {
        "content": {
            "application/json": {
                "schema": {"type": "object"},
                "example": example,
            }
        }
    }


def _usage_event_request_body() -> dict:
    return {
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"type": "object"},
                        {
                            "type": "array",
                            "items": {"type": "object"},
                            "minItems": 1,
                            "maxItems": MAX_USAGE_EVENTS_PER_REQUEST,
                        },
                    ]
                },
                "examples": {
                    "single": {"value": _USAGE_EVENT_EXAMPLE},
                    "batch": {"value": [_USAGE_EVENT_EXAMPLE]},
                },
            }
        }
    }


def _virtual_key_is_valid(key: str) -> bool:
    return virtual_key_is_valid(key, LITELLM_BASE_URL, LITELLM_MASTER_KEY)


def _virtual_key_info(key: str) -> dict | None:
    return virtual_key_info(key, LITELLM_BASE_URL, LITELLM_MASTER_KEY)


def _team_alias(team_id: str) -> str:
    return team_alias(team_id, LITELLM_BASE_URL, LITELLM_MASTER_KEY)


def require_virtual_key(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> None:
    # auto_error=False above so a missing header 401s the same as an
    # invalid one, matching the old manual-parsing behavior instead of
    # HTTPBearer's default 403 "Not authenticated".
    if credentials is None or not _virtual_key_is_valid(credentials.credentials):
        raise HTTPException(status_code=401, detail="invalid or missing virtual key")


def require_virtual_key_info(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict:
    info = _virtual_key_info(credentials.credentials) if credentials is not None else None
    if info is None:
        raise HTTPException(status_code=401, detail="invalid or missing virtual key")
    return info


@app.get("/health", tags=["health"], summary="Liveness/readiness check", response_model=HealthResponse)
async def health():
    try:
        clickhouse_alive()
        await get_async_redis().ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.post(
    "/api/v1/metrics",
    tags=["ingest"],
    summary="Receive a LiteLLM generic_api webhook payload",
    response_model=AckResponse,
    openapi_extra={"requestBody": _example_request_body(_METRICS_EXAMPLE)},
)
async def receive_metrics(request: Request):
    # Raw bytes, not `await request.json()` - parsing (and later
    # re-serializing per payload for XADD) is real CPU cost on a ~360KB-1.5MB
    # body, and under concurrent load testing that alone saturated a single
    # core even with build_event() no longer running here (see AGENTS.md
    # "Why a queue in front of ClickHouse"). enqueue_raw() skips that
    # entirely for the common case (a lone payload, not a bundled array).
    body = await request.body()
    await enqueue_raw(body)
    return {"status": "queued"}


@app.post(
    "/api/v1/usage-events",
    tags=["ingest"],
    summary="Receive a source-neutral usage event",
    response_model=AckResponse,
    responses={
        401: {"description": "invalid or missing virtual key"},
        422: {"description": "invalid usage envelope"},
    },
    openapi_extra={"requestBody": _usage_event_request_body()},
)
async def receive_usage_event(request: Request, key_info: dict = Depends(require_virtual_key_info)):
    try:
        body = await request.json()
        payloads = body if isinstance(body, list) else [body]
        if not payloads or len(payloads) > MAX_USAGE_EVENTS_PER_REQUEST:
            raise UsageEnvelopeError(
                f"request must contain between 1 and {MAX_USAGE_EVENTS_PER_REQUEST} usage events"
            )
        group_id = key_info.get("team_id") or ""
        identity = {
            "user_id": key_info.get("user_id") or key_info.get("key_alias") or "unknown-user",
            "user_name": key_info.get("key_alias") or "",
            "group_id": group_id,
            # /key/info returns team_id but no alias, so the Team's display
            # name needs its own (cached) lookup - without it this path can
            # only ever label a Team by its uuid.
            # to_thread: team_alias blocks on urllib up to 3s on a cache miss,
            # and this is an async handler - inline it would stall the whole
            # event loop, /health included.
            "group_name": await asyncio.to_thread(_team_alias, group_id),
        }
        envelopes = []
        for index, payload in enumerate(payloads):
            try:
                envelopes.append(normalize_usage_envelope(payload, identity=identity))
            except UsageEnvelopeError as exc:
                raise UsageEnvelopeError(f"events[{index}]: {exc}") from exc
    except (UsageEnvelopeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        await enqueue_usage_events(envelopes)
    except Exception as exc:
        logger.exception("failed to enqueue direct usage batch (event_count=%d)", len(envelopes))
        raise HTTPException(status_code=503, detail="usage queue unavailable") from exc
    return {"status": "queued"}


@app.post(
    "/api/v1/session-git-branch",
    tags=["session-metadata"],
    summary="Report a Claude Code session's git branch",
    response_model=AckResponse,
    responses={401: {"description": "invalid or missing virtual key"}},
)
async def receive_git_branch(payload: GitBranchPayload, _: None = Depends(require_virtual_key)):
    # Reported by hooks/report_git_branch.py (SessionStart/CwdChanged) since
    # neither StandardLoggingPayload nor ANTHROPIC_CUSTOM_HEADERS carry cwd/git state.
    # Queued (not a direct ClickHouse insert) - see queue.enqueue_side.
    await enqueue_side("git_branch", payload.model_dump())
    return {"status": "received"}


@app.post(
    "/api/v1/plan-proposal",
    tags=["session-metadata"],
    summary="Report an ExitPlanMode proposal",
    response_model=AckResponse,
    responses={401: {"description": "invalid or missing virtual key"}},
)
async def receive_plan_proposal(payload: PlanProposalPayload, _: None = Depends(require_virtual_key)):
    # Reported by hooks/report_plan_proposal.py (PreToolUse: ExitPlanMode) -
    # StandardLoggingPayload's arguments come back empty for ExitPlanMode.
    # Queued (not a direct ClickHouse insert) - see queue.enqueue_side.
    await enqueue_side("plan_proposal", payload.model_dump())
    return {"status": "received"}


@app.post(
    "/api/v1/litellm-alert",
    tags=["alerting"],
    summary="Receive a LiteLLM native alerting webhook event",
    response_model=AckResponse,
    openapi_extra={"requestBody": _example_request_body(_LITELLM_ALERT_EXAMPLE)},
)
async def receive_litellm_alert(request: Request):
    # LiteLLM's native alerting webhook (general_settings.alerting: ["webhook"]
    # in services/litellm/config.yaml, WEBHOOK_URL env var) - same trust
    # model as /api/v1/metrics (internal-network-only; LiteLLM's generic
    # alerting webhook has no header-auth mechanism to check against).
    # Queued (not a direct ClickHouse insert) - see queue.enqueue_side.
    # Not a Pydantic model - alert shape varies by event type and
    # _litellm_alert_row keeps the full raw_payload regardless.
    body = await request.json()
    await enqueue_side("litellm_alert", body)
    return {"status": "received"}
