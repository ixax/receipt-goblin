# Split `services/webhook/` into independent services

## Context

`services/webhook/src/` is a monolith (`server.py`, `worker.py`, `queue_client.py`,
`clickhouse_ingest.py` at 1552 lines, `loadtest.py`, `reparse.py`, `migrate.py`,
`config.py`, `fastjson.py`) sharing one `Dockerfile`/`requirements.txt`, with
`APP_ROLE` picking the runtime role.
A prior TODO note considered splitting this
but rejected it, to avoid duplicating a ~214MB image across 6 roles.

Decision for this refactor: do the split anyway, accepting that image-duplication
cost, because the current single `config.py` forces every role to set every env
var it doesn't use (e.g. `clickhouse-migrate` needs `REDIS_HOST/PORT` purely so
`config.py` doesn't crash on import), and `clickhouse_ingest.py` mixes pure
parsing with ClickHouse I/O with no clean boundary — `reparse.py` today reaches
into ~10 of its private (`_`-prefixed) helpers directly, an undeclared internal API.

Target: five independent services (`webhook`, `worker`, `reparse`, `migrate`,
`loadtest`), each with its own Dockerfile/requirements.txt/image tag, pulling
genuinely shared code (parsing, ClickHouse I/O, Redis Streams protocol,
config-reading, JSON) from one new shared source directory that every
Dockerfile `COPY`s in at build time — mirroring the existing precedent of
`services/webhook/Dockerfile` already COPYing `services/clickhouse/migrations/`
cross-directory.
No shared-Python-library convention exists in the repo yet
(checked: no `services/common/` or similar) — this becomes the first.

## Target layout

```
services/
  _shared/                        # copied into every image as ./shared at build time
    fastjson.py                   # moved verbatim
    queue_client.py                # moved verbatim (webhook=producer, worker=consumer; one protocol, can't drift)
    ingest_parsing.py              # NEW: pure/DB-free half of clickhouse_ingest.py
    ingest_db.py                   # NEW: ClickHouse-I/O half of clickhouse_ingest.py
    queue.yml                      # queue-mechanics half of today's config.yml
    config/
      clickhouse.py                 # HOST/PORT/DATABASE
      clickhouse_credentials.py     # USER/PASSWORD (ingest role)
      redis.py                      # HOST/PORT
      litellm.py                    # MASTER_KEY/BASE_URL
      capture.py                    # CAPTURE_DIR/CAPTURE_ENABLED
      queue.py                      # STREAM_KEY/CONSUMER_GROUP/MAXLEN/BATCH_SIZE/FLUSH_INTERVAL_MS/STALE_IDLE_MS (reads queue.yml)
    tests/                        # test_fastjson.py, test_queue_client.py, test_ingest_parsing.py,
                                   # test_ingest_db.py (split from today's test_clickhouse_ingest.py),
                                   # captures/*.json (moved corpus, not duplicated)

  webhook/            # trimmed to just the FastAPI app
    Dockerfile requirements.txt src/server.py tests/

  worker/             # new
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
    (still COPYs services/clickhouse/migrations/)

  loadtest/           # new
    Dockerfile requirements.txt
    src/loadtest.py
    tests/test_loadtest.py
```

## `clickhouse_ingest.py` split (the core of this refactor)

- **`ingest_parsing.py`** (pure, DB-free): all regex constants, `_to_dt`,
  `_flatten_content`, `_last_user_text`, `_codex_*` helpers, `EventContext`,
  `_derive_context` (takes an optional `client` but never imports `ingest_db`,
  keeping the dependency arrow one-directional), `_classify_event`,
  `_agent_invocations_from_messages`, all `_*_row` builders, `_source_row`,
  serialize/deserialize helpers, `_issue_id_from_branch`, and
  **`build_event(payload)`** (worker's only entry point). Also promote
  `_session_and_trace_id` → **`session_and_trace_id`** (public) since
  `server.py` already reaches into it today for capture-file naming — same
  wart as reparse's, just smaller.
- **`ingest_db.py`** (ClickHouse I/O): `get_client()`, `clickhouse_alive()`,
  all `_insert_*`, `_BatchWriter`, `ingest_events_batch()`, `ingest_git_branch()`,
  `ingest_plan_proposal()`.
  Imports specific names from `ingest_parsing`.
  **New public function `reparse_event(client, payload, litellm_call_id,
  source_session_id, now)`** — formalizes `reparse.py`'s current 10-private-helper
  reach-through into one real API call. `reparse.py` shrinks to: decode
  `raw_payload_full`, call `reparse_event(...)`, keep its existing try/except-log
  wrapper.
- `ingest_standard_logging_payload`/`ingest_webhook_body` have zero call sites
  today (leftover from before the Redis-queue split) — carry them into
  `ingest_db.py` unchanged for parity, flag as follow-up cleanup candidates.

## `config.py` split

Per-concern modules under `_shared/config/` (`clickhouse.py`,
`clickhouse_credentials.py`, `redis.py`, `litellm.py`, `capture.py`, `queue.py`),
each doing the same unconditional `os.environ[...]` reads as today, no new
defaults.
Service-local config (not shared): `worker`'s `WORKER_METRICS_PORT`
constant, `reparse`'s `REPARSE_CHUNK_SIZE` (own `config.yml`), `migrate`'s
bootstrap/ingest creds (already inline `os.environ` reads today, unaffected).

Result: `migrate` drops `LITELLM_*`/`REDIS_*`/`CLICKHOUSE_USER/PASSWORD`;
`reparse` drops `LITELLM_*`/`REDIS_*`; `loadtest` drops `CLICKHOUSE_*`/`REDIS_*`/
`LITELLM_*` entirely; `worker` drops `LITELLM_*`.
This is the actual coupling fix.

## Dockerfiles / requirements.txt

Every one of the five needs `context: .` (repo root) to `COPY services/_shared/
./shared/` — same cross-directory-COPY pattern the current webhook Dockerfile
already uses for `services/clickhouse/migrations/`.
Per-service deps, reusing
the role-specific comments already in today's `requirements.txt`:

| Service | Keeps | Drops (vs. today's shared superset) |
|---|---|---|
| webhook | fastapi, uvicorn, prometheus-fastapi-instrumentator, clickhouse-connect, redis, PyYAML, orjson | prometheus-client, aiohttp |
| worker | clickhouse-connect, redis, PyYAML, prometheus-client, orjson | fastapi, uvicorn, prometheus-fastapi-instrumentator, aiohttp |
| reparse | clickhouse-connect, PyYAML | fastapi/uvicorn stack, redis, aiohttp, orjson (reparse uses stdlib json today) |
| migrate | clickhouse-connect | everything else |
| loadtest | aiohttp | everything else (doesn't even need `shared/` — its one `CAPTURE_DIR` read can stay a local one-line config, skip `COPY services/_shared/` for this service) |

`docker-entrypoint.sh` and its `APP_ROLE` case-dispatch are deleted entirely —
each Dockerfile gets a direct `CMD` (e.g. worker: `CMD ["python", "-m",
"src.worker"]`); `docker-compose.dev.yml`'s `--reload` override for webhook
still works unmodified against a bare `CMD`.

## `docker-compose.yml` / `VERSION.yml` / `Makefile`

- Six existing blocks map onto five new Dockerfiles: `webhook-1`/`webhook-2` →
  `services/webhook/Dockerfile`; `worker` → `services/worker/Dockerfile`;
  `metrics-reparse` → `services/reparse/Dockerfile` (consider renaming the
  compose service to `reparse` — touches `Makefile`'s `reparse`/`reparse-all`
  targets); `clickhouse-migrate` → `services/migrate/Dockerfile`; `loadtest` →
  `services/loadtest/Dockerfile`.
- `VERSION.yml`: replace single `WEBHOOK_TAG` with `WEBHOOK_TAG`/`WORKER_TAG`/
  `REPARSE_TAG`/`MIGRATE_TAG`/`LOADTEST_TAG`. `scripts/resolve_image_version.py`
  needs no code change (already generic over keys).
- Trim each service's `environment:` block per the config split above —
  biggest wins are `clickhouse-migrate` (drops 6 vars + 2 explanatory comments)
  and `loadtest` (drops its whole ClickHouse-creds-it-doesn't-use block).
- `ports`/`healthcheck`/`depends_on`/`networks`/`profiles`/static IPs
  (`172.28.1.11`/`.12` for webhook-1/2) are unaffected — don't touch.
- `docker-compose.dev.yml`'s bind mounts need updating to each service's own
  `src/` dir, plus adding `./services/_shared:/app/shared:ro` wherever a
  service currently bind-mounts `src/`, so shared-code edits don't need a rebuild in dev.

## Tests

Mirror the source split: `worker/tests/test_worker.py`, `loadtest/tests/test_loadtest.py`
(both moved verbatim), `reparse/tests/test_reparse.py` (new, covers `reparse_event()`),
`_shared/tests/` gets `test_fastjson.py`, `test_queue_client.py`, and today's
860-line `test_clickhouse_ingest.py` split into `test_ingest_parsing.py` (pure
functions) / `test_ingest_db.py` (client-touching).
Move the `captures/*.json`
corpus to `_shared/tests/captures/` once (not duplicated); factor
`conftest.py`'s `load_capture()` into a small shared helper other services'
`conftest.py`s import.
Each service's `conftest.py` needs `sys.path` entries for
both its own `src/` and `services/` (so `import shared` resolves, matching the
container's `COPY services/_shared/ ./shared/` layout) — and should stub only
the env vars its own `shared.config.*` imports actually read, not the old
full superset, or the split's coupling fix is silently defeated in tests.
`Makefile`'s `test` target becomes one `pytest` invocation listing five test
paths instead of one.

## Migration order (keep the stack working at every step)

1. **Extract `services/_shared/`, zero behavior change.** Move/split code into
   `_shared/`, update `services/webhook/Dockerfile` (still the only Dockerfile,
   still building all 6 old compose services via `APP_ROLE`) to also `COPY
   services/_shared/ ./shared/`, update all old `src/*.py` imports to
   `from shared...`, delete the old `clickhouse_ingest.py`/`config.py`/
   `fastjson.py`/`queue_client.py` from `services/webhook/src/`.
   Run `make test`
   and `make up` — should be unchanged behavior, safe to land alone.
2. **Stand up the five new service directories/Dockerfiles** without touching
   `docker-compose.yml` yet.
   Build and smoke-test each new image standalone
   (`docker build -f services/worker/Dockerfile .` + a manual `docker run`
   against the existing network) before any compose cutover.
3. **Cut over `docker-compose.yml` one service group at a time**, lowest risk
   first: `clickhouse-migrate` (one-shot job) → `worker` → `webhook-1`/`webhook-2`
   (recreate one replica at a time, leaning on the load-balancer's two-replica
   setup so one is always healthy) → `metrics-reparse`/`loadtest` last (both
   `profiles: [tools]`, not started by default, zero risk to the live stack).
   Add the new `VERSION.yml` keys as each block is cut over.
   Only delete
   `docker-entrypoint.sh` after every old-style block is gone.
4. **Cleanup**: delete `services/webhook/config.yml` (now absorbed into
   `_shared/queue.yml` + `reparse/config.yml`), update `AGENTS.md`, `README.md`,
   `TODO.md`/`todo/*.md`, and the `.claude/agents/*.md` files that reference
   `services/webhook/src/`, `services/webhook/Dockerfile`, or `APP_ROLE`
   (`webhook-test-runner.md`, `loadtest-runner.md`, `dynamictext-panel-builder.md`,
   and any others found via `grep -rl "APP_ROLE\|docker-entrypoint.sh\|services/webhook/src"`).

## Risks to watch

- **Circular import**: keep `_derive_context` in `ingest_parsing.py` even though
  it optionally takes a DB `client` — moving it into `ingest_db.py` "for
  consistency" would create a cycle, since `ingest_db.py` already imports
  extensively from `ingest_parsing.py`.
- **`worker`'s healthcheck** references `src.queue_client` today — must become
  `shared.queue_client` or it silently starts reporting unhealthy without
  crashing the container.
- **Build context must stay `.` (repo root)** for all five Dockerfiles, not each
  service's own directory — every one needs `COPY services/_shared/`, and
  `migrate` also needs `COPY services/clickhouse/migrations/`.
  A well-meaning
  "simplify the build context" edit later would silently break this.
- **`profiles: [tools]`** on `reparse`/`loadtest` compose blocks must survive
  the rewrite verbatim, or they'd start running on every plain `docker compose up`.
- Accepted cost: `clickhouse-connect` now installs independently in 4 of the 5
  images instead of once — the image-duplication tradeoff the user chose to accept.

## Verification

- `make test` passes after Phase 1 (behavior-preserving) and again after the
  full split (per-service test paths).
- Each new image builds and runs standalone before compose cutover
  (`docker build -f services/<name>/Dockerfile .` from repo root).
- After cutover: `make status`/`docker compose ps` shows all services healthy;
  `make migrate` re-run is a no-op; a live LiteLLM call still lands rows in
  ClickHouse (`agent_events`/`agent_usage`/`agent_messages`); `worker`'s
  Prometheus metrics (`:9200`) still increment; `make reparse SESSION=<id>`
  and a short `make loadtest DURATION_MINUTES=1` both still succeed.
