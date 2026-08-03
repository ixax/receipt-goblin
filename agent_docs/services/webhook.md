# `webhook`

Ingest entry point, `services/webhook/`, `CMD uvicorn src.server:app` (x2 compose services, `webhook-1`/`webhook-2`).
One of five independent services split from a former single `webhook` service, each with its own Dockerfile/image/`CMD`, sharing code from `services/_common/src/` (`agent_docs/services/common.md`).
See `common.md`'s "Why a queue in front of ClickHouse" for why `webhook` only enqueues instead of writing to ClickHouse directly.

## Per-file breakdown

- `server.py` (`services/webhook/src/`) - receives LiteLLM's webhook POSTs, calls `.queue.enqueue_raw()`.
  No longer captures anything to disk itself (moved to `loadtest-fixtures`, which reads FROM ClickHouse on demand, not live traffic).
  Never touches ClickHouse for `/api/v1/metrics` (still does for `/api/v1/session-git-branch` and `/api/v1/plan-proposal`, via `.ingest`).
  Those two routes verify the caller's `Authorization: Bearer <virtual key>` against LiteLLM's own `/key/info` (`common.config.litellm.LITELLM_MASTER_KEY`/`LITELLM_BASE_URL`) before accepting, 401 otherwise.
- `ingest.py` (`services/webhook/src/`) - `ingest_git_branch`/`ingest_plan_proposal`/`ingest_litellm_alert`, single-caller functions moved out of `common.ingest_db`/`common.ingest_parsing` (see `plans/common-module-cleanup-refactor.md`).
- `queue.py` (`services/webhook/src/`) - `get_async_redis`/`enqueue`/`enqueue_raw`, the producer half of `common.queue_client` before it split by service.

`services/webhook/tests/` has `test_ingest.py` and `test_queue.py`.
Run with `make test-services` (a separate pytest invocation per service directory - see the `Makefile`), always via `runner-test`, never inline.
