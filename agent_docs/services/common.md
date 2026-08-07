# `_common`

Shared parsing, source-adapter, and ClickHouse code for `webhook`/`webhook-worker`/`reparse`, at `services/_common/src/`.
Not its own compose service - imported by the other ingest services below.

## Why a queue in front of ClickHouse

- ClickHouse handles a few large batched inserts far better than many small per-request ones (merge/part amplification, connection overhead per insert).
  Under many concurrent proxied and direct agent sessions, one `client.insert()` per usage payload synchronously inside `webhook`'s HTTP handler would mean many small inserts/sec hitting ClickHouse directly, with backpressure onto `webhook` if it falls behind.
- `webhook` only enqueues onto `redis`.
  `webhook-worker` is the only thing that talks to ClickHouse for `/api/v1/metrics` and `/api/v1/usage-events` traffic.
  It calls `common.ingest_adapters.build_ingest_event()` per payload, which dispatches to the LiteLLM or Claude-transcript adapter before batching through `common.ingest_db.ingest_events_batch()`.
  Keeping `build_event()`'s CPU-bound parsing (regex classification, message scanning, JSON work) off `webhook`'s request path matters under load: load testing (`make loadtest`) showed p99 latency climbing under sustained concurrency when parsing ran inline in a single-threaded event loop, serializing behind whichever request's parsing got there first.
- `request.json()`/`json.dumps()` themselves are real CPU work - parsing and re-serializing a ~360KB-1.5MB `StandardLoggingPayload` twice per request pegged `webhook-1`/`webhook-2` (each `cpus: 1.0`) at 65-96% CPU under load even with `build_event()` off the request path.
  Fix: `services/webhook/src/queue.py`'s `enqueue_raw(body: bytes)` - `server.py`'s handler reads `await request.body()` (raw bytes) and, for the common single-payload case, `XADD`s those bytes onto `redis` completely unparsed, zero `json.loads`/`json.dumps` on the request path.
  Only a bundled `log_format: json_array` body needs an actual parse, falling back to `enqueue(payloads: list)` for that case.
- `XREADGROUP ... BLOCK` unblocks as soon as it sees a single entry, not once `COUNT` is filled, so a naive `XREADGROUP COUNT 500 BLOCK 2000` loop would insert almost every event as its own batch of one under normal (non-bursty) traffic - the actual cause of ClickHouse taking constant tiny inserts instead of real batches.
  See `agent_docs/services/worker.md` for the fix.

## Per-file breakdown

- `ingest_parsing.py` - parses `StandardLoggingPayload` (incl. `Agent`/`Skill` tool_use blocks in `messages`); pure, no ClickHouse import.
  `build_event()` is the DB-free LiteLLM adapter entry point called through `ingest_adapters.py`.
- `client_attribution.py` - resolves normalized `client_product`, `client_surface`, and `ingest_path` values at the adapter boundary.
  It trusts only allowlisted explicit values, distinguishes Codex CLI/Desktop through the originator marker, and keeps ambiguous inputs as `unknown`.
- `usage_envelope.py` - validates and normalizes the versioned `UsageEnvelopeV1` contract for direct clients.
  Its strict allowlist excludes prompts, responses, messages, and tool arguments.
  Identity is accepted from the server-side virtual-key lookup, not trusted from the collector.
- `claude_transcript_adapter.py` - converts a normalized direct Claude envelope into the existing ingest row bundle.
  It emits event/usage/source/dimension rows but no `agent_messages` row.
  The transcript `requestId`/envelope `event_id` becomes `litellm_call_id` for retry dedupe.
- `ingest_adapters.py` - worker-side source registry.
  Redis entries without an explicit adapter use `litellm_standard`.
  Direct transcript entries use `claude_transcript`.
- `model_pricing.py` - fetches and caches LiteLLM's live public model cost map for direct-call API-equivalent estimates.
  A failed refresh leaves pricing unavailable for that event and retries after 30 seconds instead of inventing a fallback rate.
- `ingest_db.py` - the ClickHouse-I/O half: `ingest_events_batch()` (the batched-insert path writing `agent_events`/`agent_usage`/`agent_messages`/`agent_invocations`) and `reparse_event()` (the reparse-only entry point), plus `get_client()`.
  The batch-side client dictionary resolution patches both `agent_events.event_client_id` and `agent_usage.client_id` with the same exact-client ID.
- `side_ingest.py` - builds and inserts the worker's low-volume `session_git_branch`, `plan_proposals`, and `litellm_alerts` side-stream rows.
- `config/` - per-concern tunables (`clickhouse.py`, `clickhouse_credentials.py`, `redis.py`, `litellm.py`, `queue.py`), env-derived, no in-code defaults for the required ones.
  `config/queue.py` loads the queue-mechanics constants (`STREAM_KEY`, `CONSUMER_GROUP`, `MAXLEN`, `BATCH_SIZE`, `FLUSH_INTERVAL_MS`, `STALE_IDLE_MS`) from `queue.yml` (sizing rationale lives there).
  `queue.yml` itself sits at `services/_common/queue.yml`, beside `src/` rather than inside it - config data doesn't belong under source, matching the `config.yml`-beside-`src/` pattern other services (`reparse`, `mcp-dev`, `grafana`) already use.
  Every Dockerfile that bakes in `_common` has its own second `COPY services/_common/queue.yml ./common/queue.yml` line alongside the `COPY services/_common/src/ ./common/` one, since Docker's build context can't reach outside what's explicitly copied.
  `FIXTURES_DIR` and `REPARSE_CHUNK_SIZE` stay service-local (read directly in `loadtest.py`/`reparse.py`), not part of this shared package.
- No more `queue_client.py` here - it split by service, since the producer (`server.py`) and consumer (`worker.py`) halves never shared code, only the `config/queue.py`/`config/redis.py` constants above.
  `services/webhook/src/queue.py` has `enqueue_raw(body: bytes)`, the request path's entry point - `XADD`s the raw POST body onto `config.queue.STREAM_KEY` unparsed for the common single-payload case, falling back to `enqueue(payloads: list)` (one `XADD` per already-parsed payload) only for a bundled `log_format: json_array` body.
  `enqueue_usage_events()` pipelines a validated direct-usage batch onto the same stream with `adapter=claude_transcript` and propagates Redis failures so the caller's durable outbox can retry.
  No `build_event()` call here, deliberately - `config.queue.MAXLEN ~ 1500`, sized so a stuck worker can't grow `redis` past its `mem_limit`.
  `services/worker/src/queue.py` has `get_redis()`, the consumer's blocking client.
- `fastjson.py` - see "`fastjson` adoption status" below.

The common pytest suite covers LiteLLM parsing/DB writes, `UsageEnvelopeV1`, the Claude transcript adapter/collector, and direct-call pricing.
Real LiteLLM payloads live under `services/_common/tests/captures/`.
Run with `make test-services` (a separate pytest invocation per service directory - see the `Makefile`), always via `runner-test`, never inline.

## `fastjson` adoption status

Python code in this repo must use `services/_common/src/fastjson.py` (`from common import fastjson as json` or `from common import fastjson`) for JSON load/dump, never stdlib `json`.
It's a thin orjson-backed drop-in (`load`/`loads`/`dump`/`dumps`, same `indent=2`/`default=` contract), substantially faster than stdlib `json`, and this stack is already CPU-bound on JSON work (see "Why a queue in front of ClickHouse" above).

- Gotcha: `fastjson.dumps()` returns `bytes`, not `str`.
  A caller needing `str` (e.g. `Path.write_text()`, an f-string) must `.decode()` the result - stdlib `json.dumps()` doesn't need this, so a straight swap-in can silently break a caller expecting `str`.
- Not fully applied yet: `worker.py`, `server.py`, and `reparse.py` still have leftover raw `import json` calls alongside `fastjson` usage - don't add a new one, but fixing the existing ones isn't required as a side effect of unrelated work.
  Tracked here rather than in `AGENTS.md` so updates don't touch the cached prefix.
  Applies to any new Python service too (e.g. `services/mcp-dev`, currently importing neither, and `services/mcp-stats`, which uses stdlib `json`) - import `fastjson` from the start.
- Same status for the `LOG_LEVEL`/stdlib-`logging` rule: `server.py`/`worker.py`/`migrate.py`/`loadtest.py`/`reparse.py` still hardcode `INFO`, and `services/mcp-dev`/`services/mcp-stats`/`services/backup`/`services/init` don't use `logging` at all yet - pre-existing gaps against the `AGENTS.md` rule, not yet fixed.
