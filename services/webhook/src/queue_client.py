"""Redis Streams queue between webhook (producer) and webhook-worker
(consumer) - see AGENTS.md. webhook turns each payload into a build_event()
dict (pure CPU, no ClickHouse round-trip) and XADDs it; webhook-worker
drains the stream in batches and does the actual inserts.
"""
import json
import logging

import redis
import redis.asyncio as aioredis

from .clickhouse_ingest import build_event
from .config import MAXLEN, REDIS_HOST, REDIS_PORT, STREAM_KEY

logger = logging.getLogger("webhook.queue_client")

_async_client = None
_sync_client = None


def get_async_redis() -> aioredis.Redis:
    """Used by webhook (server.py) - the request path is async FastAPI."""
    global _async_client
    if _async_client is None:
        _async_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT)
    return _async_client


def get_redis() -> redis.Redis:
    """Used by webhook-worker - a plain blocking consumer loop, no event
    loop to share. decode_responses=True since it only reads back JSON text."""
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    return _sync_client


async def enqueue(payloads: list) -> None:
    """payloads: StandardLoggingPayload dicts from one webhook POST body.
    Never raises - LiteLLM would retry the whole body forever if a
    malformed payload or unavailable Redis broke the ack.
    """
    client = get_async_redis()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        try:
            event = build_event(payload)
            await client.xadd(
                STREAM_KEY,
                {"event": json.dumps(event, default=str)},
                maxlen=MAXLEN,
                approximate=True,
            )
        except Exception:
            logger.exception(
                "failed to enqueue payload (litellm_call_id=%s)",
                payload.get("litellm_call_id", ""),
            )
