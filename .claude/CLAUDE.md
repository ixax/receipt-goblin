# Agent Tracking Stack

Local stack for tracking cost/efficiency of AI agents (Claude Code / Claude
API) with full call-chain tracing. Hooks on the host POST to `ingest-api`,
which writes to ClickHouse; Grafana reads from ClickHouse; Claude Code reads
back out via the `mcp-clickhouse` MCP server. See `README.md` for
quickstart, troubleshooting, and the full panel/variable reference.

## Repository layout

| Path                              | What it is                                                            |
|------------------------------------|--------------------------------------------------------------------------|
| `docker-compose.yml`               | Four services, network, volumes, `mem_limit`s. Single source of truth for `CLICKHOUSE_*` defaults. |
| `clickhouse/schema.sql`            | DDL for all tables + `model_pricing` seed. Auto-applies only on first container start (empty volume). |
| `clickhouse/config.d/memory.xml`   | Caps ClickHouse memory at 85% of its `mem_limit`.                        |
| `ingest-api/main.py`               | Write-only FastAPI app: `/ingest/{event,usage,message}`, `/registry/{agent,skill}`, `/health`. |
| `mcp-clickhouse/server.py`         | Read-only-by-validation FastMCP server: `whatsup(hours)` (3 fixed queries) and `query(sql, max_rows)` (arbitrary SELECT/WITH, validated in `_validate_readonly_sql` - no separate DB user backs this, so that function is the only enforcement). |
| `.mcp.json`                        | Registers `mcp-clickhouse` at `http://localhost:8001/mcp`.               |
| `grafana/dashboards/agents_overview.json` | "Agents Overview" dashboard, `dashboard.grafana.app/v2beta1` schema, 31 panels across 6 tabs, uid `agents-overview` (stable URL). |
| `grafana/docker-entrypoint.sh`     | Renders the ClickHouse datasource template via `sed`, then execs Grafana. |
| `.claude/settings.json`            | Wires every Claude Code lifecycle event to `log_event.py` / `log_session.py`. |
| `.claude/hooks/common.py`          | Shared helpers: frontmatter parsing, user id, HTTP POST, turn/sequence/latency state. |
| `.claude/hooks/log_event.py`       | Handles every lifecycle event except SessionStart/SessionEnd.            |
| `.claude/hooks/log_session.py`     | Handles SessionStart (triggers registry scan) and SessionEnd.            |
| `.claude/hooks/register_agents.py` | Scans `.claude/agents/*.md` and `.claude/skills/*/SKILL.md`, upserts into the registries. Runnable standalone. |
| `.claude/agents/file-ops.md`       | `claude-haiku-4-5` delegate for mechanical file I/O - see "Delegate filesystem work" below. |
| `.claude/agents/clickhouse-analyst.md` | `claude-haiku-4-5` delegate for ClickHouse lookups - see "Delegate ClickHouse lookups" below. |
| `.claude/agents/script-ops.md`     | `claude-haiku-4-5` delegate for scripted (Python/jq) file inspection/transforms - see "Delegate scripted file work" below. |
| `.codex/hooks.json`                | Wires Codex CLI's lifecycle events to `.codex/hooks/log_event.py` / `log_session.py`, mirroring `.claude/settings.json`. Codex has no `$CLAUDE_PROJECT_DIR`-equivalent env var, so commands use an absolute path - not portable to another checkout as-is. |
| `.codex/hooks/`                    | Codex CLI hook handlers - same role as `.claude/hooks/log_event.py`/`log_session.py`, importing `.claude/hooks/common.py` directly (no copy-paste) for the provider-agnostic parts (user id, POST, turn/sequence counters, latency timers). Field extraction differs from Claude Code's hooks: no PostToolUseFailure/StopFailure/PermissionDenied events (status is inferred from `tool_response`), and usage comes from diffing cumulative `token_count` events in Codex's rollout JSONL rather than per-message transcript rows - see the module docstring for what's unconfirmed from public docs. |

## Delegate filesystem work to `file-ops`

For mechanical, fully-specified file operations - reading a known path,
grepping/globbing, or writing/editing where the exact content or old/new
string is already decided - dispatch to the `file-ops` subagent via the
Task tool instead of doing it directly, to run that work on the cheaper
model. Don't delegate anything requiring judgment about *what* to change,
ambiguous instructions, or Bash/git - `file-ops` has no tools for those by
design. Not worth it for a single trivial one-off read/edit either - the
win is on repeated/bulk mechanical work, not every last file touch.

## Delegate ClickHouse lookups to `clickhouse-analyst`

For questions answerable from the tables in `clickhouse/schema.sql` - cost/
token/error/latency/adoption numbers, debugging a Grafana panel's query,
one-off metric lookups - dispatch to the `clickhouse-analyst` subagent via
the Task tool instead of calling `mcp__clickhouse__query`/`whatsup`
directly. It runs on `claude-haiku-4-5` and returns only the distilled
answer, so large result sets never land in the main conversation's context.
Don't add it Bash or direct ClickHouse access - all reads go through
`mcp-clickhouse` (see "ingest-api stays write-only" below); its only tools
are the two `mcp__clickhouse__*` ones.

## Delegate scripted file work to `script-ops`

For inspecting or transforming structured files (JSON/YAML - e.g. the
Grafana dashboard) with a Python/jq snippet where the exact read/check/edit
is already decided - not "figure out what SQL/schema to use," just "run
this script" - dispatch to the `script-ops` subagent instead of running
`python3 -c "..."` directly, so verbose printed output (dumped JSON,
inspection results) stays out of the main conversation. Unlike `file-ops`,
it has `Bash` - but never use it for `docker`/`git`, and it shouldn't
decide what a transformation should do, only execute one already spelled
out.

## Rules to not violate

- **No per-service env defaults in Python/entrypoint code.** `docker-compose.yml` is the only place `CLICKHOUSE_*` defaults live; `main.py`/`server.py` read them with plain `os.environ[...]`, and `grafana/docker-entrypoint.sh` asserts (`:?`) rather than re-defaulting. A second copy of the defaults has drifted before (ingest-api once silently defaulted the password to `""`).
- **Never `UPDATE`/`ALTER` `model_pricing` rows.** Insert a new row with a new `effective_from` instead - cost is computed at query time via `ASOF JOIN`, so this keeps historical cost accurate.
- **`schema.sql` changes need a manual re-apply** on an already-initialized volume: `docker exec -i agent-tracking-clickhouse clickhouse-client --multiquery < clickhouse/schema.sql`, or drop the `clickhouse-data` volume (destroys data).
- **Bump `version` in frontmatter whenever you edit an agent's or skill's behavior.** The registry is `ReplacingMergeTree ORDER BY (name, version)`, so old rows keep pointing at the version active when they ran.
- **ingest-api stays write-only.** All reads from Claude Code go through `mcp-clickhouse`, never `docker exec`-ing into ClickHouse.
- **Don't loosen `_validate_readonly_sql` in `mcp-clickhouse/server.py`.** There's no separate read-only ClickHouse user backing `query` - that function (single statement, SELECT/WITH only, no DDL/DML keywords, no system tables, no remote/file/URL table functions) is the only thing standing between it and a write/DDL statement.
- **Grafana dashboard filters: use `has([${var:singlequote}], col)`, never bare `col IN (${var:singlequote})` or `$var = 'All'`.** A multi-select variable with zero current options renders as `IN ()`, which ClickHouse rejects at parse time even inside an `OR` guard - `has([...], col)` degrades to a valid empty-array check instead. And an unformatted `$var` never equals the literal `'All'`; it's the same comma-joined list as `:singlequote`, so `$var = 'All'` silently produces a broken tuple-literal comparison. See README for the full filter-pattern writeup if adding a new panel.
- **A variable's own query must never return zero rows**, or `grafana-clickhouse-datasource` fails variable refresh itself with `Templating [$var] Error updating options: Couldn't find any field of type string in the results` - a separate, earlier failure than the `IN ()` crash above, since it happens before any panel query even runs. `$mcp_tool`'s query (`WHERE startsWith(tool_name, 'mcp__')`, empty until an MCP tool is first called) hit this - fixed with a `UNION ALL` fallback row emitted only when the real query is empty. Apply the same fallback to any other variable query that can legitimately return nothing.
- **Don't reintroduce the panel-7 drill-down-by-URL-variable pattern.** Per-cell Grafana `inspect` (on `prompt_text`/`response_text`/`raw_payload`) replaced it - simpler and stays in place.

## Chain tracing / identity

Every row carries `session_id`, `trace_id` (parent's `session_id` for
subagent trees), `parent_session_id`, `turn_id` (increments per
`UserPromptSubmit`), `sequence_id` (increments per event in a turn).
`X-User-Id` is the logged-in Claude account email (`oauthAccount.emailAddress`
in the global `~/.claude.json`, undocumented/internal, best-effort), falling
back to `"{hostname}-{username}"` from `hooks/common.py:get_user_id()` if
that's unavailable.

```sql
SELECT turn_id, sequence_id, timestamp, event_type, tool_name, agent_name, skill_name, status
FROM agent_events WHERE session_id = '<session-id>' ORDER BY turn_id, sequence_id;
```

`agent_events.latency_ms` is overloaded by `event_type`: tool execution
time on `PostToolUse`/`PostToolUseFailure`, permission-prompt wait time on
`PreToolUse`/`PermissionDenied`, and turn duration (`UserPromptSubmit` ->
`Stop`) on `Stop`/`StopFailure` - all three reuse the same generic
`mark_tool_start`/`pop_tool_latency_ms` timer in `common.py`, just keyed
differently. `agent_usage` also carries `stop_reason`/`service_tier`/
`speed`/cache-tier breakdown/`web_search_requests`/`web_fetch_requests` -
see README "Per-request signals on `agent_usage`" for what each means and
where it comes from.

## Debugging hooks

Field extraction from hook payloads is best-effort and can drift across
Claude Code versions. Run a hook manually with `CLAUDE_TRACKING_DEBUG=1` to
dump the raw payload to stderr:

```bash
CLAUDE_TRACKING_DEBUG=1 echo '{"hook_event_name":"Stop","session_id":"test"}' | python3 .claude/hooks/log_event.py
```

Version-dependent events (`PostToolUseFailure`, `PostToolBatch`,
`PermissionRequest`, `PermissionDenied`, `SubagentStart`, `PostCompact`,
`StopFailure`) simply never fire on older Claude Code versions - safe to
leave wired in `settings.json` regardless.

## Windows

Hook scripts are stdlib-only Python 3, OS-agnostic. `settings.json` invokes
them via `${CLAUDE_HOOK_PYTHON:-python3} ...` (POSIX parameter expansion -
fine under Git Bash/WSL). If hooks run through `cmd.exe` with no `python3`
on `PATH`, set `CLAUDE_HOOK_PYTHON` to `python` or `py -3`.

## Using this in another project

```
.claude/settings.json .claude/hooks/ .claude/agents/*.md .claude/skills/*/SKILL.md
.mcp.json  mcp-clickhouse/   (needs its docker-compose service too)
```

Re-register without a new session: `python3 .claude/hooks/register_agents.py`.
