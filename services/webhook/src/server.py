"""
Receives LiteLLM's generic_api webhook payloads: captures each individual
StandardLoggingPayload event to disk under a per-session subfolder (for
offline inspection), then hands them to queue_client.enqueue() - a fast,
DB-free push onto Redis. webhook-worker (worker.py) is what actually
parses/inserts into ClickHouse, in batches - see AGENTS.md.
"""

import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator

from .clickhouse_ingest import _session_and_trace_id, get_client, ingest_git_branch, ingest_plan_proposal
from .config import CAPTURE_DIR, CAPTURE_ENABLED, LITELLM_BASE_URL, LITELLM_MASTER_KEY
from .queue_client import enqueue, get_async_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI()
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

if CAPTURE_ENABLED:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

_UNSAFE_SESSION_ID_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_session_dir_name(session_id: str) -> str:
    # session_id is client-supplied; strip to a safe charset so a crafted
    # header can't escape CAPTURE_DIR via path separators or "..".
    cleaned = _UNSAFE_SESSION_ID_CHARS.sub("_", session_id).strip("._")
    return cleaned or "unknown"


@app.get("/health")
async def health():
    try:
        get_client().command("SELECT 1")
        await get_async_redis().ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.post("/api/v1/metrics")
async def receive_metrics(request: Request):
    body = await request.json()
# body is usually a list of StandardLoggingPayload objects (log_format: json_array).
    payloads = body if isinstance(body, list) else [body]

    # One file per event (a POST can bundle several), per-session
    # subfolder, named so `ls | sort` replays creation order.
    if CAPTURE_ENABLED:
        for event in payloads:
            session_id, _ = _session_and_trace_id(event if isinstance(event, dict) else {})
            session_dir = CAPTURE_DIR / _safe_session_dir_name(session_id)
            session_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            filename = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.json"
            (session_dir / filename).write_text(json.dumps(event, indent=2, default=str))

    await enqueue(payloads)

    return {"status": "queued"}


def _virtual_key_is_valid(key: str) -> bool:
# Checks the caller's key against LiteLLM's own /key/info instead of inventing a signing scheme.
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
