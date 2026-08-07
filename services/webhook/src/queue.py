"""Redis Streams producer side - webhook XADDs source-tagged payloads onto
the stream worker.py drains. LiteLLM payloads stay raw; compact direct-usage
envelopes are validated before they reach this module. The worker selects the
adapter and writes ClickHouse batches.
Split out of common/queue_client.py - see
plans/common-module-cleanup-refactor.md.
"""
import logging

import redis.asyncio as aioredis
from common import fastjson as json
from common.config.queue import MAXLEN, SIDE_MAXLEN, SIDE_STREAM_KEY, STREAM_KEY
from common.config.redis import REDIS_HOST, REDIS_PORT

logger = logging.getLogger("webhook.queue")

_async_client = None


def get_async_redis() -> aioredis.Redis:
    """Used by webhook (server.py) - the request path is async FastAPI."""
    global _async_client
    if _async_client is None:
        _async_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT)
    return _async_client


async def enqueue(payloads: list) -> None:
    """payloads: StandardLoggingPayload dicts from one webhook POST body.
    Pushed onto Redis exactly as received - no build_event() here, see
    module docstring.
    Never raises - LiteLLM would retry the whole body forever if a
    malformed payload or unavailable Redis broke the ack.

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
    POST bytes, unparsed.
    The common case - one StandardLoggingPayload object, not a
    `log_format: json_array` bundle - goes straight onto Redis unmodified,
    no json.loads/json.dumps round-trip at all.
    Only a bundled array (starts with `[`) needs an actual parse, to split
    it into one Redis entry per payload (falls back to enqueue() for that
    case).

    This split exists because json parse+re-serialize of a ~360KB-1.5MB
    payload is itself real CPU cost - load testing showed it alone
    saturating a single core under concurrency, even with build_event()
    no longer running on webhook's request path (see AGENTS.md "Why a
    queue in front of ClickHouse").
    Never raises, same reasoning as enqueue().
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


async def enqueue_usage_event(payload: dict) -> None:
    """Compatibility wrapper for callers that have one usage event."""
    await enqueue_usage_events([payload])


async def enqueue_usage_events(payloads: list[dict]) -> None:
    """Pipelines one validated UsageEnvelopeV1 batch and propagates failure.

    Unlike LiteLLM's callback endpoint, the direct collector has a durable
    SQLite outbox. A Redis failure must reach it as a non-2xx response so it
    can retain and retry the batch.

    Delivery is at-least-once by design: a mid-pipeline failure can leave a
    prefix of the batch on the stream, and the collector's retry re-XADDs it.
    ClickHouse's ReplacingMergeTree keys on the envelope's event_id, so the
    replays collapse on merge - same semantics as every other ingest path.
    """
    if not payloads:
        return
    client = get_async_redis()
    pipeline = client.pipeline(transaction=False)
    for payload in payloads:
        pipeline.xadd(
            STREAM_KEY,
            {
                "adapter": "claude_transcript",
                "event": json.dumps(payload, default=str),
            },
            maxlen=MAXLEN,
            approximate=True,
        )
    await pipeline.execute()


async def enqueue_side(kind: str, payload: dict) -> None:
    """kind: "git_branch" | "plan_proposal" | "litellm_alert".
    Fire-and-forget onto a second, separate stream from enqueue()/enqueue_raw()
    above, so a burst of these low-volume session-metadata/alert events can't
    crowd out webhook:events' own batching - see queue.yml.
    Never raises, same reasoning as enqueue()."""
    client = get_async_redis()
    try:
        await client.xadd(
            SIDE_STREAM_KEY,
            {"kind": kind, "event": json.dumps(payload, default=str)},
            maxlen=SIDE_MAXLEN,
            approximate=True,
        )
    except Exception:
        logger.exception("failed to enqueue side event (kind=%s)", kind)
