"""
Receives LiteLLM's generic_api webhook payloads and hands them to
queue_client.enqueue()/enqueue_raw() - a fast, DB-free push onto Redis.
webhook-worker (worker.py) is what actually parses/inserts into ClickHouse,
in batches - see AGENTS.md.
"""

from fastapi import FastAPI, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator

from common.litellm_auth import virtual_key_is_valid
from common.logging_config import create_logger

from .clickhouse_ingest import (
    clickhouse_alive,
    ingest_git_branch,
    ingest_litellm_alert,
    ingest_plan_proposal,
)
from .config import LITELLM_BASE_URL, LITELLM_MASTER_KEY
from .queue_client import enqueue_raw, get_async_redis

logger = create_logger("webhook.server")

app = FastAPI()
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.get("/health")
async def health():
    try:
        clickhouse_alive()
        await get_async_redis().ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.post("/api/v1/metrics")
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


def _virtual_key_is_valid(key: str) -> bool:
    return virtual_key_is_valid(key, LITELLM_BASE_URL, LITELLM_MASTER_KEY)


@app.post("/api/v1/session-git-branch")
async def receive_git_branch(request: Request):
    # Reported by hooks/report_git_branch.py (SessionStart/CwdChanged) since
    # neither StandardLoggingPayload nor ANTHROPIC_CUSTOM_HEADERS carry cwd/git state.
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if not _virtual_key_is_valid(token):
        raise HTTPException(status_code=401, detail="invalid or missing virtual key")

    body = await request.json()
    ingest_git_branch(body.get("session_id", ""), body.get("git_branch", ""), body.get("git_repo", ""))
    return {"status": "received"}


@app.post("/api/v1/plan-proposal")
async def receive_plan_proposal(request: Request):
    # Reported by hooks/report_plan_proposal.py (PreToolUse: ExitPlanMode) -
    # StandardLoggingPayload's arguments come back empty for ExitPlanMode.
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if not _virtual_key_is_valid(token):
        raise HTTPException(status_code=401, detail="invalid or missing virtual key")

    body = await request.json()
    ingest_plan_proposal(body.get("session_id", ""), body.get("plan_text", ""))
    return {"status": "received"}


@app.post("/api/v1/litellm-alert")
async def receive_litellm_alert(request: Request):
    # LiteLLM's native alerting webhook (general_settings.alerting: ["webhook"]
    # in services/litellm/config.yaml, WEBHOOK_URL env var) - same trust
    # model as /api/v1/metrics (internal-network-only; LiteLLM's generic
    # alerting webhook has no header-auth mechanism to check against).
    # Direct-to-ClickHouse insert, not queued through Redis - see
    # ingest_litellm_alert's own docstring for why (low event volume).
    body = await request.json()
    ingest_litellm_alert(body)
    return {"status": "received"}
