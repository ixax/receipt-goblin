"""
Receives LiteLLM's generic_api webhook payloads: captures each raw POST body
to disk (for offline inspection) and parses/inserts the contained
StandardLoggingPayload entries into ClickHouse - see clickhouse_ingest.py.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request

from .clickhouse_ingest import get_client, ingest_webhook_body

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI()

CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", "/app/captures"))
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health():
    try:
        get_client().command("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


@app.post("/api/v1/metrics")
async def receive_metrics(request: Request):
    body = await request.json()
    now = datetime.now(timezone.utc)

    # One file per POST, raw as received - log_format: json_array in
    # litellm/config.yaml means `body` is usually a list of
    # StandardLoggingPayload objects, not a single one.
    filename = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.json"
    (CAPTURE_DIR / filename).write_text(json.dumps(body, indent=2, default=str))

    ingest_webhook_body(body)

    return {"status": "received"}
