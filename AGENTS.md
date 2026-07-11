# Agent Tracking Stack

Local stack for tracking cost/efficiency of AI coding agents (Claude Code and Codex CLI) with full call-chain tracing.
Hooks on the host POST to `ingest-api`, which writes to ClickHouse; Grafana reads from ClickHouse; a CLI session reads back out via the `mcp-clickhouse` MCP server.

## Repository layout

| Path                                      | What it is                                                                                                                                                                          |
|--------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `clickhouse/config.d/memory.xml`          | Caps ClickHouse memory at 85% of its `mem_limit`.                                                                                                                                   |
| `clickhouse/schema.sql`                   | DDL for all tables + `model_pricing` seed. Auto-applies only on first container start (empty volume).                                                                               |
| `grafana/dashboards/agents_overview.json` | "Agents Overview" dashboard, `dashboard.grafana.app/v2beta1` schema, 31 panels across 6 tabs, uid `agents-overview` (stable URL).                                                   |
| `grafana/docker-entrypoint.sh`            | Renders the ClickHouse datasource template via `sed`, then execs Grafana.                                                                                                           |
| `ingest-api/main.py`                      | Write-only FastAPI app: `/ingest/{event,usage,message}`, `/registry/{agent,skill}`, `/health`.                                                                                      |
| `mcp-clickhouse/server.py`                | Read-only-by-validation FastMCP server: `whatsup(hours)` (3 fixed queries) and `query(sql, max_rows)` (arbitrary SELECT/WITH; validation rules — see "Rules to not violate" below). |
| `.mcp.json`                               | Registers `mcp-clickhouse` at `${AGENT_CLI_TRACKING_MCP_URL:-http://localhost:8001/mcp}` (Claude Code env-var expansion).                                                           |
| `docker-compose.yml`                      | Four services, network, volumes, `mem_limit`s. Single source of truth for `CLICKHOUSE_*` defaults.                                                                                  |

`.claude/` and `.codex/` hold the hooks, agents, and skills that feed this stack - browse `.claude/hooks/`, `.claude/agents/`, `.claude/skills/`, `.codex/hooks/` directly rather than looking for a file-by-file index here.
Each file/frontmatter is self-explanatory once opened.
Agents and skills are picked up by their `name`/`description` frontmatter alone - that's always visible, and is the only thing that should drive when to use one.
Don't restate "when to use X" here; if a selection criterion is missing, add it to that agent's/skill's own `description` instead.
One non-obvious fact worth calling out because it doesn't show up just from reading either hooks file: Codex has no `$CLAUDE_PROJECT_DIR`-equivalent env var, so `.codex/hooks.json` uses an absolute path internally - not portable to another checkout as-is.

Everything local and disposable lives under one gitignored root, `.state/` (never under git - see `.gitignore`):

| Path                          | What it's for                                                                                   |
|-------------------------------|----------------------------------------------------------------------------------------------------|
| `.state/tracking/<session_id>.json` | `common.py`'s turn/sequence counters and tool/permission latency timers, one flat file per session - written by every hook call, on either CLI. |
| `.state/MIN_DUMP.md`          | The `/min` skill's latest session snapshot - one fixed path, not per-session, overwritten on every run (see `.claude/skills/min/SKILL.md`). |

Future loop-dev artifacts (e.g. `NOTES.md`, `TASKS.md`) should follow `MIN_DUMP.md`'s pattern - a fixed-name file directly under `.state/`, not a new per-session directory - unless there's a concrete reason a given file needs to be scoped per session.

## Rules to not violate

- **Skills, agents, and any config/naming this repo owns must stay CLI-agnostic.** This stack tracks Claude Code and Codex CLI equally - don't write a skill/agent body that only makes sense for one of them without saying so explicitly, and don't name a variable/file after "Claude" when the thing it controls is actually shared (`CLAUDE_TRACKING_API_URL` used to do exactly this - renamed to `AGENT_CLI_TRACKING_API_URL`, see README "Configuration"). The one exception: names Claude Code or Codex itself defines (`CLAUDE_PROJECT_DIR`, `.claude/`, `.codex/`) - don't rename those, they aren't ours to rename.
- **No per-service env defaults in Python/entrypoint code.** `docker-compose.yml` is the only place `CLICKHOUSE_*` defaults live; `main.py`/`server.py` read them with plain `os.environ[...]`, and `grafana/docker-entrypoint.sh` asserts (`:?`) rather than re-defaulting. A second copy of the defaults has drifted before (ingest-api once silently defaulted the password to `""`).
- **Never `UPDATE`/`ALTER` `model_pricing` rows.** Insert a new row with a new `effective_from` instead - cost is computed at query time via `ASOF JOIN`, so this keeps historical cost accurate.
- **`schema.sql` changes need a manual re-apply** on an already-initialized volume: `docker exec -i agent-tracking-clickhouse clickhouse-client --multiquery < clickhouse/schema.sql`, or drop the `clickhouse-data` volume (destroys data).
- **Bump `version` in frontmatter whenever you edit an agent's or skill's behavior.** The registry is `ReplacingMergeTree ORDER BY (name, version)`, so old rows keep pointing at the version active when they ran.
- **ingest-api stays write-only.** All reads go through `mcp-clickhouse`, never `docker exec`-ing into ClickHouse.
- **`clickhouse-analyst`'s tools stay limited to `mcp__clickhouse__query`/`whatsup`.** Never add it Bash or any other direct ClickHouse access - all reads must go through `mcp-clickhouse`, per the rule above.
- **`file-ops`/`script-ops` never get `git` (and `file-ops` never gets `Bash`).** Blast-radius judgment calls (`git`, `docker`) stay with the caller, not a delegate.
- **Don't loosen `_validate_readonly_sql` in `mcp-clickhouse/server.py`.** There's no separate read-only ClickHouse user backing `query` - that function (single statement, SELECT/WITH only, no DDL/DML keywords, no system tables, no remote/file/URL table functions) is the only thing standing between it and a write/DDL statement.
