# `_common`

Shared code for `webhook`/`webhook-worker`/`reparse`, at `services/_common/src/`.
Not its own compose service - imported by the other ingest services below.

## Why a queue in front of ClickHouse

- ClickHouse handles a few large batched inserts far better than many small per-request ones (merge/part amplification, connection overhead per insert).
  Under many concurrent Claude Code/Codex sessions routed through LiteLLM, one `client.insert()` per `StandardLoggingPayload` synchronously inside `webhook`'s HTTP handler would mean many small inserts/sec hitting ClickHouse directly, with backpressure onto `webhook` (and LiteLLM retries) if it falls behind.
- `webhook` only enqueues onto `redis`.
  `webhook-worker` is the only thing that talks to ClickHouse for `/api/v1/metrics` traffic, and the only thing calling `common.ingest_parsing.build_event()` (per payload, on decode - pure, no I/O), batched via `common.ingest_db.ingest_events_batch()`.
  Keeping `build_event()`'s CPU-bound parsing (regex classification, message scanning, JSON work) off `webhook`'s request path matters under load: load testing (`make loadtest`) showed p99 latency climbing under sustained concurrency when parsing ran inline in a single-threaded event loop, serializing behind whichever request's parsing got there first.
- `request.json()`/`json.dumps()` themselves are real CPU work - parsing and re-serializing a ~360KB-1.5MB `StandardLoggingPayload` twice per request pegged `webhook-1`/`webhook-2` (each `cpus: 1.0`) at 65-96% CPU under load even with `build_event()` off the request path.
  Fix: `queue_client.enqueue_raw(body: bytes)` - `server.py`'s handler reads `await request.body()` (raw bytes) and, for the common single-payload case, `XADD`s those bytes onto `redis` completely unparsed, zero `json.loads`/`json.dumps` on the request path.
  Only a bundled `log_format: json_array` body needs an actual parse, falling back to `enqueue(payloads: list)` for that case.
- `XREADGROUP ... BLOCK` unblocks as soon as it sees a single entry, not once `COUNT` is filled, so a naive `XREADGROUP COUNT 500 BLOCK 2000` loop would insert almost every event as its own batch of one under normal (non-bursty) traffic - the actual cause of ClickHouse taking constant tiny inserts instead of real batches.
  See `agent_docs/services/worker.md` for the fix.

## Per-file breakdown

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

The pure/DB-touching `ingest_parsing`/`ingest_db` pytest suite (real payloads from `tests/captures/`) lives at `services/_common/tests/`.
Run with `make test` (a separate pytest invocation per service directory - see the `Makefile`), always via `webhook-test-runner`, never inline.

## `fastjson` adoption status

**Python code in this repo must use `services/_common/src/fastjson.py` (`from common import fastjson as json` or `from common import fastjson`) for JSON load/dump, never stdlib `json`** - a thin orjson-backed drop-in (`load`/`loads`/`dump`/`dumps`, same `indent=2`/`default=` contract), substantially faster than stdlib `json`, and this stack is already CPU-bound on JSON work (see "Why a queue in front of ClickHouse" above).

- **Gotcha: `fastjson.dumps()` returns `bytes`, not `str`.** A caller needing `str` (e.g. `Path.write_text()`, an f-string) must `.decode()` the result - stdlib `json.dumps()` doesn't need this, so a straight swap-in can silently break a caller expecting `str`.
- **Not fully applied yet - tracked here rather than in `AGENTS.md` so updates don't touch the cached prefix:** `worker.py`, `server.py`, and `reparse.py` still have leftover raw `import json` calls alongside `fastjson` usage - don't add a new one, but fixing the existing ones isn't required as a side effect of unrelated work.
  Applies to any new Python service too (e.g. `services/mcp-dev`, currently importing neither, and `services/mcp-stats`, which uses stdlib `json`) - import `fastjson` from the start.
- **Same status for the `LOG_LEVEL`/stdlib-`logging` rule:** `server.py`/`worker.py`/`migrate.py`/`loadtest.py`/`reparse.py` still hardcode `INFO`, and `services/mcp-dev`/`services/mcp-stats`/`services/backup`/`services/init` don't use `logging` at all yet - pre-existing gaps against the `AGENTS.md` rule, not yet fixed.
