# `webhook`

Ingest entry point, `services/webhook/`, `CMD uvicorn src.server:app` (x2 compose services, `webhook-1`/`webhook-2`).
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).
See `common.md`'s "Why a queue in front of ClickHouse" for why `webhook` only enqueues instead of writing to ClickHouse directly.

## Usage routes

- `POST /api/v1/metrics` accepts LiteLLM's `StandardLoggingPayload` callback.
  It keeps the common single-object body as raw bytes and acknowledges best-effort enqueueing so LiteLLM does not retry malformed callback payloads forever.
- `POST /api/v1/usage-events` accepts one `UsageEnvelopeV1` or a batch of up to 100 from the direct Claude transcript collector.
  It validates the bearer token through LiteLLM `/key/info`, enriches every envelope with the virtual key's user/team identity, and rejects the whole batch if any envelope is invalid.
  `/key/info` has no team alias, so the Team's display name comes from a separate TTL-cached `/team/info` lookup (`common.litellm_auth.team_alias`) - without it this path can only label a Team by its uuid, and `claude_transcript_adapter` skips the `ai_gateway_groups` row entirely rather than overwrite a good name with an empty one.
  Redis failures return `503`, so the collector retains the batch in its SQLite outbox and retries later.

## Per-file breakdown

- `server.py` (`services/webhook/src/`) - owns the LiteLLM callback route, the authenticated direct-usage route, health, and low-volume metadata/alert routes.
  No longer captures anything to disk itself (moved to `loadtest-fixtures`, which reads FROM ClickHouse on demand, not live traffic).
  It never writes usage or side events directly to ClickHouse.
  `/api/v1/usage-events`, `/api/v1/session-git-branch`, and `/api/v1/plan-proposal` verify `Authorization: Bearer <virtual key>` against LiteLLM's own `/key/info` (`common.config.litellm.LITELLM_MASTER_KEY`/`LITELLM_BASE_URL`) before accepting, 401 otherwise.
- `queue.py` (`services/webhook/src/`) - Redis Streams producer for raw LiteLLM payloads, source-tagged direct usage, and the separate side stream.
  `enqueue_usage_events()` uses one non-transactional pipeline for the batch and intentionally propagates failure to `server.py`.

`services/webhook/tests/` has route/auth/envelope coverage in `test_server.py` and producer coverage in `test_queue.py`.
Run with `make test-services` (a separate pytest invocation per service directory - see the `Makefile`), always via `runner-test`, never inline.
