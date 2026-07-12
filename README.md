# Agent Tracking Stack

Local stack for tracking cost and efficiency of AI coding agents (Claude Code and Codex CLI), with full call-chain tracing - agent, skill, and MCP tool usage are all tracked, not just top-level agent activity.
See `AGENTS.md` for architecture, schema, and hook-coverage details.

## Overview

### How data flows

1. The CLI (Claude Code or Codex) fires a hook on the host (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, etc.) for every lifecycle event, tool call, and turn.
2. The hook script POSTs the event (or, at `Stop`, the turn's token usage) to the ingest API on `:8000`.
3. The ingest API writes the row straight into ClickHouse on `:8123` - nothing is buffered or batched beyond that single request.
4. Grafana on `:3000` queries ClickHouse directly for every panel; there's no caching layer, so a dashboard refresh always reflects current table state.
5. Reads go the other way: the ingest API is write-only, so a CLI session reads data back out (e.g. `/whatsup` in Claude Code) via the `mcp-server` MCP server on `:8001`, registered in `.mcp.json`.

## Getting started

### Start the stack

```bash
docker compose up -d --build
```

### Wait until it's healthy

```bash
docker compose ps
```

`ingest-api`, `mcp-server`, and `grafana` won't start until `clickhouse` shows `healthy` (all three `depends_on: condition: service_healthy`).
Re-run the command above until all four services show up and `clickhouse` is no longer `starting`/`unhealthy`.

### Open Grafana

http://localhost:3000/d/agents-overview/agents-overview

Anonymous viewer access is enabled by default - no login needed.

## Usage

### Test agents and skills

Subagents and skills are a Claude Code concept - Codex has no equivalent, so this test flow is Claude Code only.
Open a Claude Code session **in this project directory** so `.claude/settings.json` is picked up:

```bash
claude
```

`SessionStart` fires immediately and registers `test-researcher`, `test-coder`, `test-summarizer`, and `test-linter`.
Then, in the session, ask for them by name - point at a randomly chosen file in the project root rather than a fixed one, to keep token usage low:

```
> use the test-researcher agent to pick a random file from the project root and summarize what it does
> use the test-coder agent to add a one-line comment to a randomly chosen file in the project root, then immediately remove that same line
> use the test-summarizer skill to summarize a randomly chosen file in the project root
> use the test-linter skill to check a randomly chosen file in the project root for style issues
```

The write example is self-cleaning by design - anything written should be removed again right after, so repeated test runs don't leave junk in the repo.

### Check spend from Claude Code

```
/whatsup
```

Calls the `mcp-server` MCP server (`mcp__clickhouse__whatsup`, see `.mcp.json`) and reports tokens/cost spent in the last 24h, plus the top spenders - no need to open Grafana for a quick check.

### Stop the stack

```bash
docker compose down
```

Add `-v` to also delete the ClickHouse data volume (next `up` re-applies `schema.sql` from scratch).

## Troubleshooting

| Symptom                                                | Likely cause / fix                                                                                     |
|-----------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| `ingest-api`/`grafana` stuck in `Created`, never start        | Their `depends_on: condition: service_healthy` is blocking on the `clickhouse` healthcheck. Run `docker compose ps` - if `clickhouse` shows `unhealthy`, check `docker inspect agent-tracking-clickhouse --format '{{json .State.Health}}'` for the actual healthcheck error, and confirm ClickHouse itself is fine with `docker exec agent-tracking-clickhouse clickhouse-client --user default --password "$CLICKHOUSE_PASSWORD" --query "SELECT 1"`. The image ships `wget`, not `curl` - the healthcheck uses `wget --spider`. |
| `ingest-api` can't reach ClickHouse (once running)             | Check `docker compose logs clickhouse` / `docker compose logs ingest-api` for the actual connection error. |
| `/whatsup` fails or times out                                  | Confirm `mcp-server` is `healthy`/running (`docker compose ps`) and reachable at `http://localhost:8001/mcp`; check `docker compose logs mcp-server`. Claude Code only picks up `.mcp.json` changes on the next session start. |
| Hooks don't seem to fire                                     | Claude Code: run `claude --debug` or check `~/.claude/logs`; verify `.claude/settings.json` is in the project root Claude Code was started from. Codex CLI: verify `.codex/hooks.json` is in the project root Codex was started from - it uses an absolute path internally, so it isn't portable to another checkout as-is (see `AGENTS.md`). |
| `user_id` shows as hostname-username instead of an email          | `get_user_id()` prefers the Claude account email from `~/.claude.json` (`oauthAccount.emailAddress`) regardless of which CLI fired the hook - this is undocumented internal state, so if it's missing/unreadable (not logged in, older Claude Code version, or the file's shape changed) it silently falls back to `"{hostname}-{username}"`, never raises. |
| Dashboard edits stop saving after a Grafana upgrade            | Grafana 13.1.0 (bumped from 11.2.0 for tabs support - see "Dynamic dashboards" below) had a known OSS 12.4.0 bug where "Dynamic Dashboards" broke *provisioned* dashboards on save ([grafana/grafana#119450](https://github.com/grafana/grafana/issues/119450)) - our exact setup (`type: file` provider, `allowUiUpdates: true` in `grafana/provisioning/dashboards/dashboard.yml`). Unconfirmed whether 13.1.0 still has it; if UI edits silently fail to persist, that's the first thing to check. |
| Grafana stops responding after a few clicks/panel loads (no crash in browser) | Check `docker inspect agent-tracking-grafana --format '{{.State.OOMKilled}} {{.State.ExitCode}}'` - Grafana 13.1.0 is meaningfully heavier than 11.2.0 (alerting scheduler, zanzana authz, bleve search indexing, app registry, background plugin auto-updater) and hit the old `mem_limit: 512m` within a couple of dashboard interactions (`OOMKilled=true`, exit 137). Bumped to `1536m` in `docker-compose.yml` alongside the 13.1.0 upgrade; there's no `restart:` policy on the service, so an OOM-killed container just stays dead until `docker compose up -d` is run again - raise the limit further if it recurs. |
| `X-User-Id` missing / `unknown-user` in Grafana                | Both the email lookup and the hostname/username fallback failed - if `platform.node()` and `getpass.getuser()` also fail (e.g. some sandboxed CI runners), it falls back further to `"unknown-host"`/`"unknown-user"`. |
| No `agent_version`/`skill_version` on events                  | The hook resolves versions by reading `.claude/agents/<name>.md` / `.claude/skills/<name>/SKILL.md` from disk at hook time - confirm frontmatter `name:` matches the reported agent/skill name, and (Claude Code only) that `CLAUDE_PROJECT_DIR` is set, which Claude Code does automatically. |
| "Tokens by skill/version" panel empty even after using a skill | Usage is reported once per turn at `Stop`, keyed to whichever skill (if any) was invoked as a tool call earlier in that same turn - it can only pick up skills used **after** this fix landed; old `agent_usage` rows ingested before that will stay unattributed. Re-run a skill invocation to generate fresh data. |
| Grafana panel shows a query error                             | The `grafana-clickhouse-datasource` plugin's query JSON shape has changed across versions; open the panel in edit mode - the SQL in `rawSql` is otherwise plain, portable ClickHouse SQL. |
| Duplicate agent/skill registry rows                             | Expected - `ReplacingMergeTree` keyed on `(name, version)`. Re-registering the same version replaces it; bumping `version` in frontmatter creates a new row and preserves history. |
| Cost panels empty but token panels aren't                       | `model_pricing` has no row for that `model` yet, or all rows have `effective_from` after the usage timestamps. |
| Claude Code via the LiteLLM gateway fails with `x-api-key header is required` | Pointed at `litellm` directly instead of `litellm-gateway`, or `LITELLM_MASTER_KEY` isn't set - see "Why a gateway container" under "LiteLLM" below. |

## Reference

Everything below is background/design detail, not needed day-to-day - see `AGENTS.md` for the rules that actually constrain how you edit this repo.

### Configuration

| Variable                 | Default                                   | Used by                                                                                                     |
|--------------------------|--------------------------------------------|----------------------------------------------------------------------------------------------------------|
| `CLICKHOUSE_DATABASE`    | `default`                                 | clickhouse, ingest-api, mcp-server, grafana                                                             |
| `CLICKHOUSE_USER`        | `default`                                 | clickhouse, ingest-api, mcp-server, grafana                                                             |
| `CLICKHOUSE_PASSWORD`    | `changeme`                                | clickhouse, ingest-api, mcp-server, grafana                                                             |
| `CLICKHOUSE_HOST`        | `clickhouse`                              | ingest-api, mcp-server, grafana                                                                         |
| `CLICKHOUSE_PORT`        | `8123`                                    | ingest-api, mcp-server, grafana                                                                         |
| `CLICKHOUSE_HTTP_PORT`   | `8123`                                    | host port mapping for clickhouse's HTTP interface                                                           |
| `CLICKHOUSE_NATIVE_PORT` | `9000`                                    | host port mapping for clickhouse's native protocol                                                          |
| `INGEST_API_PORT`        | `8000`                                    | host port mapping for ingest-api                                                                             |
| `MCP_SERVER_PORT`        | `8001`                                    | host port mapping for mcp-server                                                                         |
| `GRAFANA_PORT`           | `3000`                                    | host port mapping for grafana                                                                               |
| `WEBHOOK_PORT`           | `8010`                                    | host port mapping for webhook                                                                           |
| `LITELLM_PORT`           | `4000`                                    | host port mapping for litellm                                                                               |
| `LITELLM_IMAGE`          | `ghcr.io/berriai/litellm:main-latest`     | litellm - pin this to a released tag before sharing the stack, see "LiteLLM" below                          |
| `WEBHOOK_URL`            | `http://webhook:8000/api/v1/metrics` | litellm - where it POSTs the `StandardLoggingPayload` for each call                                         |
| `LITELLM_MASTER_KEY`     | required, no default                      | litellm - admin credential for `/ui` and `/key/generate`; real Anthropic/OpenAI keys and per-person virtual keys are managed through the UI instead, see "LiteLLM" below |
| `LITELLM_DB_PASSWORD`    | `changeme`                                | litellm, litellm-db - Postgres password for LiteLLM's own virtual-keys/budgets database                    |

`CLICKHOUSE_PASSWORD` must stay non-empty: ClickHouse restricts the `default` user to localhost-only access whenever user/password are unset, which breaks the other three containers connecting over the Docker network.
`*_PORT` variables only change the **host** side of each port mapping - the container-internal port stays fixed, so services keep reaching each other over the `agent-tracking` Docker network regardless of what you set these to.

Each service also has a `mem_limit`: `clickhouse` 2g (paired with `clickhouse/config.d/memory.xml`'s 0.85 ratio so it respects the cgroup limit instead of trying to use host RAM), `ingest-api`/`mcp-server`/`grafana` 512m each.

The hooks and `.mcp.json` also read these from the host environment (not from `docker-compose.yml`, since they run on the host, not in a container):

| Variable                     | Default                          | Read by                                                                                               |
|------------------------------|-----------------------------------|--------------------------------------------------------------------------------------------------------|
| `AGENT_CLI_TRACKING_API_URL` | `http://localhost:8000`          | `.claude/hooks/`, `.codex/hooks/`                                                                     |
| `AGENT_CLI_TRACKING_MCP_URL` | `http://localhost:8001/mcp`      | `.mcp.json` (Claude Code `${VAR:-default}` expansion)                                                 |
| `AGENT_CLI_TRACKING_TIMEOUT` | `3` (seconds)                    | `.claude/hooks/`, `.codex/hooks/`                                                                     |
| `AGENT_CLI_TRACKING_DEBUG`   | unset (`1` to enable)            | `.claude/hooks/`, `.codex/hooks/` - dumps the raw hook payload to stderr, see "Debugging hooks" below |
| `AGENT_CLI_HOOK_PYTHON`      | `python3`                        | `.claude/settings.json` only - Codex's `.codex/hooks.json` has no such override, see "Windows" below  |
| `CLAUDE_PROJECT_DIR`         | set automatically by Claude Code | `.claude/hooks/` - Claude Code's own built-in variable, not one this repo defines                     |

If the stack isn't running on the same host as the CLI (e.g. ingest-api/ClickHouse/Grafana live on a shared server, not your laptop), set `AGENT_CLI_TRACKING_API_URL` and `AGENT_CLI_TRACKING_MCP_URL` to that host's address before starting a session - both default to `localhost`, which only works when everything runs on one machine.

### Schema

| Table            | Purpose                                                                        |
|-------------------|----------------------------------------------------------------------------------|
| `agent_registry` / `skill_registry` | name/version/description/source_file, `ReplacingMergeTree ORDER BY (name, version)`. |
| `agent_events`    | One row per lifecycle event, full `raw_payload` JSON.                           |
| `agent_usage`     | One row per model call (tokens), parsed from the session transcript.            |
| `agent_messages`  | One row per turn, holding `prompt_text`/`response_text`.                        |
| `model_pricing`   | Manually seeded (Fable 5, Opus 4.8, Sonnet 5, Haiku 4.5). Cost is computed at query time via `ASOF JOIN`, never stored. |

Add a price change by inserting a new row, never updating an old one:

```sql
INSERT INTO model_pricing (model, effective_from, price_in_per_mtok, price_out_per_mtok)
VALUES ('claude-sonnet-5', '2026-09-01 00:00:00', 3.0, 15.0);
```

### Per-request signals on `agent_usage`

Beyond token counts, each usage row also carries a few fields read straight off the transcript's `message`/`message.usage`, added because token/cost alone can't tell a normal completion from a truncated or refused one, or show which cache tier actually got written:

| Column | Source | Why |
|---|---|---|
| `stop_reason` | `message.stop_reason` | `end_turn` vs `max_tokens` vs `refusal` vs `tool_use` - a `max_tokens` row means the reply got cut off, not just that it was expensive. |
| `service_tier`, `speed` | `message.usage.service_tier` / `.speed` | Request-level metadata Anthropic already returns; currently always `standard` in this project's own history but worth capturing for when it isn't. |
| `cache_creation_1h_tokens`, `cache_creation_5m_tokens` | `message.usage.cache_creation.ephemeral_{1h,5m}_input_tokens` | 1h and 5m ephemeral cache writes are priced differently; `cache_creation_tokens` stays their sum for the existing cost/token panels, these two are the breakdown. |
| `web_search_requests`, `web_fetch_requests` | `message.usage.server_tool_use.*` | How many built-in tool calls the model made as part of generating this reply. |

There's no request-level "reasoning effort" field anywhere in the hook payload or transcript (checked - `grep`ed every local transcript for `effort`/`reasoning_effort`/`budget`, none exist).
Model choice (`agent_usage.model`) is the closest proxy: cheaper/faster models are already picked per-agent via `model:` in an agent's frontmatter (e.g. `.claude/agents/test-coder.md` uses `claude-haiku-4-5`), and panels 16/17 already break cost/tokens down by model.

### Turn duration

`agent_events.latency_ms` on a `Stop`/`StopFailure` row is the wall-clock time from that turn's `UserPromptSubmit` to its `Stop` - one more meaning for the same overloaded field alongside tool execution time and permission-prompt wait time (see "How permission-prompt wait time is measured" below).
Reuses the exact same generic start/elapsed timer (`mark_tool_start`/`pop_tool_latency_ms` in `common.py`) that `PreToolUse`/`PostToolUse` already use for tool execution, just keyed by a fixed `"turn"` string instead of a `tool_use_id`.
A subagent's own duration doesn't need a separate timer - it's already captured as the parent's `Task` tool `PostToolUse` `latency_ms`.

### Message and tool-level text

`agent_events.raw_payload` already carries `tool_input`/`tool_response` for every tool call and `UserPromptSubmit.prompt`.
`agent_messages` adds what was missing: the model's own reply text.
`_extract_usage_since()` in `log_event.py` concatenates every `text`-type content block from the transcript (skipping `tool_use` blocks) alongside the usage rows it already returned.
`UserPromptSubmit` stashes the submitted prompt (`_remember_turn_prompt`/`_pop_turn_prompt`), popped by `Stop`/`StopFailure`.
A `Task` tool's `PreToolUse` stashes `tool_input.prompt` (`_remember_subagent_prompt`/`_pop_subagent_prompt`), popped by the matching `SubagentStop`.
`_report_usage()` POSTs one `agent_messages` row whenever it has non-empty `prompt_text` or `response_text`.
Skills and MCP tool calls don't get their own row - their "response" is the surrounding turn's own response, already captured.

Grafana panel 7 joins `agent_messages` onto `agent_events` on `(session_id, turn_id, agent_name)` and enables per-cell `inspect` on `prompt_text`/`response_text` (plain text modal) and `raw_payload` (JSON-view modal) - click a cell to see the full value in place.
Caveat: that join key isn't 1:1 with individual events - every row in the same turn with the same `agent_name` shows the *same* prompt/response text (the turn's, not a per-tool-call slice), while `raw_payload` stays genuinely per-row.

### MCP server (`mcp-server`)

Listens on `:8001/mcp` (FastMCP `streamable-http` transport). Two tools:

- `whatsup(hours: int = 24)` - three fixed queries (total tokens, total cost via `ASOF JOIN` against `model_pricing`, top 5 spenders). Read-only by construction - never runs arbitrary SQL from the model.
- `query(sql: str, max_rows: int = 200)` - arbitrary SQL from the model, for the `clickhouse-analyst` subagent (see `.claude/agents/clickhouse-analyst.md`) and ad hoc lookups. There's no separate read-only ClickHouse user (`docker-compose.yml` uses one shared user for ingest-api/mcp-server/grafana), so `_validate_readonly_sql()` in `server.py` is the only thing enforcing read-only: single statement, must start with `SELECT`/`WITH`, no DDL/DML keywords anywhere in the query (word-boundary matched, so it also catches them inside subqueries), no `system`/`information_schema`/`mysql` database access, no remote/file/URL/other-DB table functions (`remote`, `url`, `file`, `s3`, `mysql`, `postgresql`, etc. - these read data from outside ClickHouse entirely, a DDL/DML keyword check alone wouldn't catch them), and must reference at least one of this stack's own tables. Results are always wrapped in an outer `LIMIT` (default/max 200, hard cap 1000) so a forgotten `LIMIT` in the model's query can't return unbounded rows.

`src/server.py` exposes `app = mcp.streamable_http_app()` at module level, run via `uvicorn src.server:app` (see `mcp-server/Dockerfile`) - deliberately *not* mounted under a separate FastAPI app, since the official `mcp` SDK has a known bug there (session manager never initializes when `streamable_http_app()` is mounted as a sub-app, requests 404/507 - [modelcontextprotocol/python-sdk#1367](https://github.com/modelcontextprotocol/python-sdk/issues/1367)).
Same dev/prod split as `webhook` below: `docker-compose.yml` still `build`s `mcp-server/Dockerfile` (deps baked into the image), then bind-mounts `mcp-server/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build mcp-server`. Built and run standalone (no compose, no `--reload`), it's the same self-contained image `Dockerfile` describes.

### Full hook coverage

`SessionStart`/`SessionEnd` (`log_session.py`); everything else (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `PermissionRequest`, `PermissionDenied`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact`, `Stop`, `StopFailure`) via `log_event.py`, matcher `.*` where applicable.

**Skill attribution:** skills have no dedicated lifecycle event - a skill is just a `Skill`-tool call.
`log_event.py` tracks the most recently invoked skill per session (`_remember_turn_skill`/`_pop_turn_skill`), reset each `UserPromptSubmit`, read back when `Stop` reports usage.
Only the last skill in a turn is attributed if more than one is invoked (deliberate simplification).
MCP tool cost/token attribution (`_remember_turn_mcp_tool`/`_pop_turn_mcp_tool`, `agent_usage.mcp_tool_name`) mirrors this exactly.

**Permission wait time:** `PermissionRequest` stashes a start time keyed by `tool_use_id` (`mark_permission_request`, separate bucket from the `PreToolUse`->`PostToolUse` execution timer).
Whichever of `PreToolUse`/`PermissionDenied` fires next pops it and reports it as its own `latency_ms` - so `latency_ms` means execution duration on `PostToolUse`/`PostToolUseFailure` but permission-prompt wait time on `PreToolUse`/`PermissionDenied`.

### Frontmatter format

```
---
name: test-researcher
version: 1.0.0
description: ...
---
```

Parsed by a minimal stdlib-only line parser in `hooks/common.py` - flat `key: value` pairs only, no nested structures.

### Grafana dashboard panels

"Agents Overview", 31 panels across 6 collapsible rows, default time range `now-3h` to `now`.
Rows exist as plain `type: row` panels (classic v1 dashboard schema) for now - see "Dynamic dashboards / tabs" below for the plan to convert them into real tabs.

| Row | # | Panel | Notes |
|-----|----|--------------------------------------------------------|----------------------------------------------------|
| **Overview** | 19 | Overview stat | |
| | 30 | Tokens by user over time | per-`user_id` line, not aggregated - for spotting a single user's behavior change |
| | 31 | Cost by user over time | same shape as 30, `agent_usage` ASOF JOIN `model_pricing` |
| **Cost & Tokens** | 1-2 | Tokens by agent/version, by skill/version over time | `agent_usage`, raw per-row timestamps (not bucketed) so sparse points still connect into a line |
| | 3-4 | Cost by agent/version, by skill/version over time | `agent_usage` ASOF JOIN `model_pricing` |
| | 13-14 | Tokens / cost by MCP tool over time | `agent_usage.mcp_tool_name != ''` |
| | 16-17 | Tokens / cost by model & scope over time | scope = `subagent`/`skill`/`main agent`, derived per row via `multiIf` |
| | 22 | Cache hit rate by agent/skill/model over time | `cache_read_tokens / (cache_read_tokens + input_tokens)` |
| **Users & Adoption** | 10 | Spend by user | barchart, `agent_usage` ASOF JOIN `model_pricing` |
| | 20 | User leaderboard | tokens/cost/session duration per user |
| | 25-26 | Week-over-week cost/tokens change, by user / by agent-skill-model | fixed trailing 7d vs prior 7d, independent of the dashboard time picker |
| | 29 | Active users & sessions per day | `uniqExact(user_id)`/`uniqExact(session_id)` |
| **Reliability & Performance** | 5 | Error rate by tool_name | `PostToolUseFailure` vs `PostToolUse`, current snapshot |
| | 6 | Latency percentiles (p50/p95) by tool_name | `agent_events.latency_ms` |
| | 11 | MCP tool calls (+ p50/p95 latency) | `tool_name` starting with `mcp__` |
| | 12 | Permission prompt wait time (p50/p95) by tool_name | `event_type IN ('PreToolUse','PermissionDenied')`, `latency_ms IS NOT NULL` |
| | 15 | Top 10 slowest tool calls | ranked by `latency_ms`, not `$` - no per-call cost exists at that granularity |
| | 27-28 | Error rate / permission-denied rate over time | trend versions of panels 5/12, bucketed `toStartOfHour` |
| **Sessions & Debugging** | 7 | Full trace of selected session(s) | see "Message and tool-level text" above |
| | 18 | Call stack for selected session(s) | |
| | 8 | Top 10 most expensive sessions (+ duration) | `agent_usage` ASOF JOIN `model_pricing`, `LIMIT 10` |
| | 21 | Top 10 most expensive prompts by tokens | `agent_usage` joined to `agent_messages` on `(session_id, turn_id, agent_name)`, `prompt_text`/`response_text` inspectable |
| **Versions** | 9 | Current agent/skill versions | unfiltered - it's the reference list the version variables come from |
| | 23-24 | Agent / skill version-change impact | before-vs-after adopting the current version, transition point auto-detected from first-seen `agent_usage`/`agent_events` timestamp per version (not `registered_at` - see agent frontmatter comment in `clickhouse-analyst.md`); latency isn't included, since `agent_events` doesn't carry `agent_version`/`skill_version` on `Stop` rows |

### Dynamic dashboards / tabs

Grafana bumped from `11.2.0` to `13.1.0` in `docker-compose.yml` to get native dashboard tabs ("Dynamic dashboards", GA'd April 2026 - new v2 dashboard schema, tabs as a first-class layout option alongside rows).
The 6 rows above are the row-based grouping to convert into tabs once on 13.1.0 - do that via the Grafana UI (open the dashboard, the new editor migrates v1→v2 on load, then drag/convert rows into tabs) rather than hand-authoring the v2 JSON schema directly, since it's new enough that hand-rolling it blind is error-prone.
Known risk to watch: [grafana/grafana#119450](https://github.com/grafana/grafana/issues/119450) reported Dynamic Dashboards breaking *provisioned* dashboards on save in OSS 12.4.0 - our setup (`type: file` provider, `allowUiUpdates: true`) matches that exactly; unconfirmed whether 13.1.0 still has it.

Six template variables in order: `$agent_version`, `$skill_version`, `$mcp_tool`, `$model`, `$user_id`, `$session_id` (the session picker's own query is scoped by selected user(s), so `$user_id` must precede it).
`$model` needs no `= ''` escape hatch since `agent_usage` rows are always real model calls; same for `$user_id`/`$session_id` against `agent_events`.
`$mcp_tool`'s dropdown label strips the `mcp__` prefix but filters on the real full `tool_name`.

### Debugging hooks

Field extraction from hook payloads is best-effort and can drift across Claude Code / Codex CLI versions.
Run a hook manually with `AGENT_CLI_TRACKING_DEBUG=1` to dump the raw payload to stderr:

```bash
AGENT_CLI_TRACKING_DEBUG=1 echo '{"hook_event_name":"Stop","session_id":"test"}' | python3 .claude/hooks/log_event.py
```

Version-dependent events (`PostToolUseFailure`, `PostToolBatch`, `PermissionRequest`, `PermissionDenied`, `SubagentStart`, `PostCompact`, `StopFailure`) simply never fire on older CLI versions - safe to leave wired in `settings.json`/`hooks.json` regardless.

### Windows

Hook scripts are stdlib-only Python 3, OS-agnostic.
`.claude/settings.json` invokes them via `${AGENT_CLI_HOOK_PYTHON:-python3} ...` (POSIX parameter expansion - fine under Git Bash/WSL); if hooks run through `cmd.exe` with no `python3` on `PATH`, set `AGENT_CLI_HOOK_PYTHON` to `python` or `py -3`.
`.codex/hooks.json` invokes them via a hardcoded `python3` with no such override - on Windows without a `python3` on `PATH`, add one (e.g. a `python3.bat` shim) or edit the command in `.codex/hooks.json` directly.

## LiteLLM

A local LiteLLM gateway (`litellm` + `litellm-db` + `webhook` services in `docker-compose.yml`) sits in front of both CLIs so their traffic can be logged, and centrally billed, before it leaves the machine.
It is prototyping infrastructure for a future server-side hooks/logging system, separate from the ClickHouse tracking stack described above - `webhook` just prints the payload shape it receives, it doesn't write to ClickHouse yet.

The model names are meant to be stable regardless of what's actually billing them: `claude-sonnet-5`/`claude-haiku-4-5`/`claude-opus-4-8`/`claude-fable-5`/`gpt-5-codex`/`gpt-5` are what you put in `ANTHROPIC_MODEL`, agent/skill frontmatter `model:` fields, Codex CLI's model setting - everywhere - and that stays true whether a name is currently backed by OAuth passthrough (no Anthropic key on hand yet) or a real, centrally-held provider key added later through the admin UI.
People get a personal LiteLLM *virtual key* either way, and per-key budgets/rate-limits/model access are enforced entirely by LiteLLM - see "Admin UI: issuing a personal key" below.
`litellm-db` (Postgres) is what makes virtual keys persistent - without a database, LiteLLM either refuses to generate them or keeps them in memory only, gone on the next restart.

### Model name mapping

The whole point of picking `model_name` values up front is that agent/skill frontmatter and both CLIs' model settings reference these same names, unaware of what's actually behind them:

| Virtual name (use everywhere) | Real model                    | Backend right now                                                   |
|--------------------------------|--------------------------------|----------------------------------------------------------------------|
| `claude-sonnet-5`              | `anthropic/claude-sonnet-5`    | OAuth passthrough, `litellm/config.yaml` (no Anthropic key yet)       |
| `claude-haiku-4-5`             | `anthropic/claude-haiku-4-5`   | OAuth passthrough, `litellm/config.yaml` (no Anthropic key yet)       |
| `claude-opus-4-8`              | `anthropic/claude-opus-4-8`    | OAuth passthrough, `litellm/config.yaml` (no Anthropic key yet)       |
| `claude-fable-5`               | `anthropic/claude-fable-5`     | OAuth passthrough, `litellm/config.yaml` (no Anthropic key yet)       |
| `gpt-5-codex`                  | `openai/gpt-5-codex`           | Not defined yet - needs a real `OPENAI_API_KEY`, see "Later" below    |
| `gpt-5`                        | `openai/gpt-5`                 | Not defined yet - needs a real `OPENAI_API_KEY`, see "Later" below    |

This table is the file-based (git-tracked) half of the mapping, and it's enough on its own for Claude-only skills/agents shared across sessions - no admin UI setup required beyond issuing personal keys.

It stops being enough the day a skill/agent's frontmatter needs to resolve to *different* real models depending on which CLI runs it (e.g. Codex should hit `gpt-5-codex` for a name that means "the good model", while Claude Code should hit `claude-sonnet-5` for that exact same name) - `model_name` in `config.yaml` is a single flat namespace, it can't branch on which CLI asked.
That branching is what LiteLLM's **Team/Key Model Aliases** are for: a Team (or an individual key) can remap an alias to a different real `model_name`, so the same alias resolves differently depending on which key made the call.
Unlike everything above, model aliases are **not** expressible in `config.yaml` - they're Team/Key configuration, which only exists once created through `/ui` or the API, persisted in `litellm-db`.
There's no reason to set this up before `gpt-5-codex`/`gpt-5` actually exist (see "Later" below) - until then, a Team alias would just point at a model that doesn't work yet.
Once it's needed: **Teams** → create e.g. `claude-users` with Model Alias `SHARED_NAME → claude-sonnet-5`, and `codex-users` with `SHARED_NAME → gpt-5-codex`; issue personal keys scoped to the matching team.

### Starting it

```bash
docker compose up -d --build litellm litellm-db webhook
docker compose logs -f litellm
```

First boot takes a bit longer than usual - LiteLLM runs its Postgres schema migration against `litellm-db` before it starts serving.

### Right now: no Anthropic/OpenAI key yet

`claude-sonnet-5`/`claude-haiku-4-5`/`claude-opus-4-8`/`claude-fable-5` are defined in `litellm/config.yaml`'s `model_list` with no `api_key` - `model_group_settings.forward_client_headers_to_llm_api` forwards the caller's own `claude login` subscription token straight to Anthropic instead.
`gpt-5-codex`/`gpt-5` have no equivalent (OpenAI has nothing like Anthropic's OAuth passthrough), so they simply don't exist yet - add them once a real `OPENAI_API_KEY` shows up, see "Later" below.

### Admin UI: issuing a personal key

1. Open http://localhost:4000/ui and log in with `admin` / `LITELLM_MASTER_KEY`.
2. **Keys** → **Create New Key**.
3. Restrict `Models` to whichever of `claude-sonnet-5`/`claude-haiku-4-5`/`claude-opus-4-8`/`claude-fable-5` that person should have, and set `Max Budget` / `Rate Limits` as needed.
4. Give the generated `sk-...` key to that person.

### Routing Claude Code through it

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_MODEL="claude-sonnet-5"                    # or claude-haiku-4-5/claude-opus-4-8/claude-fable-5 - same names everywhere, including agent/skill frontmatter `model:`
export LITELLM_MASTER_KEY="sk-anything-you-like"
export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $LITELLM_MASTER_KEY"  # the personal LiteLLM virtual key from the step above
```

Then `claude login` (subscription OAuth, Pro/Max/Team) as usual.

`ANTHROPIC_CUSTOM_HEADERS` is required even though nothing else guards these routes: without a distinct header proving something *else* authenticated to LiteLLM, it can't tell the incoming `Authorization` (the subscription token) apart from its own auth and strips it before forwarding - Anthropic then replies `x-api-key header is required` (see [BerriAI/litellm#19618](https://github.com/BerriAI/litellm/issues/19618)).
`general_settings.litellm_key_header_name: x-litellm-api-key` in `litellm/config.yaml` is what makes LiteLLM read the virtual key from that header, checking it against the budget/model/rate-limit rules on the key, independently of whatever gets forwarded to Anthropic.

### Later: checklist for when a real Anthropic/OpenAI key shows up

Do these in order - skipping the `config.yaml` cleanup step is what leads to the undefined "same `model_name` in both places" state warned about below.

**When an Anthropic key arrives:**

- [ ] `/ui` → **Models** → **Add New Model** → `Model Name: claude-sonnet-5` (the *same* name, not a new one) → `LiteLLM Model Name: anthropic/claude-sonnet-5` → paste the real key in `API Key`. Repeat for `claude-haiku-4-5`, `claude-opus-4-8`, `claude-fable-5`.
- [ ] In `litellm/config.yaml`: delete all four Claude entries from `model_list`, and remove their four names from `model_group_settings.forward_client_headers_to_llm_api` (delete the whole line if OpenAI isn't wired up yet either).
- [ ] `docker compose restart litellm` (or `up -d` again - no image/volume changes needed).
- [ ] Update the "Model name mapping" table above: Claude rows go from "OAuth passthrough, `litellm/config.yaml`" to "Central org key, admin UI".
- [ ] Drop `claude login` from "Routing Claude Code through it" above.
- [ ] `ANTHROPIC_MODEL`/frontmatter `model:` values, and everyone's personal virtual keys, need **no changes at all** - that's the entire point of the stable naming.

**When an OpenAI key arrives:**

- [ ] `/ui` → **Models** → **Add New Model** → `Model Name: gpt-5-codex` → `LiteLLM Model Name: openai/gpt-5-codex` → paste the real key. Repeat for `gpt-5`.
- [ ] Update the "Model name mapping" table above: the two OpenAI rows go from "Not defined yet" to "Central org key, admin UI".
- [ ] Issue personal virtual keys for Codex users (**Keys** → **Create New Key**, `Models` restricted to `gpt-5-codex`/`gpt-5`) and wire up "Routing Codex CLI through it" below for real.
- [ ] Only *now* does the cross-CLI shared-name problem from "Model name mapping" above become real - if a skill/agent needs one frontmatter `model:` value to mean `claude-sonnet-5` under Claude Code and `gpt-5-codex` under Codex, that's the point to set up Team Model Aliases (see above), not before.

Don't leave a `model_list` entry and a UI/DB-managed model sharing one `model_name` at the same time - that combination is undefined behavior, not a valid transition state to linger in.
`general_settings.store_model_in_db: true` is what lets the UI persist model definitions to `litellm-db` instead of requiring a `model_list` entry + restart - that's also what makes rotating a key later (or rolling `claude-sonnet-5` onto a `claude-sonnet-6` release) a UI edit, not a file edit.

### Routing Codex CLI through it

Once `gpt-5-codex`/`gpt-5` exist (see "Later" above - Codex has no subscription-passthrough option, so this can't happen before a real `OPENAI_API_KEY` is added), issue a personal virtual key the same way (**Keys** → **Create New Key**, `Models` restricted to `gpt-5-codex`/`gpt-5`), and give it to whoever needs Codex access - never the real OpenAI key.
Point Codex CLI's own base-URL setting at `http://localhost:4000`, its API key setting at that virtual key, and its model at `gpt-5-codex` or `gpt-5`; consult Codex CLI's own docs for the exact config keys, since this project doesn't wrap Codex the way it does Claude Code.

### Inspecting captured traffic

`webhook` pretty-prints each request's full `StandardLoggingPayload` (messages, response, usage, cost, timing) to its own container logs - `docker compose logs -f webhook` while driving a session through either CLI.
It listens on host port `8010` (container port `8000`, to avoid clashing with `ingest-api`'s own `8000`), reachable inside the `agent-tracking` Docker network as `webhook:8000`.

Every hit also lands as its own timestamped JSON file under `webhook/captures/` on the host (bind-mounted, not a Docker volume - `ls webhook/captures/` works directly, no `docker exec` needed), raw as received.
`log_format: json_array` in `litellm/config.yaml` means each file is usually a list of `StandardLoggingPayload` objects, not a single one.
This directory is gitignored - it's real prompt/response content, not something to commit.
`docker-compose.yml` still `build`s `webhook/Dockerfile` (deps baked into the image), then bind-mounts `webhook/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build webhook`. `captures/` is mounted separately (it's runtime output, not source) so it lands on the host either way.
Built and run standalone (no compose, no `--reload`, no bind mounts) - `docker build -t webhook . && docker run -p 8000:8000 webhook` - it's the same self-contained image `Dockerfile` describes.

### Known gaps

Nothing here writes into ClickHouse yet - `webhook`'s route (`/api/v1/metrics`) doesn't exist on `ingest-api`, which only exposes `/ingest/event`, `/ingest/usage`, and `/ingest/message`.
Wiring the two together means either adding a matching route to `ingest-api` that maps LiteLLM's payload shape onto those tables, or pointing `WEBHOOK_URL` in `docker-compose.yml` straight at one of the existing `/ingest/*` routes once the payload shapes are reconciled.
`litellm`'s image is pinned to `ghcr.io/berriai/litellm:main-latest`, which moves under you - pin it to a specific tag before this leaves local prototyping.
`litellm-db`'s Postgres has no backup story - it's a local Docker volume (`litellm-db-data`), fine for prototyping, not for a real deployment's virtual keys/budgets.
The `claude-sonnet-oauth`/`claude-haiku-oauth` routes depend on LiteLLM's own OAuth-forwarding code, which has had real bugs in this exact area - besides #19618 (already worked around via `litellm_key_header_name`), there's an open report ([BerriAI/litellm#29190](https://github.com/BerriAI/litellm/issues/29190)) of a 401 when a request carries both a subscription `Authorization` token and a virtual key at once, since LiteLLM may try to look the OAuth token up in its own key table. If these two routes start 401ing unexpectedly, that issue - and the image's exact version - is the first thing to check.
