# Agent Tracking Stack

Tracks cost/efficiency of AI coding agents (Claude Code, Codex CLI), full call-chain tracing.
Queue-based ingest: LiteLLM -> `webhook` (enqueue-only) -> `redis` -> `webhook-worker` (batches into ClickHouse); Grafana reads ClickHouse; CLI reads back via `mcp-dev`/`mcp-stats`.
Dev/prod split: `agent_docs/architecture.md`; per-service detail: `agent_docs/services/<name>.md`.

## Commands

- `make init` - first-run ClickHouse role provisioning.
- `make start` / `make up [SERVICE=x]` - bring up (existing / rebuild+recreate).
- `make migrate` - apply ClickHouse migrations, explicit-only.
- `make status` - wait until healthy.
- `make stop` / `make down [SERVICE=x]` - tear down.
- `make setup-client` - print CLI-proxy shell/config snippets.
- `make test` - umbrella: runs `test-services` + `test-hooks`.
- `make test-services` - webhook pytest suite; always via `runner-test`, never inline.
- `make test-hooks` - budget-audit unit tests.
- `make lint` - repo-wide ruff check; always via `runner-linter`, never inline.
- `make loadtest` - ramping load test; always via `loadtest-runner`, never inline.
- `make loadtest-fixtures [VOLUME=small|medium|large]` - build fixtures from ClickHouse.
- `make harness-index` - regenerate `agent_docs/harness-index.md` after any frontmatter change.

## Project structure

Read `agent_docs/*.md` on demand, when a task touches that area.
Every service has its own `Dockerfile`, dependencies, config.
Image tags: `agent_docs/architecture.md`.
Runtime tunables/settings/flags live in an explicit config file, never hardcoded.

Python version: pinned repo-wide in root `.python-version`.
Every Python-based `Dockerfile`'s `FROM python:${PYTHON_VERSION}-slim` reads that value via a `PYTHON_VERSION` build-arg, propagated through `Makefile` and `docker-compose.yml`'s `build.args` - bump `.python-version`, run `make build`, every image rebuilds against the new version in one shot.
Local scripts/tests run via `uv run`/`uv sync`, which reads `.python-version` automatically - keeps Claude Code, Codex CLI, and human contributors on one interpreter instead of whatever `python3` resolves to locally.

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
- `mcp-server/` - STALE, pycache-only - never touch
- `backup/` - only via `dev-ops`
- `autoheal` - no dir, restarts stuck-`unhealthy` services - `autoheal.md`
- `alloy/blackbox/loki/node-exporter/prometheus/` - observability (opt-in) - `observability.md`
- `langfuse-minio/`, `langfuse-redis/` - Langfuse (opt-in) - `langfuse.md`

Root-level:

- `Makefile`
- `docker-compose.yml`/`.dev.yml` - prod/dev split - `agent_docs/architecture.md`
- `.env.example`/`.env` - gitignored
- `hooks/`, `scripts/`
- `.claude/` - agents/rules
- `.agents/skills/` - canonical skill content; `.claude/skills` is a symlink to it
- `.claude/data/` - gitignored scratch, one subdir per purpose
- `plans/` - approved `/plan` outputs, one file per plan

## Agent & skill routing

Trigger conditions live in each entity's own frontmatter `description` - Claude Code lists them natively; Codex CLI reads `agent_docs/harness-index.md` instead (adapter notes: `agent_docs/architecture.md`).
Check for an owning agent before inline Bash/Read/Grep.

Proactive (dispatch without being asked):

- `harness-expert` - harness entities, `AGENTS.md`, `agent_docs/*.md`
- `dev-ops` - services, compose, backups, opt-in stacks
- `runner-test` - test runs
- `runner-linter` - lint runs
- `loadtest-runner` - load tests
- `dashboards-expert` - dashboard panel edits
- `dashboard-parser` - `agents_overview.json` reads
- `stale-ref-sweeper` - entity renames/removals
- `code-locator` - codebase search
- `script-ops` - mechanical / read-only ops

Explicit-dispatch: `clickhouse-analyst`, `sql-expert`, `query-perf-runner`, `loadtest-sql`, `litellm-tester`, `litellm-test-alerting`.

Skills: `md-format`, `clickhouse-sql`, `clickhouse-migration`, `dashboard-panels`, `dynamictext-panel-queries`, `dynamictext-panel-design-system`, `trace-debugging`, `harness-guardian`, `me`, `min`.

## Code

Writing or editing code: read `agent_docs/rules/coding.md` first (style + anti-patterns).
Skip for a pure analysis/investigation task.

## Boundaries & safety

- Git - before any git action, read `agent_docs/git-safety.md` first.
- ClickHouse access - before any direct access, read `agent_docs/rules/clickhouse-access.md` first.
- `litellm` restart/recreate - before touching, read `agent_docs/rules/litellm-ops.md` first.
- DB/volume wipe or `TRUNCATE` - ask first (`agent_docs/incidents.md`).
- Secrets - personal LiteLLM key never in `.env` (gitignored).
- New incident (destructive action, damaging bug, bad misdiagnosis) - whoever hits it appends to `agent_docs/incidents.md`.

## Working conventions

- Build a `TodoWrite` list for multiple asks; keep current.
- `/plan` output always saves directly to `plans/<name>.md` - never via a plan-mode scratch file.
- Any plan presented for approval (EnterPlanMode/ExitPlanMode or a plan doc) carries frontmatter `date` and `context` fields.
  `context` summarizes the session before the decision to plan; empty if the session opened with the plan request.
- After a plan's work is done, offer to delete its `plans/` file - never delete it unasked.
- Translate non-English subagent prompts to English (1:1 meaning).
- A dispatch's `prompt`/`description` carry the content - no prose recap alongside.
- After a significant change, check `.env.example`/`README.md`/`AGENTS.md` for updates.
