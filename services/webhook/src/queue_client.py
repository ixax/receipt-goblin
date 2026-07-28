"""Redis Streams queue between webhook (producer) and webhook-worker
(consumer) - see AGENTS.md. webhook XADDs each raw StandardLoggingPayload
completely unmodified - no parsing, no build_event() - so its own request
path stays cheap (I/O only); webhook-worker calls build_event() itself when
it drains the stream, then does the actual ClickHouse inserts in batches.
"""
import logging

import redis
import redis.asyncio as aioredis

from . import fastjson as json
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
    Pushed onto Redis exactly as received - no build_event() here, see
    module docstring. Never raises - LiteLLM would retry the whole body
    forever if a malformed payload or unavailable Redis broke the ack.

    Used directly when the caller already has parsed payloads (enqueue_raw()'s
    own bundled-array fallback below) - everyone else should call
    enqueue_raw() instead, to skip the parse this function requires its
    caller to have already done.
    """
    client = get_async_redis()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        try:
            await client.xadd(
                STREAM_KEY,
                {"event": json.dumps(payload, default=str)},
                maxlen=MAXLEN,
                approximate=True,
            )
        except Exception:
            logger.exception(
                "failed to enqueue payload (litellm_call_id=%s)",
                payload.get("litellm_call_id", ""),
            )


async def enqueue_raw(body: bytes) -> None:
    """Fast path for webhook's request handler: `body` is the exact raw
    POST bytes, unparsed. The common case - one StandardLoggingPayload
    object, not a `log_format: json_array` bundle - goes straight onto
    Redis unmodified, no json.loads/json.dumps round-trip at all. Only a
    bundled array (starts with `[`) needs an actual parse, to split it into
    one Redis entry per payload (falls back to enqueue() for that case).

    This split exists because json parse+re-serialize of a ~360KB-1.5MB
    payload is itself real CPU cost - load testing showed it alone
    saturating a single core under concurrency, even with build_event()
    no longer running on webhook's request path (see AGENTS.md "Why a
    queue in front of ClickHouse"). Never raises, same reasoning as
    enqueue().
    """
    if not body.lstrip().startswith(b"["):
        client = get_async_redis()
        try:
            await client.xadd(STREAM_KEY, {"event": body}, maxlen=MAXLEN, approximate=True)
        except Exception:
            logger.exception("failed to enqueue raw payload")
        return

    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        logger.exception("failed to decode bundled payload array")
        return
    await enqueue(parsed if isinstance(parsed, list) else [parsed])
