---
name: loadtest-workflow
description: >
  Phase 0-6 runbook for `make loadtest`: exact fixture-freshness/pre-flight/launch/monitoring/post-run/restore commands, thresholds, and stop conditions.
  TRIGGER - read before Phase 1 of any load-test run; owner of every exact command and numeric threshold `loadtest-runner.md` references.
  SKIP for phase sequencing, delegation targets, and stop-condition judgment - those stay in loadtest-runner.md.
  v1.0.0
---

## Phase 1 - fixture freshness & pre-flight questions

- `make loadtest-fixtures-status` prints `/app/loadtest_fixtures/manifest.json` from the volume, no ClickHouse touch.
  Parse `generated_at`, `volume`, `event_count`.
- TTL: `fixtures_ttl_hours` in `services/loadtest-fixtures/config.yml` (default 168, loaded as `FIXTURES_TTL_HOURS`).
- Needed volume: whatever the user specified, else `build_fixtures.py`'s default `medium`.
- No manifest: nothing to replay - ask to generate via `make loadtest-fixtures VOLUME=<small|medium|large>`.
- Stale (`now - generated_at` > TTL) or volume-mismatched: ask to regenerate or proceed anyway.
  On "proceed", warn in the Phase 2 checkpoint and again in the Phase 6 report.
- Fresh and matched: proceed, noting `event_count`/`volume`/`generated_at` in the status update.

litellm/litellm-db question: ask to shut down first if `docker ps` shows them up.
They share the host with everything being measured, and load tests never use them (`loadtest.py` hits `webhook`/`load-balancer` directly, bypassing LiteLLM).
Skip the question only if they aren't running at all.

Default load parameters, when the user didn't specify: `START_USERS=10 END_USERS=100 RAMP_STEPS=8 RAMP_STEP_MINUTES=0.25 HOLD_MINUTES=30 SPEED=5` (10->100 users over 2 min, hold 30 min at 5x, ~32 min total).

## Phase 2 - pre-flight commands

- Stack health: `docker ps` - `webhook-1`/`webhook-2`/`webhook-worker`/`redis`/`clickhouse`/`load-balancer` all `Up ... (healthy)`.
  Anything less: stop and report rather than launching into a broken stack.
- Dedicated loadtest database, unconditional every run:
  1. Resolve `CLICKHOUSE_LOADTEST_DATABASE` from `.env` (default `loadtest` if unset/commented), plus `CLICKHOUSE_LOADTEST_USER`/`CLICKHOUSE_LOADTEST_PASSWORD`.
     The dedicated `loadtest` role already has `GRANT ALL` on `<db>` from `make init` (`services/init/config.yml`) - no grant step needed.
     Never grant `ingest` on `<db>` as a substitute - that widens `ingest` beyond the real app database it's deliberately scoped to.
  2. `docker exec receipt-goblin-clickhouse clickhouse-client -q "SELECT count() FROM system.tables WHERE database = '<db>'"`.
  3. Absent/zero tables: `CREATE DATABASE IF NOT EXISTS <db>`, then apply the baked schema via `clickhouse-client --database <db> --multiquery < /docker-entrypoint-initdb.d/schema.sql` (the file already lives inside the container).
     Exists with tables: say so plainly (name + table count), skip creation.
  4. Checkpoint: the database state, before the more disruptive recreate in step 5.
  5. Delegate the recreate to `dev-ops` - never run `docker compose`/`make up` directly: exactly `webhook-1`, `webhook-2`, `webhook-worker`, with all three overrides `CLICKHOUSE_DATABASE=<db>`, `CLICKHOUSE_USER=<...>`, `CLICKHOUSE_PASSWORD=<...>` (resolved in step 1).
     A database-only override leaves the containers authenticating as `ingest` with zero grants on `<db>` - every insert fails `ACCESS_DENIED` while the run itself reports 200s (webhook's request path never touches ClickHouse) - always all three together.
  6. Truncate every table in `<db>`: `agent_events`, `agent_invocations`, `agent_messages`, `agent_usage`, `ai_gateway_groups`, `ai_gateway_users`, `ingest_dlq`, `ingest_raw`, `plan_proposals`, `session_git_branch` - never `schema_migrations` - via `clickhouse-client -q "TRUNCATE TABLE <db>.<table>"` each.
     Unconditional, no question: `<db>` never holds real data.
- If shutdown was requested: `docker stop receipt-goblin-litellm receipt-goblin-litellm-db`.
- Baseline counts: `<db>.agent_events`/`<db>.ingest_raw`, always zero right after the truncate - record anyway for the explicit before/after pairing in Phase 5.

## Phase 3 - launch commands

`make loadtest <VARS...>` via Bash with `run_in_background: true` - the run takes minutes, never block on it synchronously.
Resolve the generator container's name after launch, not before: `docker compose run` names it `receipt-goblin-webhook-loadtest-run-<hash>`, so `docker ps --format '{{.Names}}' | grep loadtest`.

Three standing Grafana links, each its own line, complete and verbatim - never a bare `/d/...` fragment:

- `http://localhost:3000/d/clickhouse-health` - ClickHouse health/query performance.
- `http://localhost:3000/d/docker-containers` - per-container CPU/memory/resource usage.
- `http://localhost:3000/d/infra-overview` - general infra overview.

## Phase 4 - monitoring commands & thresholds

Sample every 20-30s, never wait silently:

- `docker stats --no-stream` on `webhook-1`, `webhook-2`, `webhook-worker`, `redis`, `clickhouse`, and the generator container.
  Generator memory must stay flat, ~40-100MB across the whole run (`loadtest.py`'s `posix_fadvise(DONTNEED)` fix keeps the read-only fixtures volume's page-cache reads from accumulating there).
- Prometheus via `docker exec receipt-goblin-prometheus wget -qO- 'http://localhost:9090/api/v1/query?query=...'`: `worker_stream_depth`, `worker_pending_count`, and p50/p99 webhook latency via `histogram_quantile(0.5|0.99, rate(http_request_duration_seconds_bucket{job="webhook",handler="/api/v1/metrics"}[1m]))`.

Stop-condition threshold reference (the decision to stop stays in loadtest-runner.md):

- Generator memory outside ~40-100MB, climbing, OOM, or non-zero exit - a regression in the page-cache fix, not a resource-sizing problem, needing a code-level fix in `loadtest.py`'s `_read_bytes` (the `posix_fadvise(DONTNEED)` call).
- Stack stopped/restarted mid-run, by anyone - the run is contaminated.

Checkpoint trigger points: every ramp-step transition (the generator logs `ramp: now at N/M users`), reaching the hold phase, and anything urgent the moment it's seen (OOM, a container going unhealthy, error rate off zero, Redis/ClickHouse looking saturated).

## Phase 5 - post-run verification commands

- Read the generator's final report: requests sent, status-code breakdown, error rate, latency p50/p90/p99/max.
- Re-check `<db>.agent_events`/`<db>.ingest_raw` via `docker exec receipt-goblin-clickhouse clickhouse-client --user "$CLICKHOUSE_LOADTEST_USER" --password "$CLICKHOUSE_LOADTEST_PASSWORD" -q "SELECT count() FROM <db>.<table>"` - never `mcp__dev__query`, whose `mcp` role has `SELECT` only on the real `CLICKHOUSE_DATABASE`, never `<db>` (`services/init/config.yml`).
  Compare against the zero baseline - a count still at zero after the run is the real bug signal (no dedup false alarm: `<db>` never has pre-existing overlapping session data).

## Phase 6 - restore commands & report fields

Restore the write path: delegate to `dev-ops` again, same shape as Phase 2 step 5, the exact 3 services, all three overrides unset (back to `.env`'s normal `ingest`-role values, never just the database), confirmation required before proceeding.

Report fields, in the language the user used to invoke:

- Result: requests sent, error rate, latency percentiles.
- ClickHouse: before/after row counts, whether data landed as expected.
- Bottlenecks: CPU/memory peaks per container from the samples - name whichever service(s) got close to their limits, don't dump raw numbers.
- Redis: peak memory vs its current `mem_limit` read from `docker-compose.yml` at run time (never a number remembered from a previous run - limits change), and whether it's adequately sized.
- Suggestions: concrete `docker-compose.yml` `mem_limit`/resource changes only where the data supports them.
- Regression callout: if the OOM/page-cache issue recurred, lead with it.
- The same three Grafana links, same one-per-line verbatim-URL formatting as Phase 3.
