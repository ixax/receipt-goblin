"""Drains the raw StandardLoggingPayloads webhook (server.py) enqueues
unmodified, calls build_event() on each one here (the CPU-bound parsing -
regex classification, message scanning, JSON work - kept off webhook's own
request path on purpose), and writes the results into ClickHouse in
batches via ingest_events_batch(), so ClickHouse sees a handful of large
inserts instead of many small ones per request.
Also drains a second, low-volume side-channel stream (session-git-branch,
plan-proposal, LiteLLM native alerts) in the same XREADGROUP call - see
common.side_ingest and queue.yml.
See AGENTS.md.

Runs as its own process (`python -m src.worker`), not through FastAPI/uvicorn.
"""
import os
import socket
import time
from datetime import datetime, timezone

import redis
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from common import fastjson as json
from common.config.queue import (
    BATCH_SIZE, CONSUMER_GROUP, FLUSH_INTERVAL_MS, SIDE_STREAM_KEY, STALE_IDLE_MS, STREAM_KEY,
)
from common.ingest_db import clickhouse_alive, get_client, ingest_events_batch
from common.ingest_parsing import build_event
from common.logging_config import create_logger
from common.side_ingest import (
    _git_branch_row, _litellm_alert_row, _plan_proposal_row,
    insert_git_branch_batch, insert_litellm_alert_batch, insert_plan_proposal_batch,
)

from .config import WORKER_METRICS_PORT
from .queue import get_redis

logger = create_logger("webhook.worker")

CONSUMER_NAME = f"{socket.gethostname()}-{os.getpid()}"

# redis_exporter only covers Redis-server stats; these cover the stream's
# own business metrics.
BATCHES_FLUSHED = Counter("worker_batches_flushed_total", "Batches flushed to ClickHouse")
EVENTS_INGESTED = Counter("worker_events_ingested_total", "Events ingested into ClickHouse")
ENTRIES_RECLAIMED = Counter("worker_entries_reclaimed_total", "Stale pending entries reclaimed via XAUTOCLAIM")
DECODE_FAILURES = Counter("worker_decode_failures_total", "Queued events that failed JSON decoding or build_event() construction")
FLUSH_LATENCY = Histogram("worker_flush_latency_seconds", "Time spent in ingest_events_batch per flush")
STREAM_DEPTH = Gauge("worker_stream_depth", "Current XLEN of the queue stream")
PENDING_COUNT = Gauge("worker_pending_count", "Current XPENDING count for the consumer group")

# Side-channel (git-branch/plan-proposal/litellm-alert) equivalents of the
# above - kept separate since they represent a distinct, much lower-volume
# event population; batch/flush-latency counters are still shared with the
# main stream (see _flush_side).
SIDE_EVENTS_INGESTED = Counter("worker_side_events_ingested_total", "Side-channel events ingested into ClickHouse")
SIDE_DECODE_FAILURES = Counter("worker_side_decode_failures_total", "Queued side events that failed JSON decoding or row construction")
SIDE_STREAM_DEPTH = Gauge("worker_side_stream_depth", "Current XLEN of the side-channel queue stream")
SIDE_PENDING_COUNT = Gauge("worker_side_pending_count", "Current XPENDING count for the side-channel consumer group")


def _ensure_group(client: redis.Redis) -> None:
    for stream_key in (STREAM_KEY, SIDE_STREAM_KEY):
        try:
            client.xgroup_create(stream_key, CONSUMER_GROUP, id="$", mkstream=True)
        except redis.exceptions.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise


def _check_dependencies(redis_client: redis.Redis) -> None:
    """Fails fast (crashes the process) if Redis or ClickHouse aren't
    reachable at startup.
    Deliberately not relying on docker-compose's own `depends_on`
    health-gating alone - a scoped `--no-deps` restart of just this service
    (see Makefile) skips that gating entirely, and this service's own
    `restart: always` is what should retry until the dependency comes up,
    not a silently-stuck consumer loop."""
    try:
        _ensure_group(redis_client)
    except Exception:
        logger.exception("redis unreachable at startup - exiting for restart: always to retry")
        raise
    try:
        clickhouse_alive()
    except Exception:
        logger.exception("clickhouse unreachable at startup - exiting for restart: always to retry")
        raise


def _decode_into(entries: list[tuple[str, dict]], message_ids: list[str], events: list[dict]) -> None:
    """Turns each queued raw StandardLoggingPayload into a build_event()
    dict - this is where the CPU-bound parsing webhook itself no longer
    does happens.
    One bad payload (malformed JSON, or an exception inside build_event())
    only drops that item, same guarantee webhook's own per-payload
    try/except used to give when it called build_event() itself."""
    for message_id, fields in entries:
        message_ids.append(message_id)
        raw = fields.get("event")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            DECODE_FAILURES.inc()
            logger.exception("failed to decode queued payload (message_id=%s)", message_id)
            continue
        try:
            events.append(build_event(payload))
        except Exception:
            DECODE_FAILURES.inc()
            logger.exception(
                "failed to build event from queued payload (message_id=%s litellm_call_id=%s)",
                message_id, payload.get("litellm_call_id", "") if isinstance(payload, dict) else "",
            )


def _decode_side_into(
    entries: list[tuple[str, dict]], message_ids: list[str],
    git_branch_rows: list[list], plan_proposal_rows: list[list], alert_rows: list[list],
) -> None:
    """Side-stream counterpart to _decode_into(): each entry additionally
    carries a `kind` field (see queue.enqueue_side) that picks which
    row-builder in common.side_ingest to call.
    One `now` timestamp per XREADGROUP batch, not per row - these are
    reporting timestamps (when the worker observed the event), not values
    the caller controls."""
    now = datetime.now(timezone.utc)
    for message_id, fields in entries:
        message_ids.append(message_id)
        kind = fields.get("kind")
        raw = fields.get("event")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            SIDE_DECODE_FAILURES.inc()
            logger.exception("failed to decode queued side event (message_id=%s kind=%s)", message_id, kind)
            continue
        try:
            if kind == "git_branch":
                git_branch_rows.append(_git_branch_row(payload, now))
            elif kind == "plan_proposal":
                plan_proposal_rows.append(_plan_proposal_row(payload, now))
            elif kind == "litellm_alert":
                alert_rows.append(_litellm_alert_row(payload, now))
            else:
                raise ValueError(f"unknown side event kind: {kind!r}")
        except Exception:
            SIDE_DECODE_FAILURES.inc()
            logger.exception("failed to build row from queued side event (message_id=%s kind=%s)", message_id, kind)


def _flush(client: redis.Redis, message_ids: list[str], events: list[dict]) -> None:
    if not message_ids:
        return
    with FLUSH_LATENCY.time():
        ingest_events_batch(events)
    client.xack(STREAM_KEY, CONSUMER_GROUP, *message_ids)
    BATCHES_FLUSHED.inc()
    EVENTS_INGESTED.inc(len(events))
    logger.info("ingested batch (n=%d)", len(events))


def _flush_side(
    client: redis.Redis, message_ids: list[str],
    git_branch_rows: list[list], plan_proposal_rows: list[list], alert_rows: list[list],
) -> None:
    if not message_ids:
        return
    ch_client = get_client()
    with FLUSH_LATENCY.time():
        # Each call below has its own row-by-row DLQ fallback (see
        # insert_rows_with_dlq_fallback) and never raises, so one poison
        # row only costs that row, not the rest of this table's batch or
        # the other two tables'.
        insert_git_branch_batch(ch_client, git_branch_rows)
        insert_plan_proposal_batch(ch_client, plan_proposal_rows)
        insert_litellm_alert_batch(ch_client, alert_rows)
    client.xack(SIDE_STREAM_KEY, CONSUMER_GROUP, *message_ids)
    BATCHES_FLUSHED.inc()
    SIDE_EVENTS_INGESTED.inc(len(git_branch_rows) + len(plan_proposal_rows) + len(alert_rows))
    logger.info("ingested side batch (n=%d)", len(message_ids))


def _claim_stale_entries(client: redis.Redis, message_ids: list[str], events: list[dict]) -> None:
    _, claimed, _ = client.xautoclaim(
        STREAM_KEY, CONSUMER_GROUP, CONSUMER_NAME,
        min_idle_time=STALE_IDLE_MS, start_id="0-0", count=BATCH_SIZE,
    )
    if claimed:
        ENTRIES_RECLAIMED.inc(len(claimed))
        logger.info("reclaimed stale pending entries (n=%d)", len(claimed))
        _decode_into(claimed, message_ids, events)


def _claim_stale_side_entries(
    client: redis.Redis, message_ids: list[str],
    git_branch_rows: list[list], plan_proposal_rows: list[list], alert_rows: list[list],
) -> None:
    _, claimed, _ = client.xautoclaim(
        SIDE_STREAM_KEY, CONSUMER_GROUP, CONSUMER_NAME,
        min_idle_time=STALE_IDLE_MS, start_id="0-0", count=BATCH_SIZE,
    )
    if claimed:
        ENTRIES_RECLAIMED.inc(len(claimed))
        logger.info("reclaimed stale pending side entries (n=%d)", len(claimed))
        _decode_side_into(claimed, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)


def _refresh_queue_gauges(client: redis.Redis) -> None:
    STREAM_DEPTH.set(client.xlen(STREAM_KEY))
    pending = client.xpending(STREAM_KEY, CONSUMER_GROUP)
    PENDING_COUNT.set(pending["pending"] if pending else 0)

    SIDE_STREAM_DEPTH.set(client.xlen(SIDE_STREAM_KEY))
    side_pending = client.xpending(SIDE_STREAM_KEY, CONSUMER_GROUP)
    SIDE_PENDING_COUNT.set(side_pending["pending"] if side_pending else 0)


def run() -> None:
    start_http_server(WORKER_METRICS_PORT)
    client = get_redis()
    _check_dependencies(client)
    logger.info("webhook-worker started (consumer=%s, stream=%s, group=%s)", CONSUMER_NAME, STREAM_KEY, CONSUMER_GROUP)

    # Buffers accumulate across multiple XREADGROUP calls per flush window,
    # so a batch fills up instead of inserting on the first event.
    message_ids: list[str] = []
    events: list[dict] = []
    side_message_ids: list[str] = []
    git_branch_rows: list[list] = []
    plan_proposal_rows: list[list] = []
    alert_rows: list[list] = []
    window_start = time.monotonic()
    last_claim_check = 0.0

    while True:
        elapsed_ms = (time.monotonic() - window_start) * 1000
        block_ms = max(int(FLUSH_INTERVAL_MS - elapsed_ms), 1)
        response = client.xreadgroup(
            CONSUMER_GROUP, CONSUMER_NAME,
            {STREAM_KEY: ">", SIDE_STREAM_KEY: ">"},
            count=max(BATCH_SIZE - len(events), 1), block=block_ms,
        )
        if response:
            for stream_name, entries in response:
                if stream_name == SIDE_STREAM_KEY:
                    _decode_side_into(entries, side_message_ids, git_branch_rows, plan_proposal_rows, alert_rows)
                else:
                    _decode_into(entries, message_ids, events)

        now = time.monotonic()
        window_elapsed_ms = (now - window_start) * 1000
        side_row_count = len(git_branch_rows) + len(plan_proposal_rows) + len(alert_rows)
        if len(events) >= BATCH_SIZE or side_row_count >= BATCH_SIZE or window_elapsed_ms >= FLUSH_INTERVAL_MS:
            # Reset the window even when empty, or window_elapsed_ms would stay
            # past FLUSH_INTERVAL_MS forever and busy-poll Redis at block_ms=1.
            # Side buffers share this same window/trigger - see queue.yml.
            _flush(client, message_ids, events)
            _flush_side(client, side_message_ids, git_branch_rows, plan_proposal_rows, alert_rows)
            message_ids, events = [], []
            side_message_ids, git_branch_rows, plan_proposal_rows, alert_rows = [], [], [], []
            window_start = now

        if not response and now - last_claim_check > STALE_IDLE_MS / 1000:
            # Only check for stranded entries when idle, at most every
            # STALE_IDLE_MS; reuses the gate to refresh queue-depth gauges too.
            _claim_stale_entries(client, message_ids, events)
            _claim_stale_side_entries(client, side_message_ids, git_branch_rows, plan_proposal_rows, alert_rows)
            _refresh_queue_gauges(client)
            last_claim_check = now


if __name__ == "__main__":
    run()
