# `webhook`

Ingest entry point, `services/webhook/`, `CMD uvicorn src.server:app` (x2 compose services, `webhook-1`/`webhook-2`).
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).
See `common.md`'s "Why a queue in front of ClickHouse" for why `webhook` only enqueues instead of writing to ClickHouse directly.

## Per-file breakdown

- `server.py` (`services/webhook/src/`) - receives LiteLLM's webhook POSTs, calls `common.queue_client.enqueue_raw()`.
  No longer captures anything to disk itself (moved to `loadtest-fixtures`, which reads FROM ClickHouse on demand, not live traffic).
  Never touches ClickHouse for `/api/v1/metrics` (still does for `/api/v1/session-git-branch` and `/api/v1/plan-proposal`).
  Those two routes verify the caller's `Authorization: Bearer <virtual key>` against LiteLLM's own `/key/info` (`common.config.litellm.LITELLM_MASTER_KEY`/`LITELLM_BASE_URL`) before accepting, 401 otherwise.

`services/webhook/tests/` currently has no test file of its own (`server.py` has none yet).
Run with `make test` (a separate pytest invocation per service directory - see the `Makefile`), always via `webhook-test-runner`, never inline.
