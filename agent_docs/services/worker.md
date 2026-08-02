# `worker`

`services/worker/`, `CMD python -m src.worker`, the `webhook-worker` compose service.
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).
The only service that writes `/api/v1/metrics` traffic to ClickHouse - see `common.md`'s "Why a queue in front of ClickHouse" for why.

## Batching

- `worker.py` fixes the tiny-batch problem described in `common.md` by accumulating `build_event()`-decoded events across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer, flushing (one `client.insert()` per table for the whole batch) only once `common.config.queue.BATCH_SIZE` is reached or `FLUSH_INTERVAL_MS` has elapsed since the window opened, whichever comes first.
- `agent_name`/`agent_version` can't be resolved at `build_event()` time - it needs a `SELECT` against `agent_invocations`, which only makes sense once that batch's own invocation rows are committed.
  Left blank there, patched in by `ingest_events_batch()` batch-side, with a per-batch cache so repeated `agent_invocation_id`s trigger only one `SELECT` each.
- See `AGENTS.md`'s "Rules to not violate" for the `source_row`/`ingest_raw` full-payload-preservation rule and its memory-footprint sizing.

## Per-file breakdown

- `worker.py` (`services/worker/src/`) - standalone consumer process (`python -m src.worker`, the `webhook-worker` container).
  Accumulates entries from `common.config.queue.STREAM_KEY` across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer.
  `_decode_into()` calls `common.ingest_parsing.build_event()` on each raw payload as it's decoded, flushing once `common.config.queue.BATCH_SIZE` fills or `FLUSH_INTERVAL_MS` elapses, then calls `common.ingest_db.ingest_events_batch()` on the accumulated batch.

`services/worker/tests/` has its own pytest suite.
Run with `make test` (a separate pytest invocation per service directory - see the `Makefile`), always via `webhook-test-runner`, never inline.
