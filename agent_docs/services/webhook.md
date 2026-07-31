# `webhook` / `webhook-worker`

Five independent services, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/`:

- `services/webhook/` - `webhook`(x2 compose services, `webhook-1`/`webhook-2`), `CMD uvicorn src.server:app`
- `services/worker/` - `webhook-worker`, `CMD python -m src.worker`
- `services/reparse/` - `metrics-reparse`, `CMD python -m src.reparse`
- `services/migrate/` - `clickhouse-migrate`, `CMD python -m src.migrate`
- `services/loadtest/` - `make loadtest`'s replay role, not a standing compose service, `CMD python -m src.loadtest`

## Why a queue in front of ClickHouse

- ClickHouse handles a few large batched inserts far better than many small per-request ones (merge/part amplification, connection overhead per insert).
  Under many concurrent Claude Code/Codex sessions routed through LiteLLM, one `client.insert()` per `StandardLoggingPayload` synchronously inside `webhook`'s HTTP handler would mean many small inserts/sec hitting ClickHouse directly, with backpressure onto `webhook` (and LiteLLM retries) if it falls behind.
- `webhook` only enqueues onto `redis`.
  `webhook-worker` is the only thing that talks to ClickHouse for `/api/v1/metrics` traffic, and the only thing calling `common.ingest_parsing.build_event()` (per payload, on decode - pure, no I/O), batched via `common.ingest_db.ingest_events_batch()`.
  Keeping `build_event()`'s CPU-bound parsing (regex classification, message scanning, JSON work) off `webhook`'s request path matters under load: load testing (`make loadtest`) showed p99 latency climbing under sustained concurrency when parsing ran inline in a single-threaded event loop, serializing behind whichever request's parsing got there first.
- `request.json()`/`json.dumps()` themselves are real CPU work - parsing and re-serializing a ~360KB-1.5MB `StandardLoggingPayload` twice per request pegged `webhook-1`/`webhook-2` (each `cpus: 1.0`) at 65-96% CPU under load even with `build_event()` off the request path.
  Fix: `queue_client.enqueue_raw(body: bytes)` - `server.py`'s handler reads `await request.body()` (raw bytes) and, for the common single-payload case, `XADD`s those bytes onto `redis` completely unparsed, zero `json.loads`/`json.dumps` on the request path.
  Only a bundled `log_format: json_array` body needs an actual parse, falling back to `enqueue(payloads: list)` for that case.
- `XREADGROUP ... BLOCK` unblocks as soon as it sees a single entry, not once `COUNT` is filled, so a naive `XREADGROUP COUNT 500 BLOCK 2000` loop would insert almost every event as its own batch of one under normal (non-bursty) traffic - the actual cause of ClickHouse taking constant tiny inserts instead of real batches. See "Workers" below for the fix.

## Workers (`webhook-worker`)

- `worker.py` fixes the tiny-batch problem above by accumulating `build_event()`-decoded events across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer, flushing (one `client.insert()` per table for the whole batch) only once `common.config.queue.BATCH_SIZE` is reached or `FLUSH_INTERVAL_MS` has elapsed since the window opened, whichever comes first.
- `agent_name`/`agent_version` can't be resolved at `build_event()` time - it needs a `SELECT` against `agent_invocations`, which only makes sense once that batch's own invocation rows are committed. Left blank there, patched in by `ingest_events_batch()` batch-side, with a per-batch cache so repeated `agent_invocation_id`s trigger only one `SELECT` each.
- See `AGENTS.md`'s "Rules to not violate" for the `source_row`/`ingest_raw` full-payload-preservation rule and its memory-footprint sizing.

## Per-file breakdown

- `server.py` (`services/webhook/src/`) - receives LiteLLM's webhook POSTs, calls `common.queue_client.enqueue_raw()`. No longer captures anything to disk itself (moved to `loadtest-fixtures`, which reads FROM ClickHouse on demand, not live traffic).
  Never touches ClickHouse for `/api/v1/metrics` (still does for `/api/v1/session-git-branch` and `/api/v1/plan-proposal`).
  Those two routes verify the caller's `Authorization: Bearer <virtual key>` against LiteLLM's own `/key/info` (`common.config.litellm.LITELLM_MASTER_KEY`/`LITELLM_BASE_URL`) before accepting, 401 otherwise.
- `worker.py` (`services/worker/src/`) - standalone consumer process (`python -m src.worker`, the `webhook-worker` container).
  Accumulates entries from `common.config.queue.STREAM_KEY` across repeated `XREADGROUP`/`XAUTOCLAIM` calls into a buffer; `_decode_into()` calls `common.ingest_parsing.build_event()` on each raw payload as it's decoded, flushing once `common.config.queue.BATCH_SIZE` fills or `FLUSH_INTERVAL_MS` elapses, then calls `common.ingest_db.ingest_events_batch()` on the accumulated batch.
- `migrate.py` (`services/migrate/src/`) - ClickHouse migration runner for the `clickhouse-migrate` service, applies `services/clickhouse/migrations/*.sql` in order.
  Never touches users/roles/grants (see `agent_docs/services/clickhouse.md`'s `init` section for why that's a separate, `make init`-only concern).
- `reparse.py` (`services/reparse/src/`) - CLI for the `metrics-reparse` service, replays `ingest_raw`'s full stored payloads back through `common.ingest_db.reparse_event()` after a parsing bug fix, without needing the original LiteLLM webhook POST again.
- `loadtest.py` (`services/loadtest/src/`) - CLI load generator, replays real captured traffic against `webhook`'s own `POST /api/v1/metrics` - see the "Load testing" section elsewhere for the full model.

Shared by `webhook`/`webhook-worker`/`reparse` (`services/_common/src/`, not any one service's own):

- `ingest_parsing.py` - parses `StandardLoggingPayload` (incl. `Agent`/`Skill` tool_use blocks in `messages`); pure, no ClickHouse import.
  `build_event()` is the DB-free entry point called from `worker.py`.
- `ingest_db.py` - the ClickHouse-I/O half: `ingest_events_batch()` (the batched-insert path writing `agent_events`/`agent_usage`/`agent_messages`/`agent_invocations`), `reparse_event()` (the reparse-only entry point), and `ingest_git_branch`/`ingest_plan_proposal`, called from `server.py`'s `/api/v1/session-git-branch`/`/api/v1/plan-proposal` routes, feeding `session_git_branch` (see `hooks/report_git_branch.py`) and `plan_proposals` (see `hooks/report_plan_proposal.py`) - both stay direct-to-ClickHouse, not queued (single low-volume insert per CLI session start / `ExitPlanMode` call).
- `config/` - per-concern tunables (`clickhouse.py`, `clickhouse_credentials.py`, `redis.py`, `litellm.py`, `queue.py`), env-derived, no in-code defaults for the required ones.
  `config/queue.py` loads the queue-mechanics constants (`STREAM_KEY`, `CONSUMER_GROUP`, `MAXLEN`, `BATCH_SIZE`, `FLUSH_INTERVAL_MS`, `STALE_IDLE_MS`) from `queue.yml` (sizing rationale lives there).
  `FIXTURES_DIR` and `REPARSE_CHUNK_SIZE` stay service-local (read directly in `loadtest.py`/`reparse.py`), not part of this shared package.
- `queue_client.py` - Redis Streams client shared by `server.py` (producer) and `worker.py` (consumer).
  `enqueue_raw(body: bytes)` is the request path's entry point - `XADD`s the raw POST body onto `config.queue.STREAM_KEY` unparsed for the common single-payload case, falling back to `enqueue(payloads: list)` (one `XADD` per already-parsed payload) only for a bundled `log_format: json_array` body.
  No `build_event()` call here, deliberately - `config.queue.MAXLEN ~ 1500`, sized so a stuck worker can't grow `redis` past its `mem_limit`.
- `fastjson.py` - see "`fastjson` adoption status" below.

`worker.py`/`reparse.py`/`loadtest.py` each have their own `tests/` under `services/worker/`/`services/reparse/`/`services/loadtest/`; the pure/DB-touching `ingest_parsing`/`ingest_db` pytest suite (real payloads from `tests/captures/`) lives at `services/_common/tests/`.
`services/webhook/tests/` currently has no test file of its own (`server.py` has none yet).
Run with `make test` (a separate pytest invocation per service directory - see the `Makefile`), always via `webhook-test-runner`, never inline.

## `fastjson` adoption status

**Python code in this repo must use `services/_common/src/fastjson.py` (`from common import fastjson as json` or `from common import fastjson`) for JSON load/dump, never stdlib `json`** - a thin orjson-backed drop-in (`load`/`loads`/`dump`/`dumps`, same `indent=2`/`default=` contract), substantially faster than stdlib `json`, and this stack is already CPU-bound on JSON work (see "Why a queue in front of ClickHouse" above).

- **Gotcha: `fastjson.dumps()` returns `bytes`, not `str`.** A caller needing `str` (e.g. `Path.write_text()`, an f-string) must `.decode()` the result - stdlib `json.dumps()` doesn't need this, so a straight swap-in can silently break a caller expecting `str`.
- **Not fully applied yet - tracked here rather than in `AGENTS.md` so updates don't touch the cached prefix:** `worker.py`, `server.py`, and `reparse.py` still have leftover raw `import json` calls alongside `fastjson` usage - don't add a new one, but fixing the existing ones isn't required as a side effect of unrelated work.
  Applies to any new Python service too (e.g. `services/mcp-dev`, currently importing neither, and `services/mcp-stats`, which uses stdlib `json`) - import `fastjson` from the start.
- **Same status for the `LOG_LEVEL`/stdlib-`logging` rule:** `server.py`/`worker.py`/`migrate.py`/`loadtest.py`/`reparse.py` still hardcode `INFO`, and `services/mcp-dev`/`services/mcp-stats`/`services/backup`/`services/init` don't use `logging` at all yet - pre-existing gaps against the `AGENTS.md` rule, not yet fixed.
