"""
Mock metrics receiver for inspecting LiteLLM's generic_api webhook payload
before wiring a real route into ingest-api.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request

app = FastAPI()

CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", "/app/captures"))
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/metrics")
async def receive_metrics(request: Request):
    body = await request.json()
    now = datetime.now(timezone.utc)

    print(f"\n=== webhook hit @ {now.isoformat()} ===")
    print(json.dumps(body, indent=2, default=str))

    # One file per POST, raw as received - log_format: json_array in
    # litellm/config.yaml means `body` is usually a list of
    # StandardLoggingPayload objects, not a single one.
    filename = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.json"
    (CAPTURE_DIR / filename).write_text(json.dumps(body, indent=2, default=str))

    return {"status": "received"}
