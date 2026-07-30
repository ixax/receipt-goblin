# Agent Tracking Stack

## Project Overview & Tech Stack

**Agent Tracking Stack** - tracks cost/efficiency of AI coding agents (Claude Code, Codex CLI), full call-chain tracing.
Queue-based ingest: LiteLLM -> `webhook` (enqueue-only) -> `redis` -> `webhook-worker` (batches into ClickHouse); Grafana reads ClickHouse; CLI reads back via `mcp-dev`/`mcp-stats`.
Dev/prod split: `agent_docs/architecture.md`; per-service detail: `agent_docs/services/<name>.md` (Project Structure below).

Write agent instructions, `.md` files, and comments in ASD-STE100 Simplified Technical English - rules: `md-format` skill.

## Essential Commands

### Setup & Run

- `make init` - first-run ClickHouse role provisioning.
- `make start` / `make up [SERVICE=x]` - bring up (existing / rebuild+recreate).
- `make migrate` - apply ClickHouse migrations, explicit-only.
- `make status` - wait until healthy.
- `make stop` / `make down` - tear down.
- `make setup-client` - print CLI-proxy shell/config snippets.

Single-service rebuild/restart after a config/env change routes to `dev-ops`, not inline (Agent & Skill Routing).

### Testing & Quality Assurance

- `make test` - webhook pytest suite; always via `webhook-test-runner`, never inline.
- `make test-harness-audit` - budget-audit unit tests.
- `make loadtest` - ramping load test; always via `loadtest-runner`, never inline.
- `make loadtest-fixtures [VOLUME=small|medium|large]` - build fixtures from ClickHouse.

### Harness maintenance

- `make harness-index` - regenerate `agent_docs/harness-index.md` after any frontmatter change.

## Project Structure & Directory Rules

Read `agent_docs/*.md` on demand, when a task touches that area.

Every service has its own `Dockerfile`, dependencies, config.
Image tags: `VERSIONS.yml` (`SERVICE_TAG: X.Y.Z-{build}`), resolved by `scripts/resolve_image_version.py` into `.image-tags.mk`; `{build}` = commit hash, except `observability`/`langfuse` (static SEMVER).
Bump a tag when its Dockerfile/image code changes.
Runtime tunables/settings/flags live in an explicit config file, never hardcoded.

Orientation only - doc filename `<dirname>.md` under `agent_docs/services/` unless noted:

- `_common/` - shared logging/auth/mcp helpers
- `clickhouse/`, `init/` - storage/schema, role bootstrap - `clickhouse.md`
- `webhook/` - ingest x2/worker/reparse/migrate
- `redis/` - queue
- `grafana/` - dashboards
- `litellm/` - proxy
- `load-balancer/` - gateway
- `loadtest-fixtures/` - fixture extraction
- `mcp-dev/`, `mcp-stats/` - dev SQL / prod stats MCP
- `mcp-server/` - STALE, pycache-only - pending removal
- `backup/` - only via `dev-ops`
- `autoheal` - no dir, restarts stuck-`unhealthy` services - `autoheal.md`
- `alloy/blackbox/loki/node-exporter/prometheus/` - observability (opt-in) - `observability.md`
- `langfuse-minio/`, `langfuse-redis/` - Langfuse (opt-in) - `langfuse.md`

Root-level:

- `Makefile`
- `docker-compose.yml`/`.dev.yml` - prod/dev split - `agent_docs/architecture.md`
- `.env.example`/`.env` - gitignored
- `hooks/`, `scripts/`
- `.claude/` - agents/skills/commands/rules
- `.claude/data/` - gitignored scratch, one subdir per purpose

## Agent & Skill Routing

**Any mention of `AGENTS.md` hands off to `harness-expert`**, before inline Read/grep/Bash - pass the request verbatim.
Full descriptions: `agent_docs/harness-index.md`.

**Proactive agents (no waiting to be asked):**

- `harness-expert` - any Subagent/Skill/Command frontmatter/body edit, or `AGENTS.md` edit/review/audit.
- `dev-ops` - service rebuild/restart after a config/env change, `make backup-*`/`restore-*`, `Makefile`/compose edits, Langfuse/observability up/down.
- `webhook-test-runner` - after any `services/webhook/` change, or any test-run request.
- `loadtest-runner` - any load-test request or stack-under-load question.
- `dashboard-panels-builder` - any dashboard panel edit except panel-76/77 (Trace).
- `dashboard-parser` - any read/parse of `agents_overview.json`.
- `dynamictext-panel-builder` - panel-76 (Trace) or its companion panel-77.
- `stale-ref-sweeper` - after any rename/removal of a named entity, or before naming one in a new comment/doc.
- `code-locator` - find files/symbols instead of inline Grep/Glob.
- `script-ops` - mechanical file/JSON/YAML work, open-ended investigation, read-only docker inspection; never `git`.

**Explicit-dispatch agents:**

- `clickhouse-analyst` - cost/token/error/latency questions, panel-query debugging, one-off lookups.
- `sql-expert` - ClickHouse DBA: profiling, schema-fit review, complex queries, inexplicable-query escalation.
- `query-perf-runner` - execution half of query-perf benchmarking, dispatched by `sql-expert`.
- `loadtest-sql` - dashboard widget SQL timing under load.
- `litellm-tester` - smoke-test LiteLLM models reachable after a config change.
- `litellm-test-alerting` - verify LiteLLM's native alerting end to end.

**Skills:**

- `md-format` - before writing/editing markdown prose or tables.
- `clickhouse-sql` - before non-trivial ClickHouse SQL, or when a result looks inexplicable.
- `clickhouse-migration` - before creating/editing a file under `services/clickhouse/migrations/`.
- `dashboard-panels` - before creating/editing any dashboard panel.
- `trace-debugging` - troubleshooting a call chain via `session_id`/`trace_id`/`turn_id`.
- `harness-guardian` - explicit-only harness budget-audit workflow.

## Code Style & Guidelines

- Stdlib `logging`, never `print()`; bare `LOG_LEVEL` env var (`agent_docs/services/webhook.md`).
- `services/webhook/src/fastjson.py`, never stdlib `json` (`dumps()` returns `bytes`).
- Every `webhook`/`webhook-worker` tunable in `config.py`, never scattered `os.environ`.
- Skills/agents/config stay CLI-agnostic - exception: names the CLI itself defines.
- Comments cover a non-obvious *why*, never this machine's current state.

## Coding Anti-Patterns

- No per-service env defaults - `docker-compose.yml` is the only place `CLICKHOUSE_*`/`REDIS_*` defaults live.
- No TTL-based auto-delete on any table - half-year `PARTITION BY` instead.
- No per-service `README.md` under `services/*/` - playbooks live in root README.
- Never loosen `_validate_readonly_sql` in `services/mcp-dev/src/server.py`.
- Never derive cost from a local price table - use LiteLLM's own `response_cost`/`cost_breakdown` (`agent_docs/incidents.md`).
- Never restart/recreate `clickhouse` or edit a dashboard as a side effect of other work.
- Never call `docker compose build up / start / restart logs status` directly - always `make build / up start / restart / logs / status`.

## Boundaries & Safety

- **Git destructive actions** - never `checkout --`/`restore`/`reset --hard`/`clean` without asking, even on your own edit (hook-enforced). Rule: `agent_docs/git-safety.md`; why: `agent_docs/incidents.md`.
- **ClickHouse access** - never `docker exec .../clickhouse-client`; always `mcp-dev`'s `query`/`profile_query` or `mcp-stats`'s `me`. If rejected, ask first.
- **`litellm` restart/recreate** - never without asking; `restart` drops new env vars, only `up -d` picks them up (`agent_docs/incidents.md`).
- **DB/volume wipe or `TRUNCATE`** - ask first (`agent_docs/incidents.md`).
- **Secrets** - personal LiteLLM key never in `.env` (gitignored).
- **New incident** (destructive action, damaging bug, bad misdiagnosis) - whoever hits it appends to `agent_docs/incidents.md`.

## Agent Working Conventions

- Build a `TodoWrite` list for multiple asks; keep current.
- Check for an owning agent before inline Bash/Read/Grep.
- Translate non-English subagent prompts to English (1:1 meaning).
- A dispatch's `prompt`/`description` carry the content - no prose recap alongside.
- After a significant change, check `.env.example`/`README.md`/`AGENTS.md` for updates.
- Rename/removal of a named entity invokes `stale-ref-sweeper` proactively.

### CLI adapter notes (Codex CLI)

Codex-specific operating notes (no `Task` tool, agent discovery, model routing) - `agent_docs/architecture.md`.
