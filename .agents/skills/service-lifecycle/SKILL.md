---
name: service-lifecycle
description: >
  Restart-vs-rebuild-vs-recreate decision tree, Makefile service targets, and backup/restore mechanics for this repo's docker-compose stack.
  TRIGGER - read before any restart/rebuild/recreate decision, before running a backup/restore target, and before editing Makefile/docker-compose.yml.
  Owner of the how; dev-ops.md keeps the ownership/authorization judgment.
  v1.0.0
---

## Diagnose first: does `restart` pick this up?

`docker compose restart <service>` reuses the existing image and environment snapshot.
It picks up neither a changed compose `environment:` entry nor a file baked via Dockerfile `COPY` - only rebuild/recreate does.

Check the regime before acting:

- The service's `Dockerfile`: is the changed file `COPY`-baked?
- `docker-compose.yml` + `docker-compose.dev.yml` (when `ENVIRONMENT` != `production`): does a bind mount cover the path, and does the service have a dev override at all?
  Many (e.g. `load-balancer`) don't and always run the baked config, dev and prod alike.

`COPY`-baked or `environment:` change -> `make up SERVICE=<name>`: rebuild + recreate in one (`make start` won't rebuild; a prior `make build` just doubles the build step).
Genuinely bind-mounted in this environment and no `environment:` change -> plain `docker compose restart <service>`; don't rebuild needlessly.

Canonical case: `services/load-balancer/nginx.conf` is `COPY`-baked (`services/load-balancer/Dockerfile`), no dev bind mount - `restart` silently serves the stale config; the fix is `make up SERVICE=load-balancer` alone.

## `load-balancer` routing/latency/error debugging

Not `docker compose logs` alone, not speculation from `nginx.conf`.
Access/error logs flow to Loki (`observability` profile), filterable by `backend`/`stream`: `agent_docs/services/load-balancer.md`, "Access/error logs flow to Loki" - labels plus `infra_overview.json`'s "Load balancer" tab ("Access log"/"Error log" sub-tabs).

## `build`/`start`/`up`/`up-no-deps`: four tools, not one habit

| Target                       | Effect                                                                              | Use when                                                              |
|-------------------------------|--------------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| `make build SERVICE=x`        | image only, running container untouched                                              | explicitly asked to just build                                          |
| `make start SERVICE=x`        | `up -d` with the existing image, no rebuild, no force-recreate                       | resume an already-correct state                                         |
| `make up SERVICE=x`           | `up -d --build --force-recreate`, full `depends_on` chain                            | baked config/env/Dockerfile change (diagnosis above)                    |
| `make up-no-deps SERVICE=x`   | same rebuild+recreate with `--no-deps`, exactly the named service(s)                  | targeted change when dependencies are already up, healthy, and unchanged |

`SERVICE` takes space-separated names for `up-no-deps` (`SERVICE="webhook-1 webhook-2 worker"`).
`up-no-deps` doesn't health-check dependencies; use only when they're already known healthy.
Chain itself needs recreating, or a whole-stack rebuild -> plain `make up`.
Nothing baked changed and the goal is just bringing a container up -> `make start`; `make up` there is a wasted rebuild+recreate cycle.

## Profile stacks (Langfuse/observability)

Opt-in profiles have only this shape - no `-build`/`-start` split, `-up` always does `up -d --build --force-recreate`:

- `langfuse-up`/`langfuse-down`/`langfuse-logs`
- `observability-up`/`observability-down`/`observability-logs`/`observability-status`

## One-off recreate with env overrides

`<OVERRIDE_VARS> docker compose <COMPOSE_FILES> up -d --build --force-recreate --no-deps <service...>`, with any env vars to override set inline before the command.

- Not `make up SERVICE=...`: the `Makefile`'s `include .env` + `unexport $(ENV_VARS)` fights shell-level overrides also defined in `.env`; with plain `docker compose`, shell env wins.
- `--no-deps` applies in both directions (overrides on, and the restore back to `.env` values); without it the command cascades into recreating dependency services too.
  Safe only when the dependencies are already confirmed healthy beforehand - `--no-deps` doesn't health-check them.

## Running it

- Always `make build`/`make start`/`make up` (scoped via `SERVICE=<name>`), never raw `docker compose` - the `Makefile` resolves image tags from `VERSIONS.yml`; a raw call leaves a stray untracked image version.
  The one-off env-override recreate above is the sole sanctioned exception.
- Scope to the single named service; no whole-stack rebuilds on your own initiative.

## Verify before reporting done

- Go/no-go is always `make status`, never `docker compose ps`/log-grepping judgment.
  It polls every service to `healthy` (one-shots like `clickhouse-migrate`: exit 0), fails fast with that service's `docker compose logs --tail 80`, times out at 180s.
  Don't report done until it exits 0.
- On failure, diagnose from the logs it printed; `docker compose logs <service>` only if 80 lines isn't enough.
  Manual log inspection finds causes - it never decides pass/fail.
- Re-checks after corrective action: space `make status` runs ~5s apart (its internal 1s poll already covers fast-changing state within one run).
- A green stack doesn't prove the change took effect - verify content too:
  - config file: `docker exec <container> grep <marker> <path-inside-container>` (or `cat`/`diff` vs source)
  - env var: `docker exec <container> env | grep <VAR>`, or `docker compose config <service> | grep <VAR>`
- A one-off tool-container target (`docker compose run --rm <container>` under the hood - e.g. `make reparse-dlq`, `make loadtest-fixtures`) can stream thousands of unbounded progress lines.
  Launch it with `run_in_background: true` (or redirect to a file); never read the raw stream into context.
  Inspect via a targeted `grep -Ei 'error|complete|exit_code|traceback|warn'` against the captured output, not a full read.
  Report only the outcome - counts, success/fail, the final summary line.
- Report the outcome only: what changed, which command (`restart` vs rebuild+up) and why, `make status` result, content verification result.
  Never paste `make status`'s table verbatim - filter to the final `Healthy`/`Failed` line, failed service name(s), and its failure log excerpt.

## Editing `Makefile` / `docker-compose.yml`

- Read the whole file first; keep `check-env`/`COMPOSE_FILES`/`VERSIONS.yml` resolution (`Makefile`) and the static-IP/`mem_limit`/profile conventions (`docker-compose.yml`) intact.
- Verify before done: `make <target> --dry-run` or a safe real run; a new/changed service comes up healthy via `make status`.
- Target added/removed/renamed, or required args changed -> update README.md's "Make targets" table (under "## Reference") in the same change, not a follow-up.

## Backup & restore (`make backup-*`/`restore-*`)

Covers the three non-reproducible stores - `clickhouse`, `litellm-db`, `grafana`'s `grafana.db` - via the `backup` tools-profile service.

Mechanics: the `backup` service (`docker-compose.yml`) never gets `docker exec`/socket access; `clickhouse`/`litellm-db` are reached over the `receipt-goblin` network, `grafana-data` is mounted as the same named volume `grafana` uses.
Files land under `$BACKUP_DIR` (`.env`, default `.backups/`): `clickhouse/`, `litellm/`, `grafana/`.
One-time setup: ClickHouse's BACKUP/RESTORE disk (`services/clickhouse/config.d/backups.xml`) and the `$BACKUP_DIR/clickhouse` bind mount take effect only after `docker compose up -d --build clickhouse` - a brief `clickhouse` restart, so pick a quiet moment (Grafana panels gap while it's down).

Backup - always safe against a live stack, no service stopped:

- `make backup-clickhouse` - `BACKUP DATABASE` via `clickhouse-client`.
- `make backup-litellm` - `pg_dump` against `litellm-db`.
- `make backup-grafana` - `sqlite3 .backup` against `grafana.db`.
- `make backup-all` - all three; the only target cron ever calls.

Restore - always destructive (drops/overwrites the live target), manual and explicit-only; list files first (`ls .backups/<store>/`, or under `$BACKUP_DIR`):

- ClickHouse: safe with `clickhouse` up (mid-flight queries fail, nothing corrupts) - `make restore-clickhouse FILE=<filename>`.
- LiteLLM: `docker compose stop litellm` first (it writes continuously; `litellm-db` itself stays up), `make restore-litellm FILE=<filename>`, then `docker compose start litellm`.
- Grafana: `docker compose stop grafana`, `make restore-grafana FILE=<filename>`, then `docker compose start grafana`.
- The `backup` container can't stop/start siblings - do the stop/start manually.
- After any restore: `make status` green before reporting done.

Cron: `0 3 * * * cd /path/to/receipt-goblin && make backup-all >> .backups/cron.log 2>&1` - cron's PATH is sparse, use absolute paths or source the shell profile.
Never point cron at a `restore-*` target.
No retention/pruning: `.backups/` accumulates until removed by hand - deliberate; don't add cleanup unasked.

Before a major ClickHouse change, take `make backup-clickhouse` (or the covering `backup-*` target) first:

- a migration under `services/clickhouse/migrations/` with a BACKFILL or an engine/rename change (`clickhouse-migration` skill for the migration itself)
- re-applying `services/clickhouse/schema.sql` by hand against an already-initialized volume
- truncating `agent_events`/`agent_invocations`/`agent_messages`/`agent_usage`/etc. against the real database - not the dedicated `loadtest` one ("Running the load test" in `AGENTS.md`)
- any manual `ALTER`/`DELETE`/data-surgery query against ClickHouse

If it goes wrong: restore from that backup, don't hand-patch the damage.
