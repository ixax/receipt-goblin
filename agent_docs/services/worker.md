# `worker`

`services/worker/`, `CMD python -m src.worker`, the `webhook-worker` compose service.
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).
The only service that writes `/api/v1/metrics` and `/api/v1/usage-events` traffic to ClickHouse - see `common.md`'s "Why a queue in front of ClickHouse" for why.

## Source adapters

- Redis entries without an `adapter` field are LiteLLM `StandardLoggingPayload`s and use `litellm_standard`.
- Direct Claude entries carry `adapter=claude_transcript` and use the privacy-minimal `UsageEnvelopeV1` adapter.
- Both adapters return the same row bundle consumed by `ingest_events_batch()`.
  Direct events intentionally have no message row, request latency, or TTFT.

Each adapter resolves `client_product`, `client_surface`, and `ingest_path` before the row bundle reaches the database layer.
The database writer then resolves one exact client ID and applies it to both event and usage rows.
Grafana therefore reads attribution from `agent_usage` directly instead of repeating user-agent classification or joining events for token totals.

The direct adapter resolves model pricing through LiteLLM's live public cost map.
The resolver caches a successful map for one hour and retries a failed refresh after 30 seconds.
The resulting value is an API-equivalent estimate, not Claude Max billing.

## Batching

- `worker.py` fixes the tiny-batch problem described in `common.md` by accumulating adapter-decoded events across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer, flushing (one `client.insert()` per table for the whole batch) only once `common.config.queue.BATCH_SIZE` is reached or `FLUSH_INTERVAL_MS` has elapsed since the window opened, whichever comes first.
- `agent_name`/`agent_version` can't be resolved at `build_event()` time - it needs a `SELECT` against `agent_invocations`, which only makes sense once that batch's own invocation rows are committed.
  Left blank there, patched in by `ingest_events_batch()` batch-side, with a per-batch cache so repeated `agent_invocation_id`s trigger only one `SELECT` each.
- The worker acknowledges Redis entries only after the ClickHouse batch call returns.
  If a consumer dies first, `XAUTOCLAIM` replays the pending entry.
- Direct retries reuse the transcript `requestId` as `litellm_call_id`.
  Existing `ReplacingMergeTree` keys collapse duplicate event/usage/raw rows after ClickHouse merges them.
- LiteLLM source rows preserve the full original payload in `ingest_raw`.
  Direct source rows preserve only the normalized envelope because transcript content is excluded by contract.

## Per-file breakdown

- `worker.py` (`services/worker/src/`) - standalone consumer process (`python -m src.worker`, the `webhook-worker` container).
  Accumulates entries from `common.config.queue.STREAM_KEY` across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer.
  `_decode_into()` selects `common.ingest_adapters.build_ingest_event()` using the Redis entry's adapter tag, flushing once `common.config.queue.BATCH_SIZE` fills or `FLUSH_INTERVAL_MS` elapses, then calls `common.ingest_db.ingest_events_batch()` on the accumulated batch.

`services/worker/tests/` has its own pytest suite.
Run with `make test-services` (a separate pytest invocation per service directory - see the `Makefile`), always via `runner-test`, never inline.
