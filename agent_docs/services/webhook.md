# `webhook` / `webhook-worker`

One image (`services/webhook/Dockerfile`), five roles picked at runtime by `APP_ROLE` (`server`/`worker`/`reparse`/`migrate`/`loadtest`, default `server`) via `services/webhook/docker-entrypoint.sh`.
Covers the `webhook`(x2)/`webhook-worker`/`metrics-reparse`/`clickhouse-migrate` compose services - `loadtest` is `make loadtest`'s own replay role, not a standing compose service.

## `APP_ROLE` dispatch

`docker-entrypoint.sh` is baked in as `ENTRYPOINT`; the role choice lives in `docker-compose.yml`'s per-service `environment: APP_ROLE: ...`, not a compose-level `command:` override - one image/Dockerfile for five behaviors instead of five near-duplicate images.
`if [ "$#" -gt 0 ]; then exec "$@"; fi` is a passthrough so `docker-compose.dev.yml`'s `command:` override (adding uvicorn's `--reload` for `webhook`) still works without touching this script.

## Why a queue in front of ClickHouse

- ClickHouse handles a few large batched inserts far better than many small per-request ones (merge/part amplification, connection overhead per insert).
  The old design did up to 4 separate `client.insert()` calls per `StandardLoggingPayload`, synchronously, inside `webhook`'s HTTP handler - fine at low volume, but under many concurrent Claude Code/Codex sessions routed through LiteLLM, that's many small inserts/sec hitting ClickHouse directly, with backpressure onto `webhook` (and LiteLLM retries) if it falls behind.
- `webhook` now only enqueues onto `redis`. `webhook-worker` is the only thing that talks to ClickHouse for `/api/v1/metrics` traffic, and the only thing calling `clickhouse_ingest.build_event()` (per payload, on decode - pure, no I/O), batched via `clickhouse_ingest.ingest_events_batch()`.
  Moving `build_event()`'s CPU-bound parsing (regex classification, message scanning, JSON work) off `webhook`'s request path was itself a fix, not just batching: load testing (`make loadtest`) showed p99 latency climbing under sustained concurrency when it ran inline in `webhook`'s single-threaded event loop, serializing behind whichever request's parsing got there first.
- Removing `build_event()` alone wasn't enough: `webhook-1`/`webhook-2` (each `cpus: 1.0`) still pegged at 65-96% CPU under the same load.
  The culprit was `request.json()`/`json.dumps()` themselves - parsing and re-serializing a ~360KB-1.5MB `StandardLoggingPayload` twice per request is real CPU work.
  Fix: `queue_client.enqueue_raw(body: bytes)` - `server.py`'s handler reads `await request.body()` (raw bytes) and, for the common single-payload case, `XADD`s those bytes onto `redis` completely unparsed, zero `json.loads`/`json.dumps` on the request path.
  Only a bundled `log_format: json_array` body needs an actual parse, falling back to `enqueue(payloads: list)` for that case.
- `XREADGROUP ... BLOCK` unblocks as soon as it sees a single entry, not once `COUNT` is filled, so a naive `XREADGROUP COUNT 500 BLOCK 2000` loop would insert almost every event as its own batch of one under normal (non-bursty) traffic - the actual cause of ClickHouse taking constant tiny inserts instead of real batches. See "Workers" below for the fix.

## Workers (`webhook-worker`)

- `worker.py` fixes the tiny-batch problem above by accumulating `build_event()`-decoded events across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer, flushing (one `client.insert()` per table for the whole batch) only once `config.BATCH_SIZE` is reached or `config.FLUSH_INTERVAL_MS` has elapsed since the window opened, whichever comes first.
- `agent_name`/`agent_version` can't be resolved at `build_event()` time - it needs a `SELECT` against `agent_invocations`, which only makes sense once that batch's own invocation rows are committed. Left blank there, patched in by `ingest_events_batch()` batch-side, with a per-batch cache so repeated `agent_invocation_id`s trigger only one `SELECT` each.
- See `AGENTS.md`'s "Rules to not violate" for the `source_row`/`ingest_raw` full-payload-preservation rule and its memory-footprint sizing.

## Per-file breakdown (`services/webhook/src/`)

- `server.py` - receives LiteLLM's webhook POSTs, calls `queue_client.enqueue_raw()`. No longer captures anything to disk itself (moved to `loadtest-fixtures`, which reads FROM ClickHouse on demand, not live traffic).
  Never touches ClickHouse for `/api/v1/metrics` (still does for `/api/v1/session-git-branch` and `/api/v1/plan-proposal`).
  Those two routes verify the caller's `Authorization: Bearer <virtual key>` against LiteLLM's own `/key/info` (`config.LITELLM_MASTER_KEY`/`LITELLM_BASE_URL`) before accepting, 401 otherwise.
- `queue_client.py` - Redis Streams client shared by `server.py` (producer) and `worker.py` (consumer).
  `enqueue_raw(body: bytes)` is the request path's entry point - `XADD`s the raw POST body onto `config.STREAM_KEY` unparsed for the common single-payload case, falling back to `enqueue(payloads: list)` (one `XADD` per already-parsed payload) only for a bundled `log_format: json_array` body.
  No `build_event()` call here, deliberately - `config.MAXLEN ~ 5000`, sized so a stuck worker can't grow `redis` past its `mem_limit`.
- `worker.py` - standalone consumer process (`python -m src.worker`, the `webhook-worker` container, same image, `APP_ROLE=worker`).
  Accumulates entries from `config.STREAM_KEY` across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer; `_decode_into()` calls `clickhouse_ingest.build_event()` on each raw payload as it's decoded, flushing once `config.BATCH_SIZE` fills or `config.FLUSH_INTERVAL_MS` elapses, then calls `clickhouse_ingest.ingest_events_batch()` on the accumulated batch.
- `clickhouse_ingest.py` - parses `StandardLoggingPayload` (incl. `Agent`/`Skill` tool_use blocks in `messages`).
  `build_event()` is the pure, DB-free half; `ingest_events_batch()` is the batched-insert half that writes `agent_events`/`agent_usage`/`agent_messages`/`agent_invocations`.
  Also has `ingest_git_branch`/`ingest_plan_proposal`, called from `server.py`'s `/api/v1/session-git-branch`/`/api/v1/plan-proposal` routes, feeding `session_git_branch` (see `hooks/report_git_branch.py`) and `plan_proposals` (see `hooks/report_plan_proposal.py`) - both stay direct-to-ClickHouse, not queued (single low-volume insert per CLI session start / `ExitPlanMode` call).
- `migrate.py` - ClickHouse migration runner for the `clickhouse-migrate` role/service, applies `services/clickhouse/migrations/*.sql` in order.
  Never touches users/roles/grants (see `agent_docs/services/clickhouse.md`'s `init` section for why that's a separate, `make init`-only concern).
- `reparse.py` - CLI for the `metrics-reparse` role, replays `ingest_raw`'s full stored payloads back through `clickhouse_ingest.py` after a parsing bug fix, without needing the original LiteLLM webhook POST again.
- `config.py` - single place for every tunable value across `webhook`/`webhook-worker`: `CLICKHOUSE_*`/`REDIS_*`/`FIXTURES_DIR` (env-derived, no in-code defaults for the required ones - `FIXTURES_DIR` is only for `loadtest.py`'s own read path over the mounted fixtures volume) plus the queue-mechanics constants (`STREAM_KEY`, `CONSUMER_GROUP`, `MAXLEN`, `BATCH_SIZE`, `FLUSH_INTERVAL_MS`, `STALE_IDLE_MS`), loaded from `config.yml`.
  Every other module imports from here rather than reading `os.environ`/hardcoding its own constants.
- `config.yml` - queue-mechanics tunables loaded by `config.py` (sizing rationale lives here, since it's the file you actually edit to tune them): `stream_key`, `consumer_group`, `maxlen`, `batch_size`, `flush_interval_ms`, `stale_idle_ms`, `reparse_chunk_size`.

`services/webhook/tests/` - pytest suite for `clickhouse_ingest.py`'s pure (no-DB) functions, run against real payloads copied into `tests/captures/` from `services/webhook/captures/`.
Run with `make test`, always via `webhook-test-runner`, never inline.

## `fastjson` adoption status

**Python code in this repo must use `services/webhook/src/fastjson.py` (`from . import fastjson as json` or `from . import fastjson`) for JSON load/dump, never stdlib `json`** - a thin orjson-backed drop-in (`load`/`loads`/`dump`/`dumps`, same `indent=2`/`default=` contract), substantially faster than stdlib `json`, and this stack is already CPU-bound on JSON work (see "Why a queue in front of ClickHouse" above).

- **Gotcha: `fastjson.dumps()` returns `bytes`, not `str`.** A caller needing `str` (e.g. `Path.write_text()`, an f-string) must `.decode()` the result - stdlib `json.dumps()` doesn't need this, so a straight swap-in can silently break a caller expecting `str`.
- **Not fully applied yet - tracked here rather than in `AGENTS.md` so updates don't touch the cached prefix:** `worker.py`, `queue_client.py`, `server.py`, `clickhouse_ingest.py`, and `reparse.py` still have leftover raw `import json` calls alongside `fastjson` usage - don't add a new one, but fixing the existing ones isn't required as a side effect of unrelated work.
  Applies to any new Python service too (e.g. `services/mcp-dev`, currently importing neither, and `services/mcp-stats`, which uses stdlib `json`) - import `fastjson` from the start.
- **Same status for the `LOG_LEVEL`/stdlib-`logging` rule:** `services/webhook/src/{worker,migrate,loadtest,reparse,server}.py` still hardcode `INFO`, and `services/mcp-dev`/`services/mcp-stats`/`services/backup`/`services/init` don't use `logging` at all yet - pre-existing gaps against the `AGENTS.md` rule, not yet fixed.
