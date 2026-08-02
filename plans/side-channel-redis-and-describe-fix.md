# Route side-channel hooks through Redis/worker + remove DESCRIBE overhead

## Context

Investigated why ClickHouse CPU was spiky (44-120%).
The `ingest` user (webhook-worker's ClickHouse identity, `services/_common/src/ingest_db.py`) generates ~650 queries/3min, split roughly in half between actual `INSERT`s and `DESCRIBE TABLE` calls.
Two independent causes, two independent fixes:

1. **DESCRIBE overhead**: every `client.insert(...)` in `ingest_db.py` omits `column_types`/`column_type_names`.
   So `clickhouse_connect` issues a fresh `DESCRIBE TABLE` before *every single insert* (confirmed in the installed package's `driver/client.py:707-714`, `create_insert_context`).
   Trivial to eliminate: pass `column_type_names` everywhere.
2. **Synchronous direct inserts**: three routes in `services/webhook/src/server.py` (`/api/v1/session-git-branch`, `/api/v1/plan-proposal`, `/api/v1/litellm-alert`) call `ingest_git_branch`/`ingest_plan_proposal`/`ingest_litellm_alert` in `services/_common/src/ingest_db.py` synchronously from the HTTP handler, opening a ClickHouse connection and inserting one row per request.
   This was an intentional low-volume shortcut (see the docstrings), but it means every one of these requests blocks on a ClickHouse round-trip and skips the batching the main `/api/v1/metrics` -> Redis -> webhook-worker path already gets.
   Moving them onto a second, separate Redis stream keeps webhook's request path DB-free (matching the main path's own rationale in `AGENTS.md` "Why a queue in front of ClickHouse") and lets webhook-worker batch-insert them too.

## Part 2 (do first, standalone, low risk): eliminate DESCRIBE

`services/_common/src/ingest_db.py`: add a `column_type_names` list next to each `_X_COLUMNS` constant (types taken from `services/clickhouse/schema.sql`, already enumerated during investigation), and pass `column_type_names=...` on every `client.insert(...)` call site:

- `_insert_agent_invocations`
- `_insert_event`
- `_insert_usage`
- `_insert_message`
- `_insert_source`
- `_insert_git_branch`
- `_insert_plan_proposal`
- `_insert_litellm_alert`
- `_insert_ai_gateway_groups`
- `_insert_ai_gateway_users`
- `_insert_clients`
- the two inline calls inside `ingest_events_batch`'s dimension writer (line ~415, `ingest_raw`)
- `_BatchWriter.insert_with_dlq_fallback` (lines ~504/512/522, generic `table`/`columns` + `ingest_dlq`) - thread a `column_type_names` lookup dict keyed by table name through this function's signature

`services/_common/tests/test_ingest_db.py`: widen `_FakeClient.insert` and `_PoisonRowClient.insert` signatures to accept `column_type_names=None` (or `**kwargs`).
Confirmed no test asserts on it, so no assertion changes needed, just signature compatibility.

Verify: run the webhook test suite (delegate to `webhook-test-runner` agent), then confirm live with `clickhouse-analyst` or a direct `system.query_log` check that `DESCRIBE TABLE` calls from user `ingest` drop to zero over a few minutes of real traffic.

## Part 1: route git-branch/plan-proposal/litellm-alert through Redis

Design: one **new, separate** Redis stream (`webhook:side-events`) for all three low-volume hook types, distinguished by a `kind` field in each XADD entry - not three separate streams.
All three are low-volume, share the same "fire-and-forget, never block/raise" semantics, and batching per-kind inside one flush cycle is simple.
Three full streams (three consumer-group registrations, three XACK targets, three sets of gauges) would be unnecessary ceremony for this volume.
This keeps them isolated from the high-volume `webhook:events` stream (so a side-event burst can't crowd out main-event batching) while reusing the existing worker loop structure.

`services/_common/src/queue.yml`: add

```yaml
side_stream_key: webhook:side-events
side_maxlen: 200   # low volume; generous headroom over expected burst size
```

`services/_common/src/config/queue.py`: export `SIDE_STREAM_KEY`, `SIDE_MAXLEN` alongside the existing constants.

`services/_common/src/queue_client.py`: add

```python
async def enqueue_side(kind: str, payload: dict) -> None:
    """kind: "git_branch" | "plan_proposal" | "litellm_alert".
    Never raises, same reasoning as enqueue()."""
    client = get_async_redis()
    try:
        await client.xadd(
            SIDE_STREAM_KEY,
            {"kind": kind, "event": json.dumps(payload, default=str)},
            maxlen=SIDE_MAXLEN, approximate=True,
        )
    except Exception:
        logger.exception("failed to enqueue side event (kind=%s)", kind)
```

`services/webhook/src/server.py`: replace the three direct `ingest_*` calls with `await enqueue_side("git_branch", {...})` / `"plan_proposal"` / `"litellm_alert"` (raw JSON body for alerts, same as today), dropping the `ingest_git_branch`/`ingest_plan_proposal`/`ingest_litellm_alert` imports from `common.ingest_db`.
Response bodies stay `{"status": "received"}`.

`services/_common/src/ingest_db.py`: split each of `ingest_git_branch`/`ingest_plan_proposal`/`ingest_litellm_alert` into a pure row-builder (`_git_branch_row(payload, now)`, `_plan_proposal_row(...)`, `_litellm_alert_row(...)`) and a new batch inserter (`insert_git_branch_batch(client, rows)`, `insert_plan_proposal_batch(...)`, `insert_litellm_alert_batch(...)`) that does one multi-row `client.insert(...)` per table (with `column_type_names` from Part 2).
Keep the existing `ingest_git_branch`/`ingest_plan_proposal`/`ingest_litellm_alert` functions only if something else still calls them directly (check first) - otherwise delete in favor of the batch path.

`services/worker/src/worker.py`: extend the consumer loop to read both streams in one `XREADGROUP` call:

```python
response = client.xreadgroup(
    CONSUMER_GROUP, CONSUMER_NAME,
    {STREAM_KEY: ">", SIDE_STREAM_KEY: ">"},
    count=..., block=block_ms,
)
```

Dispatch per `_stream_name` in the response.
The main stream keeps today's `_decode_into` -> `events` path.
The side stream gets a new `_decode_side_into` that reads the `kind` field and appends the parsed payload into one of three row lists (`git_branch_rows`, `plan_proposal_rows`, `alert_rows`), building each row via the new pure row-builders.
Track side-stream message ids separately from main-stream message ids - they must be XACK'd against different stream keys.
`_flush` grows to also call the three new batch inserters and `client.xack(SIDE_STREAM_KEY, CONSUMER_GROUP, *side_message_ids)` when the side buffers are non-empty.
`_ensure_group` calls `xgroup_create` for `SIDE_STREAM_KEY` too.
`_refresh_queue_gauges` reports side-stream depth/pending as well (reuse existing gauge objects with a label, or add `_side` suffixed gauges - match whatever's less invasive).

Flush timing: reuse the same window/`FLUSH_INTERVAL_MS`/`BATCH_SIZE` - no separate side-specific batch size needed given the volume.
Time-based flush (2s) will dominate for these streams in practice, which is an acceptable latency for session metadata and alerts (compare against the "never blocks the CLI" guarantee these hooks already have today, since the HTTP response now returns before ClickHouse is even touched).

Verify:

1. `webhook-test-runner` for `services/webhook` and `services/_common` suites after the split (row-builder + batch-insert unit tests may need small additions/adjustments - check existing coverage for `ingest_git_branch`/`ingest_plan_proposal`/`ingest_litellm_alert` in `test_ingest_db.py` and update to test the new pure builders + batch inserters instead).
2. `dev-ops` rebuild/restart webhook + worker, then manually POST to `/api/v1/session-git-branch` and `/api/v1/litellm-alert` (or trigger via existing hooks) and confirm rows land in `session_git_branch`/`litellm_alerts` within ~2s, and that `webhook:side-events` XLEN returns to 0 (no stuck backlog).
3. Re-check `system.query_log`/`docker stats` for the `ingest` user's query rate/CPU after both parts land, to confirm the original symptom (CPU spikes) actually improved.
