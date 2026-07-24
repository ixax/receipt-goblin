"""Standalone consumer for queue_client.STREAM_KEY - the "other worker" that
drains what webhook (server.py) enqueues and writes it into ClickHouse in
batches via clickhouse_ingest.ingest_events_batch(), so ClickHouse sees a
handful of large inserts instead of many small ones per request. See
AGENTS.md.

Run as its own process/container (`python -m src.worker`), not through
FastAPI/uvicorn - webhook only ever produces onto the stream, never
consumes it.
"""
import json
import logging
import os
import socket
import time

import redis
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from .clickhouse_ingest import ingest_events_batch
from .config import BATCH_SIZE, FLUSH_INTERVAL_MS, CONSUMER_GROUP, STALE_IDLE_MS, STREAM_KEY, WORKER_METRICS_PORT
from .queue_client import get_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("webhook.worker")

CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"

# redis_exporter only sees Redis-server-level stats (memory, clients,
# commands) - these cover the stream's own business metrics instead.
BATCHES_FLUSHED = Counter("worker_batches_flushed_total", "Batches flushed to ClickHouse")
EVENTS_INGESTED = Counter("worker_events_ingested_total", "Events ingested into ClickHouse")
ENTRIES_RECLAIMED = Counter("worker_entries_reclaimed_total", "Stale pending entries reclaimed via XAUTOCLAIM")
DECODE_FAILURES = Counter("worker_decode_failures_total", "Queued events that failed JSON decoding")
FLUSH_LATENCY = Histogram("worker_flush_latency_seconds", "Time spent in ingest_events_batch per flush")
STREAM_DEPTH = Gauge("worker_stream_depth", "Current XLEN of the queue stream")
PENDING_COUNT = Gauge("worker_pending_count", "Current XPENDING count for the consumer group")


def _ensure_group(client: redis.Redis) -> None:
    try:
        client.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="$", mkstream=True)
    except redis.exceptions.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _decode_into(entries: list[tuple[str, dict]], message_ids: list[str], events: list[dict]) -> None:
    for message_id, fields in entries:
        message_ids.append(message_id)
        raw = fields.get("event")
        if not raw:
            continue
        try:
            events.append(json.loads(raw))
        except (TypeError, ValueError):
            DECODE_FAILURES.inc()
            logger.exception("failed to decode queued event (message_id=%s)", message_id)


def _flush(client: redis.Redis, message_ids: list[str], events: list[dict]) -> None:
    if not message_ids:
        return
    with FLUSH_LATENCY.time():
        ingest_events_batch(events)
    client.xack(STREAM_KEY, CONSUMER_GROUP, *message_ids)
    BATCHES_FLUSHED.inc()
    EVENTS_INGESTED.inc(len(events))
    logger.info("ingested batch (n=%d)", len(events))


def _claim_stale_entries(client: redis.Redis, message_ids: list[str], events: list[dict]) -> None:
    _, claimed, _ = client.xautoclaim(
        STREAM_KEY, CONSUMER_GROUP, CONSUMER_NAME,
        min_idle_time=STALE_IDLE_MS, start_id="0-0", count=BATCH_SIZE,
    )
    if claimed:
        ENTRIES_RECLAIMED.inc(len(claimed))
        logger.info("reclaimed stale pending entries (n=%d)", len(claimed))
        _decode_into(claimed, message_ids, events)


def _refresh_queue_gauges(client: redis.Redis) -> None:
    STREAM_DEPTH.set(client.xlen(STREAM_KEY))
    pending = client.xpending(STREAM_KEY, CONSUMER_GROUP)
    PENDING_COUNT.set(pending["pending"] if pending else 0)


def run() -> None:
    start_http_server(WORKER_METRICS_PORT)
    client = get_redis()
    _ensure_group(client)
    logger.info("webhook-worker started (consumer=%s, stream=%s, group=%s)", CONSUMER_NAME, STREAM_KEY, CONSUMER_GROUP)

    # Buffers accumulate across multiple XREADGROUP calls within one flush
    # window, so a batch actually fills up instead of being inserted as
    # soon as the first event of the window arrives (see FLUSH_INTERVAL_MS).
    message_ids: list[str] = []
    events: list[dict] = []
    window_start = time.monotonic()
    last_claim_check = 0.0

    while True:
        elapsed_ms = (time.monotonic() - window_start) * 1000
        block_ms = max(int(FLUSH_INTERVAL_MS - elapsed_ms), 1)
        response = client.xreadgroup(
            CONSUMER_GROUP, CONSUMER_NAME,
            {STREAM_KEY: ">"}, count=max(BATCH_SIZE - len(events), 1), block=block_ms,
        )
        if response:
            for _stream_name, entries in response:
                _decode_into(entries, message_ids, events)

        now = time.monotonic()
        window_elapsed_ms = (now - window_start) * 1000
        if len(events) >= BATCH_SIZE or window_elapsed_ms >= FLUSH_INTERVAL_MS:
            # Reset the window even with nothing buffered - otherwise, once
            # idle, window_elapsed_ms would stay past FLUSH_INTERVAL_MS
            # forever and block_ms would clamp to 1ms, busy-polling Redis.
            _flush(client, message_ids, events)
            message_ids, events = [], []
            window_start = now

        if not response and now - last_claim_check > STALE_IDLE_MS / 1000:
            # Only worth checking for stranded pending entries when the
            # stream was otherwise idle, and no more than once every
            # STALE_IDLE_MS - no need to hammer XAUTOCLAIM on every empty poll.
            # Reuses the same gate to refresh the queue-depth gauges, rather
            # than hitting Redis with XLEN/XPENDING on every loop iteration.
            _claim_stale_entries(client, message_ids, events)
            _refresh_queue_gauges(client)
            last_claim_check = now


if __name__ == "__main__":
    run()
