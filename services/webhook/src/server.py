"""
Receives LiteLLM's generic_api webhook payloads: captures each raw POST body
to disk (for offline inspection), then hands the contained
StandardLoggingPayload entries to queue_client.enqueue() - a fast, DB-free
push onto Redis. webhook-worker (worker.py) is what actually parses/inserts
into ClickHouse, in batches - see AGENTS.md.
"""

import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request

from .clickhouse_ingest import get_client, ingest_git_branch, ingest_plan_proposal
from .config import CAPTURE_DIR, CAPTURE_ENABLED, LITELLM_BASE_URL, LITELLM_MASTER_KEY
from .queue_client import enqueue, get_async_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI()

if CAPTURE_ENABLED:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


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

    # One file per POST, raw as received - log_format: json_array in
    # litellm/config.yaml means `body` is usually a list of
    # StandardLoggingPayload objects, not a single one. Off by default, see
    # config.CAPTURE_ENABLED.
    if CAPTURE_ENABLED:
        now = datetime.now(timezone.utc)
        filename = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.json"
        (CAPTURE_DIR / filename).write_text(json.dumps(body, indent=2, default=str))

    payloads = body if isinstance(body, list) else [body]
    await enqueue(payloads)

    return {"status": "queued"}


def _virtual_key_is_valid(key: str) -> bool:
    # Checks the caller's personal LiteLLM virtual key against LiteLLM's own
    # /key/info - reuses the trust root the proxy already uses for every LLM
    # call, instead of inventing a separate signing scheme.
    if not key:
        return False
    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/key/info?key={key}",
        # services/litellm/config.yaml sets general_settings.litellm_key_header_name
        # to x-litellm-api-key (see AGENTS.md), which applies to every proxy
        # route including admin ones - plain `Authorization: Bearer` here gets
        # rejected as "Malformed API Key passed in" since LiteLLM no longer
        # looks at that header for key auth.
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
    # Reported by hooks/report_git_branch.py at SessionStart and (Claude Code
    # only) CwdChanged - the one lifecycle hook this stack still has, since
    # neither LiteLLM's StandardLoggingPayload nor ANTHROPIC_CUSTOM_HEADERS
    # can carry the client's cwd/git state. See session_git_branch in
    # clickhouse/schema.sql.
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if not _virtual_key_is_valid(token):
        raise HTTPException(status_code=401, detail="invalid or missing virtual key")

    body = await request.json()
    ingest_git_branch(body.get("session_id", ""), body.get("git_branch", ""), body.get("git_repo", ""))
    return {"status": "received"}


@app.post("/api/v1/plan-proposal")
async def receive_plan_proposal(request: Request):
    # Reported by hooks/report_plan_proposal.py at PreToolUse (matcher:
    # ExitPlanMode) - LiteLLM's StandardLoggingPayload doesn't carry the
    # plan text (its tool_calls[0].function.arguments comes back empty for
    # ExitPlanMode), so this hook reads it straight from the tool_input
    # Claude Code passes it. See plan_proposals in clickhouse/schema.sql.
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if not _virtual_key_is_valid(token):
        raise HTTPException(status_code=401, detail="invalid or missing virtual key")

    body = await request.json()
    ingest_plan_proposal(body.get("session_id", ""), body.get("plan_text", ""))
    return {"status": "received"}
