"""
Receives LiteLLM's generic_api webhook payloads: captures each raw POST body
to disk (for offline inspection), then hands the contained
StandardLoggingPayload entries to queue_client.enqueue() - a fast, DB-free
push onto Redis. webhook-worker (worker.py) is what actually parses/inserts
into ClickHouse, in batches - see AGENTS.md.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request

from .clickhouse_ingest import get_client, ingest_git_branch
from .config import CAPTURE_DIR, CAPTURE_ENABLED
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


@app.post("/api/v1/session-git-branch")
async def receive_git_branch(request: Request):
    # Reported by hooks/report_git_branch.py at SessionStart - the one
    # lifecycle hook this stack still has, since neither LiteLLM's
    # StandardLoggingPayload nor ANTHROPIC_CUSTOM_HEADERS can carry the
    # client's cwd/git state. See session_git_branch in clickhouse/schema.sql.
    body = await request.json()
    ingest_git_branch(body.get("session_id", ""), body.get("git_branch", ""), body.get("git_repo", ""))
    return {"status": "received"}
