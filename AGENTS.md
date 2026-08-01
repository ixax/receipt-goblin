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
Image tags: `agent_docs/architecture.md`.
Runtime tunables/settings/flags live in an explicit config file, never hardcoded.

Orientation only - doc filename `<dirname>.md` under `agent_docs/services/` unless noted:

- `_common/` - shared logging/auth/mcp helpers - `common.md`
- `clickhouse/`, `init/` - storage/schema, role bootstrap - `clickhouse.md`
- `webhook/` - ingest entry point (enqueue-only)
- `worker/` - `webhook-worker`, batches queue into ClickHouse
- `reparse/` - replays stored payloads after a parsing fix
- `migrate/` - applies ClickHouse migrations
- `loadtest/` - `make loadtest`'s replay role
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
- `.claude/` - agents/commands/rules
- `.agents/skills/` - canonical skill content; `.claude/skills` is a symlink to it
- `.claude/data/` - gitignored scratch, one subdir per purpose
- `plans/` - approved `/plan` outputs, one file per plan

## Agent & Skill Routing

Full descriptions of every agent/skill below: each one's own frontmatter `description` (Claude Code shows these natively).
Codex CLI has no native listing - it reads `agent_docs/harness-index.md` instead (CLI adapter notes, below).

**Proactive agents (no waiting to be asked):**

- `harness-expert` - any Subagent/Skill/Command frontmatter/body edit, or `AGENTS.md` edit/review/audit.
- `dev-ops` - service rebuild/restart after a config/env change, `make backup-*`/`restore-*`, `Makefile`/compose edits, Langfuse/observability up/down.
- `webhook-test-runner` - after any `services/worker/`, `services/reparse/`, `services/loadtest/`, or `services/_common/` change, or any test-run request.
- `loadtest-runner` - any load-test request or stack-under-load question.
- `dashboard-panels-builder` - any dashboard panel edit except panel-76/77 (Trace).
- `dashboard-parser` - any read/parse of `agents_overview.json`.
- `dynamictext-panel-builder` - panel-76 (Trace) or its companion panel-77.
- `stale-ref-sweeper` - after any rename/removal of a named entity, or before naming one in a new comment/doc.
- `code-locator` - find files/symbols instead of inline Grep/Glob.
- `script-ops` - mechanical file/JSON/YAML work, open-ended investigation, read-only docker/git inspection; never state-changing `docker`/`git`.

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

## Code & Anti-Patterns

- Writing or editing code: read `agent_docs/rules/coding.md` first (style + anti-patterns).
  Skip for a pure analysis/investigation task.

## Boundaries & Safety

- **Git** - before any git action, read `agent_docs/git-safety.md` first.
- **ClickHouse access** - before any direct ClickHouse access, read `agent_docs/rules/clickhouse-access.md` first.
- **`litellm` restart/recreate** - before touching, read `agent_docs/rules/litellm-ops.md` first.
- **DB/volume wipe or `TRUNCATE`** - ask first (`agent_docs/incidents.md`).
- **Secrets** - personal LiteLLM key never in `.env` (gitignored).
- **New incident** (destructive action, damaging bug, bad misdiagnosis) - whoever hits it appends to `agent_docs/incidents.md`.

## Agent Working Conventions

- Build a `TodoWrite` list for multiple asks; keep current.
- `/plan` output always saves directly to `plans/<name>.md`.
  Never write it to a plan-mode scratch file first and then copy it over.
- Before presenting any plan for approval (EnterPlanMode/ExitPlanMode or any plan doc), its frontmatter includes a `date` field and a `context` field.
  Leave `context` empty if the session opened directly with a request to make a plan.
  Otherwise `context` summarizes what happened earlier in the session, before the decision to write a plan.
- After a plan's work is done, offer to delete its `plans/` file - never delete it unasked.
- Check for an owning agent before inline Bash/Read/Grep.
- Translate non-English subagent prompts to English (1:1 meaning).
- A dispatch's `prompt`/`description` carry the content - no prose recap alongside.
- After a significant change, check `.env.example`/`README.md`/`AGENTS.md` for updates.
- Rename/removal of a named entity invokes `stale-ref-sweeper` proactively.

### CLI adapter notes (Codex CLI)

Codex-specific operating notes (no `Task` tool, agent discovery, model routing) - `agent_docs/architecture.md`.
