# Split `services/webhook/` into independent services

## Context

`services/webhook/src/` is a monolith (`server.py`, `worker.py`, `queue_client.py`, `clickhouse_ingest.py` at 1613 lines, `loadtest.py`, `reparse.py`, `migrate.py`, `config.py`, `fastjson.py`) sharing one `Dockerfile`/`requirements.txt`, with `APP_ROLE` picking the runtime role (five roles today: `server`/`worker`/`reparse`/`migrate`/`loadtest` — `docker-entrypoint.sh` and the `Dockerfile` header comment are stale and still say "four").
A prior TODO note considered splitting this but rejected it, to avoid duplicating a ~214MB image across roles.

Decision for this refactor: do the split anyway, accepting that image-duplication cost, because the current single `config.py` forces every role to set every env var it doesn't use (e.g. `clickhouse-migrate` needs `REDIS_HOST/PORT` purely so `config.py` doesn't crash on import), and `clickhouse_ingest.py` mixes pure parsing with ClickHouse I/O with no clean boundary.
`reparse.py` today reaches into 14 of its private (`_`-prefixed) helpers directly, an undeclared internal API.

Target: five independent services (`webhook`, `worker`, `reparse`, `migrate`, `loadtest`), each with its own Dockerfile/requirements.txt/image tag, pulling genuinely shared code (parsing, ClickHouse I/O, Redis Streams protocol, config-reading, JSON) from `services/_common/src/` — the shared-code directory that already exists in this repo (created by commit `9efbe99`, "Refactor common services/ logic.", 2026-07-29), not a new directory.
`services/_common/src/` today holds `litellm_auth.py`, `logging_config.py`, `mcp_common.py`, `__init__.py`.
It's already `COPY`'d cross-directory into `webhook`, `mcp-dev`, `mcp-stats`, and `loadtest-fixtures` Dockerfiles (`COPY services/_common/src/ ./common/`), already bind-mounted in `docker-compose.dev.yml` for all of them, and imported everywhere as `from common.<module> import ...`.
This refactor extends that existing directory rather than inventing a parallel `services/_shared/`.

Two precedent splits already exist in this repo and should be modeled directly:

- `services/loadtest-fixtures/` (pulled out of the old `.capture` flow into its own `Dockerfile`/image/`requirements.txt`/`config.py`)
- `services/mcp-dev/` + `services/mcp-stats/` (split from one `mcp-server`)

Both follow the same shape: own `Dockerfile` with `COPY services/_common/src/ ./common/` + `COPY services/<name>/src/ ./src/`, own trimmed `requirements.txt`, own `config.py`/`config.yml`, own `image:` in `docker-compose.yml`, own `VERSIONS.yml` key.

Note: `services/webhook/requirements.txt`, `Dockerfile`, and `docker-entrypoint.sh` all carry an explicit, current comment saying the flat/shared-deps approach is deliberate ("harmless to bake into other roles") — the opposite stance from the two precedent splits, both of which have trimmed per-service `requirements.txt`.
This split reverses that stated stance for `webhook`'s family.
Flag this explicitly when landing, since it contradicts a comment written days before this plan, not stale doc rot.

## Target layout

```
services/
  _common/src/                    # EXISTING dir, extended (not services/_shared/)
    litellm_auth.py                # existing, unchanged
    logging_config.py              # existing, unchanged
    mcp_common.py                  # existing, unchanged (FastMCP-only, irrelevant to webhook family)
    fastjson.py                     # MOVED from services/webhook/src/
    queue_client.py                 # MOVED (webhook=producer, worker=consumer; one protocol, can't drift)
    ingest_parsing.py               # NEW: pure/DB-free half of clickhouse_ingest.py
    ingest_db.py                    # NEW: ClickHouse-I/O half of clickhouse_ingest.py
    queue.yml                       # queue-mechanics half of today's config.yml
    config/
      clickhouse.py                 # HOST/PORT/DATABASE
      clickhouse_credentials.py     # USER/PASSWORD (ingest role)
      redis.py                      # HOST/PORT
      litellm.py                    # MASTER_KEY/BASE_URL
      queue.py                      # STREAM_KEY/CONSUMER_GROUP/MAXLEN/BATCH_SIZE/FLUSH_INTERVAL_MS/STALE_IDLE_MS (reads queue.yml)
  _common/tests/                   # test_fastjson.py, test_queue_client.py, test_ingest_parsing.py,
                                    # test_ingest_db.py (split from today's test_clickhouse_ingest.py),
                                    # captures/*.json (moved corpus, not duplicated)

  webhook/            # trimmed to just the FastAPI app
    Dockerfile requirements.txt src/server.py tests/

  worker/             # new — models services/loadtest-fixtures/ structure
    Dockerfile requirements.txt
    src/worker.py src/config.py (WORKER_METRICS_PORT)
    tests/test_worker.py

  reparse/            # new
    Dockerfile requirements.txt config.yml (REPARSE_CHUNK_SIZE)
    src/reparse.py src/config.py
    tests/test_reparse.py (new)

  migrate/            # new
    Dockerfile requirements.txt
    src/migrate.py src/config.py
    (still COPYs services/clickhouse/migrations/, same as today)

  loadtest/           # new
    Dockerfile requirements.txt
    src/loadtest.py
    tests/test_loadtest.py
```

Note: `capture.py` (`CAPTURE_DIR`/`CAPTURE_ENABLED`) doesn't correspond to anything in current `config.py` — current `config.py` has `FIXTURES_DIR`, used only by `loadtest.py`.
Drop the `capture.py` module; keep `FIXTURES_DIR` as `loadtest`'s own service-local config, not shared.

## `clickhouse_ingest.py` split (the core of this refactor)

Confirmed current top-level contents (67 functions/classes, 1613 lines) split cleanly along one line:

- **`ingest_parsing.py`** (pure, DB-free): `_to_dt`, `_flatten_content`, `_last_user_text`, `_codex_collaboration_mode_change`, `_codex_session_id`, `_active_command_name_and_version`, `_failed_tool_call`, `EventContext`, `_derive_context` (takes an optional `client` but never imports `ingest_db`, keeping the dependency arrow one-directional), `_classify_event`, `_agent_invocations_from_messages`, all `_*_row` builders (`_source_row`, `_event_row`, `_usage_row`, `_message_row`, `_group_row`, `_user_row`), `_agent_invocation_rows`, serialize/deserialize helpers (`_serialize_row`, `_serialize_row_multi`, `_deserialize_row`, `_deserialize_row_multi`), `_issue_id_from_branch`, and **`build_event(payload)`** (worker's only entry point).
  Promote `_session_and_trace_id` → **`session_and_trace_id`** (public), since `server.py` already reaches into it today for capture-file naming.
- **`ingest_db.py`** (ClickHouse I/O): `get_client()`, `clickhouse_alive()`, all `_insert_*` (9 functions), `_BatchWriter`, `_resolve_client_id`, `ingest_events_batch()`, `ingest_git_branch()`, `ingest_plan_proposal()`, `ingest_litellm_alert()`.
  Imports specific names from `ingest_parsing`.
  **New public function `reparse_event(client, payload, litellm_call_id, source_session_id, now)`** — formalizes `reparse.py`'s current 14-private-helper reach-through into one real API call.
  Confirmed reach-through list:
  - `_agent_invocation_rows`
  - `_derive_context`
  - `_event_row`
  - `_group_row`
  - `_insert_agent_invocations`
  - `_insert_ai_gateway_groups`
  - `_insert_ai_gateway_users`
  - `_insert_event`
  - `_insert_message`
  - `_insert_usage`
  - `_message_row`
  - `_session_and_trace_id`
  - `_usage_row`
  - `_user_row`
  - plus public `get_client`

  `reparse.py` shrinks to: decode `raw_payload_full`, call `reparse_event(...)`, keep its existing try/except-log wrapper.
- `ingest_standard_logging_payload`/`ingest_webhook_body` have zero call sites today (leftover from before the Redis-queue split).
  Carry them into `ingest_db.py` unchanged for parity, flag as follow-up cleanup candidates.

## `config.py` split

Per-concern modules under `_common/src/config/` (`clickhouse.py`, `clickhouse_credentials.py`, `redis.py`, `litellm.py`, `queue.py`), each doing the same unconditional `os.environ[...]` reads as today, no new defaults.
Service-local config (not shared): `worker`'s `WORKER_METRICS_PORT` constant, `reparse`'s `REPARSE_CHUNK_SIZE` (own `config.yml`), `loadtest`'s `FIXTURES_DIR`, `migrate`'s bootstrap/ingest creds (already inline `os.environ` reads today, unaffected).

Result:

- `migrate` drops `LITELLM_*`/`REDIS_*`
- `reparse` drops `LITELLM_*`/`REDIS_*`
- `loadtest` drops `CLICKHOUSE_*`/`REDIS_*`/`LITELLM_*` entirely
- `worker` drops `LITELLM_*`

This is the actual coupling fix.

## Dockerfiles / requirements.txt

Model directly on `services/loadtest-fixtures/Dockerfile` and `services/mcp-dev/Dockerfile`'s existing structure (both already do exactly this): `context: .` (repo root), `COPY services/_common/src/ ./common/` then `COPY services/<name>/src/ ./src/` — the same cross-directory-COPY pattern already proven in production for four other services.
Per-service deps, reusing the role-specific comments already in today's `requirements.txt`:

| Service  | Keeps                                                                                       | Drops (vs. today's shared superset)                                                                                                             |
| -------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| webhook  | fastapi, uvicorn, prometheus-fastapi-instrumentator, clickhouse-connect, redis, PyYAML, orjson | prometheus-client, aiohttp                                                                                                                            |
| worker   | clickhouse-connect, redis, PyYAML, prometheus-client, orjson                                  | fastapi, uvicorn, prometheus-fastapi-instrumentator, aiohttp                                                                                          |
| reparse  | clickhouse-connect, PyYAML                                                                    | fastapi/uvicorn stack, redis, aiohttp, orjson (reparse uses stdlib json today)                                                                        |
| migrate  | clickhouse-connect                                                                             | everything else                                                                                                                                        |
| loadtest | aiohttp                                                                                        | everything else (doesn't even need `shared/` — its one `FIXTURES_DIR` read can stay a local one-line config, skip `COPY services/_common/` for this service) |

`docker-entrypoint.sh` and its `APP_ROLE` case-dispatch are deleted entirely — each Dockerfile gets a direct `CMD` (e.g. worker: `CMD ["python", "-m", "src.worker"]`).
`docker-compose.dev.yml`'s `--reload` override for `webhook-1`/`webhook-2` still works unmodified against a bare `CMD`.

## `docker-compose.yml` / `VERSIONS.yml` / `Makefile`

- Five existing blocks map onto five new Dockerfiles:
  - `webhook-1`/`webhook-2` → `services/webhook/Dockerfile`
  - `worker` → `services/worker/Dockerfile`
  - `metrics-reparse` → `services/reparse/Dockerfile` (consider renaming the compose service to `reparse` — touches `Makefile`'s `reparse`/`reparse-all` targets, which reference the compose service name directly)
  - `clickhouse-migrate` → `services/migrate/Dockerfile`
  - `loadtest` → `services/loadtest/Dockerfile`
- `VERSIONS.yml` (note: the actual filename is plural, not `VERSION.yml`): replace single `WEBHOOK_TAG` with `WEBHOOK_TAG`/`WORKER_TAG`/`REPARSE_TAG`/`MIGRATE_TAG`/`LOADTEST_TAG`, matching the per-service key pattern already used for `LOADTEST_FIXTURES_TAG`/`MCP_DEV_TAG`/`MCP_STATS_TAG`.
  `scripts/resolve_image_version.py` needs no code change — confirmed fully generic over `VERSIONS.yml` keys, no hardcoded service list.
- Trim each service's `environment:` block per the config split above — biggest wins are `clickhouse-migrate` (drops `LITELLM_*`/`REDIS_*`) and `loadtest` (drops its whole ClickHouse-creds-it-doesn't-use block).
- `ports`/`healthcheck`/`depends_on`/`networks`/`profiles`/static IPs (`172.28.1.11`/`.12` for webhook-1/2) are unaffected — don't touch.
- **`worker`'s healthcheck** references `src.queue_client` today — must become `common.queue_client` or it silently starts reporting unhealthy without crashing the container.
- `docker-compose.dev.yml`'s bind mounts need updating to each service's own `src/` dir.
  `./services/_common/src:/app/common:ro` mounts already exist for `webhook-1`/`webhook-2`/`worker`/`clickhouse-migrate`/`metrics-reparse` today — carry them forward under the new service directories rather than adding from scratch.
  `loadtest` currently has no dev bind-mount block at all — decide whether to add one or leave it image-only, matching today.

## Tests

Mirror the source split:

- `worker/tests/test_worker.py` (moved verbatim)
- `loadtest/tests/test_loadtest.py` (moved verbatim)
- `reparse/tests/test_reparse.py` (new, covers `reparse_event()`)
- `_common/tests/test_fastjson.py`
- `_common/tests/test_queue_client.py`
- today's 860-line `test_clickhouse_ingest.py` split into `_common/tests/test_ingest_parsing.py` (pure functions) / `_common/tests/test_ingest_db.py` (client-touching)

Move the `captures/*.json` corpus to `_common/tests/captures/` once (not duplicated); factor `conftest.py`'s `load_capture()` into a small shared helper other services' `conftest.py`s import.

Each service's `conftest.py` needs `sys.path` entries for both its own `src/` and `services/` (so `import common` resolves, matching the container's `COPY services/_common/src/ ./common/` layout — same import name already used by `mcp-dev`/`mcp-stats`/`loadtest-fixtures`), and should stub only the env vars its own `common.config.*` imports actually read, not the old full superset, or the split's coupling fix is silently defeated in tests.

**`Makefile`'s `test` target must change** — it currently hardcodes `-c services/webhook/pytest.ini services/webhook/tests`; becomes one `pytest` invocation listing five test paths (`services/webhook/tests services/worker/tests services/reparse/tests services/migrate/tests services/loadtest/tests`), plus `services/_common/tests`.
`migrate`/`reparse`/`reparse-all`/`loadtest`/`build`/`up`/`start` targets reference compose *service names* only and need no change as long as those names are preserved.

## Migration order (keep the stack working at every step)

1. **Extract into `services/_common/src/`, zero behavior change.**
   Move/split code (`fastjson.py`, `queue_client.py`, `ingest_parsing.py`/`ingest_db.py` split out of `clickhouse_ingest.py`, `config/` modules) into `_common/src/` alongside the existing `litellm_auth.py`/`logging_config.py`/`mcp_common.py`.
   `services/webhook/Dockerfile` already `COPY`s `services/_common/src/`, so no Dockerfile change is needed for this step.
   Update all old `services/webhook/src/*.py` imports from `from . import fastjson` / relative imports to `from common.fastjson import ...` style (matching the existing `from common.logging_config import create_logger` convention), delete the old `clickhouse_ingest.py`/`config.py`/`fastjson.py`/`queue_client.py` from `services/webhook/src/`.
   Update `AGENTS.md`'s fastjson-adoption note and `agent_docs/services/webhook.md` to reflect the new location.
   Run `make test` and `make up` — should be unchanged behavior, safe to land alone.
2. **Stand up the five new service directories/Dockerfiles**, modeled on `services/loadtest-fixtures/Dockerfile`'s structure, without touching `docker-compose.yml` yet.
   Build and smoke-test each new image standalone (`docker build -f services/worker/Dockerfile .` + a manual `docker run` against the existing network) before any compose cutover.
3. **Cut over `docker-compose.yml` one service group at a time**, lowest risk first: `clickhouse-migrate` (one-shot job) → `worker` → `webhook-1`/`webhook-2` (recreate one replica at a time, leaning on the load-balancer's two-replica setup so one is always healthy) → `metrics-reparse`/`loadtest` last (both `profiles: [tools]`, not started by default, zero risk to the live stack).
   Add the new `VERSIONS.yml` keys as each block is cut over.
   Only delete `docker-entrypoint.sh` after every old-style block is gone.
4. **Cleanup**: delete `services/webhook/config.yml` (now absorbed into `_common/queue.yml` + `reparse/config.yml`).
   Update `Makefile`'s `test` target, `AGENTS.md`, `agent_docs/architecture.md`, `agent_docs/services/webhook.md` (needs a real rewrite, not just a reference fix — it currently documents the one-image/`APP_ROLE` design in detail), `README.md`, `TODO.md`.
   Update the `.claude/agents/*.md` files that reference `services/webhook/src`, `services/webhook/Dockerfile`, or `APP_ROLE` (`webhook-test-runner.md`, `loadtest-runner.md`, `dynamictext-panel-builder.md`, `sql-expert.md`, `harness-expert.md`, `litellm-test-alerting.md`, and any others found via `grep -rl "APP_ROLE\|docker-entrypoint.sh\|services/webhook/src"`).

## Risks to watch

- **Circular import**: keep `_derive_context` in `ingest_parsing.py` even though it optionally takes a DB `client` — moving it into `ingest_db.py` "for consistency" would create a cycle, since `ingest_db.py` already imports extensively from `ingest_parsing.py`.
- **`worker`'s healthcheck** references `src.queue_client` today — must become `common.queue_client` or it silently starts reporting unhealthy without crashing the container.
- **Build context must stay `.` (repo root)** for all five Dockerfiles, not each service's own directory — every one needs `COPY services/_common/`, and `migrate` also needs `COPY services/clickhouse/migrations/`.
  A well-meaning "simplify the build context" edit later would silently break this.
- **`profiles: [tools]`** on `reparse`/`loadtest` compose blocks must survive the rewrite verbatim, or they'd start running on every plain `docker compose up`.
- **`services/loadtest-fixtures/Dockerfile` and `config.yml` both have comments referencing `APP_ROLE`** ("deliberately not another APP_ROLE sharing services/webhook/Dockerfile's image") — these are contrastive comments about a *different* service, not live dispatch code.
  Update the wording once `webhook`'s `APP_ROLE` mechanism no longer exists, but they're not part of this split's blast radius otherwise.
- Accepted cost: `clickhouse-connect` now installs independently in 4 of the 5 images instead of once — the image-duplication tradeoff the user chose to accept, now consistent with (not an exception to) how `loadtest-fixtures`/`mcp-dev`/`mcp-stats` already work.

## Verification

- `make test` passes after Phase 1 (behavior-preserving) and again after the full split (per-service test paths, once `Makefile`'s `test` target is updated).
- Each new image builds and runs standalone before compose cutover (`docker build -f services/<name>/Dockerfile .` from repo root).
- After cutover:
  - `make status`/`docker compose ps` shows all services healthy
  - `make migrate` re-run is a no-op
  - a live LiteLLM call still lands rows in ClickHouse (`agent_events`/`agent_usage`/`agent_messages`)
  - `worker`'s Prometheus metrics (`:9200`) still increment
  - `make reparse SESSION=<id>` and a short `make loadtest DURATION_MINUTES=1` both still succeed
