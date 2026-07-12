# Agent Tracking Stack

Local stack for tracking cost and efficiency of AI coding agents (Claude Code and Codex CLI), with full call-chain tracing - agent, skill, and MCP tool usage are all tracked, not just top-level agent activity.
See `AGENTS.md` for architecture and schema details.

## Overview

### How data flows

1. Every LLM call from the CLI (Claude Code or Codex) is routed through a local LiteLLM proxy on `:4000` instead of hitting Anthropic/OpenAI directly.
2. LiteLLM's `generic_api` callback POSTs the full `StandardLoggingPayload` for each call to `webhook` on `:8010`.
3. `webhook` captures the raw body to `webhook/captures/` (for offline inspection), parses it, and writes rows straight into ClickHouse on `:8123` - nothing is buffered or batched beyond that single request. Agent/skill invocations are recovered from the payload itself (see `AGENTS.md`), not from a CLI-side hook.
4. Grafana on `:3000` queries ClickHouse directly for every panel; there's no caching layer, so a dashboard refresh always reflects current table state.
5. Reads go the other way: `webhook` is write-only, so a CLI session reads data back out (e.g. `/whatsup` in Claude Code) via the `mcp-server` MCP server on `:8001`, registered in `.mcp.json`.

## Getting started

### Start the stack

```bash
docker compose up -d --build
```

### Wait until it's healthy

```bash
docker compose ps
```

`webhook`, `mcp-server`, and `grafana` won't start until `clickhouse` shows `healthy` (all three `depends_on: condition: service_healthy`).
Re-run the command above until all services show up and `clickhouse` is no longer `starting`/`unhealthy`.

### Open Grafana

http://localhost:3000/d/agents-overview/agents-overview

Anonymous viewer access is enabled by default - no login needed.

## Usage

### Test agents and skills

Subagents and skills are a Claude Code concept - Codex has no equivalent, so this test flow is Claude Code only.
Open a Claude Code session **in this project directory**, routed through the local LiteLLM proxy (see "LiteLLM" below), then ask for `test-researcher`/`test-coder`/`test-summarizer`/`test-linter` by name - point at a randomly chosen file in the project root rather than a fixed one, to keep token usage low:

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
| `webhook`/`grafana` stuck in `Created`, never start        | Their `depends_on: condition: service_healthy` is blocking on the `clickhouse` healthcheck. Run `docker compose ps` - if `clickhouse` shows `unhealthy`, check `docker inspect receipt-goblin-clickhouse --format '{{json .State.Health}}'` for the actual healthcheck error, and confirm ClickHouse itself is fine with `docker exec receipt-goblin-clickhouse clickhouse-client --user default --password "$CLICKHOUSE_PASSWORD" --query "SELECT 1"`. The image ships `wget`, not `curl` - the healthcheck uses `wget --spider`. |
| `webhook` can't reach ClickHouse (once running)             | Check `docker compose logs clickhouse` / `docker compose logs webhook` for the actual connection error; `webhook`'s `/health` route runs `SELECT 1` against ClickHouse and reports the exception if it fails. |
| `/whatsup` fails or times out                                  | Confirm `mcp-server` is `healthy`/running (`docker compose ps`) and reachable at `http://localhost:8001/mcp`; check `docker compose logs mcp-server`. Claude Code only picks up `.mcp.json` changes on the next session start. |
| No rows landing in ClickHouse at all                          | Confirm the CLI is actually routed through LiteLLM (`ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_HEADERS` set, see "Routing Claude Code through it" below), then check `docker compose logs litellm` for callback errors and `docker compose logs webhook` for ingest exceptions - `ingest_standard_logging_payload` never raises out to LiteLLM, it only logs, so a parsing bug shows up as a log line, not a retry loop. |
| Dashboard edits stop saving after a Grafana upgrade            | Grafana 13.1.0 (bumped from 11.2.0 for tabs support - see "Dynamic dashboards" below) had a known OSS 12.4.0 bug where "Dynamic Dashboards" broke *provisioned* dashboards on save ([grafana/grafana#119450](https://github.com/grafana/grafana/issues/119450)) - our exact setup (`type: file` provider, `allowUiUpdates: true` in `grafana/provisioning/dashboards/dashboard.yml`). Unconfirmed whether 13.1.0 still has it; if UI edits silently fail to persist, that's the first thing to check. |
| Grafana stops responding after a few clicks/panel loads (no crash in browser) | Check `docker inspect receipt-goblin-grafana --format '{{.State.OOMKilled}} {{.State.ExitCode}}'` - Grafana 13.1.0 is meaningfully heavier than 11.2.0 (alerting scheduler, zanzana authz, bleve search indexing, app registry, background plugin auto-updater) and hit the old `mem_limit: 512m` within a couple of dashboard interactions (`OOMKilled=true`, exit 137). Bumped to `1g` in `docker-compose.yml` alongside the 13.1.0 upgrade; there's a `restart: always` policy, so an OOM-killed container comes back on its own - raise the limit further if it recurs. |
| No `agent_name`/`skill_name` on events                  | Recovered from the LiteLLM payload itself, not a CLI-side hook - see `AGENTS.md` (`_agent_invocations_from_messages`/`_skill_name_from_last_turn` in `clickhouse_ingest.py`). A subagent's own rows only resolve `agent_name` once the orchestrator's `Agent` tool_use/tool_result pair has itself been ingested and upserted into `agent_invocations` - a subagent call that reaches `webhook` before that happens will have `agent_invocation_id` set but blank `agent_name`. |
| Grafana panel shows a query error                             | The `grafana-clickhouse-datasource` plugin's query JSON shape has changed across versions; open the panel in edit mode - the SQL in `rawSql` is otherwise plain, portable ClickHouse SQL. |
| Duplicate agent/skill registry rows                             | Expected - `ReplacingMergeTree` keyed on `(name, version)`. Re-registering the same version replaces it; bumping `version` in frontmatter creates a new row and preserves history. |
| Claude Code via the LiteLLM proxy fails with `x-api-key header is required` | Missing `ANTHROPIC_CUSTOM_HEADERS`, or `LITELLM_MASTER_KEY` isn't set - see "Routing Claude Code through it" under "LiteLLM" below. |

## Reference

Everything below is background/design detail, not needed day-to-day - see `AGENTS.md` for the rules that actually constrain how you edit this repo.

### Configuration

| Variable                 | Default                                   | Used by                                                                                                     |
|--------------------------|--------------------------------------------|----------------------------------------------------------------------------------------------------------|
| `CLICKHOUSE_DATABASE`    | `default`                                 | clickhouse, webhook, mcp-server, grafana                                                                 |
| `CLICKHOUSE_USER`        | `default`                                 | clickhouse, webhook, mcp-server, grafana                                                                 |
| `CLICKHOUSE_PASSWORD`    | `changeme`                                | clickhouse, webhook, mcp-server, grafana                                                                 |
| `CLICKHOUSE_HOST`        | `clickhouse`                              | webhook, mcp-server, grafana                                                                             |
| `CLICKHOUSE_PORT`        | `8123`                                    | webhook, mcp-server, grafana                                                                             |
| `CLICKHOUSE_HTTP_PORT`   | `8123`                                    | host port mapping for clickhouse's HTTP interface                                                           |
| `CLICKHOUSE_NATIVE_PORT` | `9000`                                    | host port mapping for clickhouse's native protocol                                                          |
| `MCP_SERVER_PORT`        | `8001`                                    | host port mapping for mcp-server                                                                         |
| `GRAFANA_PORT`           | `3000`                                    | host port mapping for grafana                                                                               |
| `WEBHOOK_PORT`           | `8010`                                    | host port mapping for webhook                                                                           |
| `LITELLM_PORT`           | `4000`                                    | host port mapping for litellm                                                                               |
| `LITELLM_IMAGE`          | `ghcr.io/berriai/litellm:main-latest`     | litellm - pin this to a released tag before sharing the stack, see "LiteLLM" below                          |
| `WEBHOOK_URL`            | `http://webhook:8000/api/v1/metrics` | litellm - where it POSTs the `StandardLoggingPayload` for each call                                         |
| `LITELLM_MASTER_KEY`     | required, no default                      | litellm - admin credential for `/ui` and `/key/generate`; real Anthropic/OpenAI keys and per-person virtual keys are managed through the UI instead, see "LiteLLM" below |
| `LITELLM_DB_PASSWORD`    | `changeme`                                | litellm, litellm-db - Postgres password for LiteLLM's own virtual-keys/budgets database                    |

`CLICKHOUSE_PASSWORD` must stay non-empty: ClickHouse restricts the `default` user to localhost-only access whenever user/password are unset, which breaks the other containers connecting over the Docker network.
`*_PORT` variables only change the **host** side of each port mapping - the container-internal port stays fixed, so services keep reaching each other over the `receipt-goblin` Docker network regardless of what you set these to.

Each service also has a `mem_limit`: `clickhouse` 2g (paired with `clickhouse/config.d/memory.xml`'s 0.85 ratio so it respects the cgroup limit instead of trying to use host RAM), `grafana` 1g, `webhook`/`mcp-server` 256m each, `litellm-db` 256m.

### Schema

| Table            | Purpose                                                                        |
|-------------------|----------------------------------------------------------------------------------|
| `agent_registry` / `skill_registry` | name/version/description/source_file, `ReplacingMergeTree ORDER BY (name, version)`. |
| `agent_events`    | One row per LiteLLM call, full `raw_payload` JSON (the `StandardLoggingPayload`, minus `messages`). |
| `agent_usage`     | One row per model call: tokens, plus `cost`/`input_cost`/`output_cost` straight from LiteLLM's own `response_cost`/`cost_breakdown` - cache-pricing-aware and never derived locally (a manually-maintained `model_pricing` table + `ASOF JOIN` used to compute cost instead, and was removed after it was found to overcount by several times whenever prompt caching was in play). |
| `agent_messages`  | One row per call, holding `prompt_text`/`response_text`.                        |

### Per-request signals on `agent_usage`

Beyond token counts, each usage row also carries a few fields read straight off LiteLLM's `StandardLoggingPayload`, added because token/cost alone can't tell a normal completion from a truncated or refused one, or show which cache tier actually got written:

| Column | Source | Why |
|---|---|---|
| `stop_reason` | `response.choices[0].finish_reason` | `end_turn` vs `max_tokens` vs `refusal` vs `tool_use` - a `max_tokens` row means the reply got cut off, not just that it was expensive. |
| `cache_creation_1h_tokens`, `cache_creation_5m_tokens` | `usage.prompt_tokens_details.cache_creation_token_details.ephemeral_{1h,5m}_input_tokens` | 1h and 5m ephemeral cache writes are priced differently; `cache_creation_tokens` stays their sum for the existing cost/token panels, these two are the breakdown. |
| `service_tier`, `speed`, `web_search_requests`, `web_fetch_requests` | none - always blank/0 on rows written by `webhook` | No equivalent field exists in LiteLLM's normalized payload (checked directly against real captures). Historical rows from the retired transcript-hook pipeline may still carry real values for these; new rows won't. Claude Code's own `WebSearch`/`WebFetch` tools show up as ordinary `tool_use` blocks in `messages`, not as Anthropic's server-side built-in tools, so they aren't recoverable from this source either. |

There's no request-level "reasoning effort" field anywhere in the payload (checked - `grep`ed real captures for `effort`/`reasoning_effort`/`budget`, none exist).
Model choice (`agent_usage.model`) is the closest proxy: cheaper/faster models are already picked per-agent via `model:` in an agent's frontmatter (e.g. `.claude/agents/test-coder.md` uses `claude-haiku-4-5`), and panels 16/17 already break cost/tokens down by model.

### Message-level text

`agent_events.raw_payload` carries the full `StandardLoggingPayload` (minus `messages`, which is the ever-growing full conversation history and already on disk verbatim in `webhook/captures/*.json`).
`agent_messages` adds what's missing from that: the last user message's text and the model's own reply text for that call, via `_last_user_text()`/`_flatten_content()` in `clickhouse_ingest.py`.
A row is only written when at least one of `prompt_text`/`response_text` is non-empty.

### MCP server (`mcp-server`)

Listens on `:8001/mcp` (FastMCP `streamable-http` transport). Two tools:

- `whatsup(hours: int = 24)` - three fixed queries (total tokens, total cost from `agent_usage.cost`, top 5 spenders). Read-only by construction - never runs arbitrary SQL from the model.
- `query(sql: str, max_rows: int = 200)` - arbitrary SQL from the model, for the `clickhouse-analyst` subagent (see `.claude/agents/clickhouse-analyst.md`) and ad hoc lookups. There's no separate read-only ClickHouse user (`docker-compose.yml` uses one shared user for webhook/mcp-server/grafana), so `_validate_readonly_sql()` in `server.py` is the only thing enforcing read-only: single statement, must start with `SELECT`/`WITH`, no DDL/DML keywords anywhere in the query (word-boundary matched, so it also catches them inside subqueries), no `system`/`information_schema`/`mysql` database access, no remote/file/URL/other-DB table functions (`remote`, `url`, `file`, `s3`, `mysql`, `postgresql`, etc. - these read data from outside ClickHouse entirely, a DDL/DML keyword check alone wouldn't catch them), and must reference at least one of this stack's own tables. Results are always wrapped in an outer `LIMIT` (default/max 200, hard cap 1000) so a forgotten `LIMIT` in the model's query can't return unbounded rows.

`src/server.py` exposes `app = mcp.streamable_http_app()` at module level, run via `uvicorn src.server:app` (see `mcp-server/Dockerfile`) - deliberately *not* mounted under a separate FastAPI app, since the official `mcp` SDK has a known bug there (session manager never initializes when `streamable_http_app()` is mounted as a sub-app, requests 404/507 - [modelcontextprotocol/python-sdk#1367](https://github.com/modelcontextprotocol/python-sdk/issues/1367)).
Same dev/prod split as `webhook` below: `docker-compose.yml` still `build`s `mcp-server/Dockerfile` (deps baked into the image), then bind-mounts `mcp-server/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build mcp-server`. Built and run standalone (no compose, no `--reload`), it's the same self-contained image `Dockerfile` describes.

### Frontmatter format

```
---
name: test-researcher
version: 1.0.0
description: ...
---
```

`agent_registry`/`skill_registry` are no longer populated automatically now that the transcript-reading hooks are retired - see "Known gaps" under "LiteLLM" below.

### Grafana dashboard panels

"Agents Overview", 31 panels across 6 collapsible rows, default time range `now-3h` to `now`.
Rows exist as plain `type: row` panels (classic v1 dashboard schema) for now - see "Dynamic dashboards / tabs" below for the plan to convert them into real tabs.

| Row | # | Panel | Notes |
|-----|----|--------------------------------------------------------|----------------------------------------------------|
| **Overview** | 19 | Overview stat | |
| | 30 | Tokens by user over time | per-`user_id` line, not aggregated - for spotting a single user's behavior change |
| | 31 | Cost by user over time | same shape as 30, `agent_usage.cost` |
| **Cost & Tokens** | 1-2 | Tokens by agent/version, by skill/version over time | `agent_usage`, raw per-row timestamps (not bucketed) so sparse points still connect into a line |
| | 3-4 | Cost by agent/version, by skill/version over time | `agent_usage.input_cost`/`output_cost` |
| | 38-39 | Tokens / cost by command over time | `agent_usage.command_name != ''` - the slash command that triggered the call chain, recovered from the `<command-name>` tag Claude Code injects into the triggering message (see `AGENTS.md`); always unversioned by design |
| | 13-14 | Tokens / cost by MCP tool over time | `agent_usage.mcp_tool_name != ''` |
| | 16-17 | Tokens / cost by model & scope over time | scope = `subagent`/`skill`/`main agent`, derived per row via `multiIf` |
| | 22 | Cache hit rate by agent/skill/model over time | `cache_read_tokens / (cache_read_tokens + input_tokens)` |
| **Users & Adoption** | 10 | Spend by user | barchart, `agent_usage.cost` |
| | 20 | User leaderboard | tokens/cost/session duration per user |
| | 25-26 | Week-over-week cost/tokens change, by user / by agent-skill-model | fixed trailing 7d vs prior 7d, independent of the dashboard time picker |
| | 29 | Active users & sessions per day | `uniqExact(user_id)`/`uniqExact(session_id)` |
| **Reliability & Performance** | 5 | Error rate by tool_name | `status = 'failure'` vs `'success'`, current snapshot |
| | 6 | Latency percentiles (p50/p95) by tool_name | `agent_events.latency_ms`, `status = 'success'` |
| | 11 | MCP tool calls (+ p50/p95 latency) | `tool_name` starting with `mcp__` |
| | 12 | Permission prompt wait time (p50/p95) by tool_name | always empty - permission prompts are a CLI-local interaction that never reaches LiteLLM, so there's no event to log; needs a future client-side hook |
| | 15 | Top 10 slowest tool calls | ranked by `latency_ms`, not `$` - no per-call cost exists at that granularity |
| | 27-28 | Error rate / permission-denied rate over time | trend version of panel 5, bucketed `toStartOfHour`; 28 shares panel 12's gap (always empty) |
| **Sessions & Debugging** | 7 | Full trace of selected session(s) | see "Message and tool-level text" above |
| | 18 | Call stack for selected session(s) | one row per LiteLLM call (no turn_id/sequence_id granularity from this source), ordered by timestamp, annotated with tool/agent/skill/command context and per-call tokens/cost |
| | 8 | Top 10 most expensive sessions (+ duration) | `agent_usage.cost`, `LIMIT 10` |
| | 21 | Top 10 most expensive prompts by tokens | `agent_usage` joined to `agent_messages` on `(session_id, turn_id, agent_name)`, `prompt_text`/`response_text` inspectable |
| **Versions** | 9 | Current agent/skill versions | unfiltered - it's the reference list the version variables come from; always empty until `agent_registry`/`skill_registry` get a writer again (see "Known gaps") |
| | 23-24 | Agent / skill version-change impact | before-vs-after adopting the current version, transition point auto-detected from first-seen `agent_usage`/`agent_events` timestamp per version (not `registered_at` - see agent frontmatter comment in `clickhouse-analyst.md`); empty until an agent/skill has 2+ distinct versions observed |

### Dynamic dashboards / tabs

Grafana bumped from `11.2.0` to `13.1.0` in `docker-compose.yml` to get native dashboard tabs ("Dynamic dashboards", GA'd April 2026 - new v2 dashboard schema, tabs as a first-class layout option alongside rows).
The 6 rows above are the row-based grouping to convert into tabs once on 13.1.0 - do that via the Grafana UI (open the dashboard, the new editor migrates v1→v2 on load, then drag/convert rows into tabs) rather than hand-authoring the v2 JSON schema directly, since it's new enough that hand-rolling it blind is error-prone.
Known risk to watch: [grafana/grafana#119450](https://github.com/grafana/grafana/issues/119450) reported Dynamic Dashboards breaking *provisioned* dashboards on save in OSS 12.4.0 - our setup (`type: file` provider, `allowUiUpdates: true`) matches that exactly; unconfirmed whether 13.1.0 still has it.

Seven template variables in order: `$agent_name`, `$skill_name`, `$command_name`, `$mcp_tool`, `$model`, `$user_id`, `$session_id` (the session picker's own query is scoped by selected user(s), so `$user_id` must precede it).
`$model` needs no `= ''` escape hatch since `agent_usage` rows are always real model calls; same for `$user_id`/`$session_id` against `agent_events`.
`$mcp_tool`'s dropdown label strips the `mcp__` prefix but filters on the real full `tool_name`.

### Debugging ingestion

Field extraction from the LiteLLM payload is best-effort and can drift across LiteLLM versions.
`docker compose logs -f webhook` shows one log line per exception raised by `ingest_standard_logging_payload` (it never re-raises, so a parsing bug never breaks LiteLLM's ack); every raw POST body also lands verbatim under `webhook/captures/` for offline replay (see "Inspecting captured traffic" below).

## LiteLLM

A local LiteLLM gateway (`litellm` + `litellm-db` + `webhook` services in `docker-compose.yml`) sits in front of both CLIs so their traffic can be logged, and centrally billed, before it leaves the machine.
This gateway *is* how the ClickHouse tracking stack described above gets its data now - `webhook` is the only ingestion path (see "How data flows" above).

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

`webhook` logs one line per ingested payload (or per exception, see "Debugging ingestion" above) - `docker compose logs -f webhook` while driving a session through either CLI.
It listens on host port `8010` (container port `8000`), reachable inside the `receipt-goblin` Docker network as `webhook:8000`.

Every hit also lands as its own timestamped JSON file under `webhook/captures/` on the host (bind-mounted, not a Docker volume - `ls webhook/captures/` works directly, no `docker exec` needed), raw as received.
`log_format: json_array` in `litellm/config.yaml` means each file is usually a list of `StandardLoggingPayload` objects, not a single one.
This directory is gitignored - it's real prompt/response content, not something to commit.
`docker-compose.yml` still `build`s `webhook/Dockerfile` (deps baked into the image), then bind-mounts `webhook/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build webhook`. `captures/` is mounted separately (it's runtime output, not source) so it lands on the host either way.
Built and run standalone (no compose, no `--reload`, no bind mounts) - `docker build -t webhook . && docker run -p 8000:8000 webhook` - it's the same self-contained image `Dockerfile` describes.

### Known gaps

`agent_registry`/`skill_registry` are no longer populated automatically - the hook that read `.claude/agents/*.md`/`.claude/skills/*/SKILL.md` frontmatter and registered them (`register_agents.py`) was retired along with the rest of the transcript-hook pipeline, and nothing in the `webhook` pipeline replaces it yet (agent/skill *names* are recovered from the LiteLLM payload - see `AGENTS.md` - but not their `version`/`description`/`source_file`).
`agent_version` is populated for agents (Subagent frontmatter `name:` doubles as the invocation identifier, so it's named `<name>_v<version>` and split back apart on ingest - see `AGENTS.md`). `skill_version` stays always blank though - Skills are identified by their *directory name*, not their frontmatter `name:` (unlike Subagents), so the same trick doesn't carry over; renaming a skill's directory to embed a version would also change its literal `/<name>` slash command, which isn't worth it. `command_name` (the stable, deliberately unversioned slash-command entry point) exists precisely to route around this - see the Commands panels above.
`litellm`'s image is pinned to `ghcr.io/berriai/litellm:main-latest`, which moves under you - pin it to a specific tag before this leaves local prototyping.
`litellm-db`'s Postgres has no backup story - it's a local Docker volume (`litellm-db-data`), fine for prototyping, not for a real deployment's virtual keys/budgets.
The `claude-sonnet-oauth`/`claude-haiku-oauth` routes depend on LiteLLM's own OAuth-forwarding code, which has had real bugs in this exact area - besides #19618 (already worked around via `litellm_key_header_name`), there's an open report ([BerriAI/litellm#29190](https://github.com/BerriAI/litellm/issues/29190)) of a 401 when a request carries both a subscription `Authorization` token and a virtual key at once, since LiteLLM may try to look the OAuth token up in its own key table. If these two routes start 401ing unexpectedly, that issue - and the image's exact version - is the first thing to check.
