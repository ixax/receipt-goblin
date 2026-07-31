# `clickhouse` + `init`

Storage for every table this stack writes (`agent_events`/`agent_usage`/`agent_messages`/`agent_invocations`/`ingest_raw`/etc).
Folds in `services/init/` too - role/user provisioning is tightly coupled to this schema and has no other home.

## Image & version pin

`services/clickhouse/Dockerfile` bakes `schema.sql`, `config.d/*.xml`, and `users.d/*.xml` in at build time - no config bind-mounts, dev or prod.
Pinned to `CLICKHOUSE_VERSION` in root `VERSIONS.yml` (`24.8.14.39` as of writing) - single source for both this image's `FROM` and `services/backup/Dockerfile`'s apt-installed `clickhouse-client`, so server/client versions can't drift apart.
Same `VERSIONS.yml` feeds `make build`/`make up`'s image-tag resolution (`scripts/resolve_image_version.py`) - never `docker compose build`/`up` directly.

## `schema.sql`

DDL for every table.
Auto-applies via `docker-entrypoint-initdb.d` only on first container start against an empty data volume.
A schema change on a running stack needs a migration instead (`services/clickhouse/migrations/*.sql`, applied via `make migrate` / `clickhouse-migrate` role - see `.claude/skills/clickhouse-migration/SKILL.md`), not an edit to `schema.sql` itself.

## `config.d/*.xml` (server-level)

- `memory.xml` - caps ClickHouse memory at 85% of its `mem_limit`.
- `tuning.xml` - cache/merge_tree resource tuning (mark/uncompressed cache sizing, part-loading threads, mutation/optimize pool thresholds), sized for the container's `mem_limit`/CPU budget.
- `backups.xml` - registers the `backups` local disk (`/backups/clickhouse/`, bind-mounted to `$BACKUP_DIR/clickhouse` on the host) that native `BACKUP`/`RESTORE` statements write to, used by `services/backup/scripts/backup_clickhouse.sh`/`restore_clickhouse.sh` (see `.claude/agents/dev-ops.md` for the playbook).
- `prometheus.xml` - exposes `/metrics` on port 9363, scraped by Prometheus's `clickhouse` job over the internal Docker network only.
- `logging.xml` - enables `query_log`/`crash_log`/`asynchronous_metric_log`/`metric_log` system tables, needed by `services/grafana/dashboards-health/clickhouse.json`.

## `users.d/tuning.xml` (per-user)

Per-`default`-profile tuning (thread/pool sizing, execution timeouts, external sort/group-by spill thresholds).
Must live under `users.d`, not `config.d` - ClickHouse merges the two into `config.xml`/`users.xml` separately, and profile settings only take effect from `users.d`.

## `services/init/` - role/user provisioning (`make init`)

- `init_clickhouse_users.py` - interactive, stdlib-only first-run script (`make init`), the *only* place ClickHouse users/roles/grants get created (not on every `docker compose up` - see `services/migrate/src/migrate.py`'s own docstring for why that changed).
  Asks every question first (database name, bootstrap creds, then per role), writes `.env` once at the end, brings up `clickhouse` alone, issues every `CREATE USER`/`GRANT` via `docker compose exec clickhouse clickhouse-client`, then stops `clickhouse` again.
  Idempotent - safe to re-run; existing usernames/passwords already in `.env` are reused, not regenerated.
- `ch_roles.py` - loads `config.yml`'s role/grant definitions via a deliberately restricted (not general-YAML) parser: one top-level `roles:` list, flat scalar fields plus one nested `grants:` list.
  Extend the parser deliberately if `config.yml` ever needs more than that shape.
- `config.yml` - the role/grant definitions themselves: `ingest`, `grafana`, `mcp` (unrestricted read, used by `mcp-dev`), `mcp_stats` (narrow `agent_usage`/`agent_events` read, used by `mcp-stats`), `backup`, `loadtest` (its own database, `CREATE DATABASE` + `GRANT ALL`).
- `scripts/create_user.sh` - separate, ad-hoc interactive tool (`docker compose exec clickhouse /scripts/create_user.sh`) for provisioning a one-off user outside the `init_clickhouse_users.py` role set.
  Prints every statement and asks y/N confirmation before running anything.

## Tables (`schema.sql`)

One line each: purpose, plus partitioning if it has one.
The half-year `PARTITION BY` convention itself is documented in `AGENTS.md`'s Coding Anti-Patterns ("no TTL-based auto-delete") - not restated here, only which tables use it.

- `agent_invocations` - `agent_id` -> `subagent_type` lookup, recovered from an orchestrator's Agent tool_use/tool_result pair. Tiny, no partitioning.
- `session_git_branch` - one row per session's git branch/repo, captured once at `SessionStart`. `issue_id` is a ticket key parsed out of the branch name. No partitioning.
- `plan_proposals` - one row per `ExitPlanMode` call (Claude Code only), captured by a `PreToolUse` hook since the plan text isn't recoverable from LiteLLM's payload. Insert-only, no partitioning.
- `ai_gateway_groups` - `group_id` -> `group_name` (LiteLLM Team) lookup, latest-name-wins. No partitioning.
- `ai_gateway_users` - `user_id` -> (`group_id`, `user_name`, `user_agent`) lookup, latest-wins. No partitioning.
- `clients` - one row per distinct calling-client user-agent string, `id = cityHash64(value)`. No partitioning.
- `agent_events` - one row per lifecycle event (hook/tool call), the main trace table. **Half-year `PARTITION BY`.**
- `agent_usage` - one row per model call (tokens/cost/provider). **Half-year `PARTITION BY`.**
- `agent_messages` - one row per turn, holding prompt/response text. **Half-year `PARTITION BY`.**
- `ingest_raw` - full untouched original payload per call, write-once, ZSTD(3)-compressed, the source for reparsing. **Half-year `PARTITION BY`.**
- `ingest_dlq` - dead-letter table for rows a table's `insert()` rejects; triage feed, not a source of truth. **Half-year `PARTITION BY`** (moved off a 30-day TTL by migration 011).
- `litellm_alerts` - LiteLLM's native alerting webhook events (budget/spend crossings, outages, DB exceptions), raw-payload-preserving since only the budget-event shape is fully documented. **Half-year `PARTITION BY`.**

## Migrations (`services/clickhouse/migrations/`)

Applied by the `clickhouse-migrate` service (`services/migrate/src/migrate.py`, its own image built from `services/migrate/Dockerfile`) against `services/clickhouse/migrations/*.sql`, in order, via `make migrate` - explicit-only, never automatic on `up`.
See `.claude/skills/clickhouse-migration/SKILL.md` before creating/editing a migration file - this section is what exists, not how to add to it.
`009`/`010` don't exist in the sequence - no evidence in the migration files themselves of why; a factual gap, not an invented explanation.

- `001` - one-time recreate+swap of `agent_events`/`agent_usage`/`agent_messages` onto `ReplacingMergeTree(ingested_at)` + `litellm_call_id` in `ORDER BY`; adds `event_sources` (now `ingest_raw`). Manual only.
- `002` - adds `ai_gateway_groups`/`ai_gateway_users` dimension tables; drops the old `group_alias` column. Manual only, backfill via `make reparse-all`.
- `003` - adds `user_key_hash` (virtual-key identity) and `user_agent` columns. Manual only, backfill via `make reparse-all`.
- `004` - adds `session_git_branch.issue_id`, backfilled in-SQL via `extract()` since this table has no `ingest_raw` counterpart to reparse from. Manual only.
- `005` - adds `ingest_failures` (now `ingest_dlq`), originally with a 30-day TTL. Auto-applied.
- `006` - lowers `event_sources.raw_payload_full` codec from `ZSTD(19)` to `ZSTD(3)`, fixing the ingest bottleneck that codec caused under load. Auto-applied.
- `007` - pure rename: `event_sources` -> `ingest_raw`, `ingest_failures` -> `ingest_dlq`. Auto-applied.
- `008` - adds `session_git_branch.session_id_hash` (`MATERIALIZED cityHash64(session_id)`) backing `session_git_branch_dict`, replacing a per-panel `GROUP BY` scan. Auto-applied.
- `011` - moves `ingest_dlq` off its 30-day TTL onto the half-year `PARTITION BY` convention via recreate+swap. Auto-applied.
- `012` - adds `litellm_alerts`. Auto-applied.
