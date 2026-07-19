# Agent Tracking Stack

## Minimal resource requirements

Memory: 8 GiB. CPU: 4.

Local stack for tracking cost and efficiency of AI coding agents (Claude Code and Codex CLI), with full call-chain tracing - agent, skill, and MCP tool usage are all tracked, not just top-level agent activity.
See `AGENTS.md` for architecture and schema details.

## Overview

### How data flows

1. Every LLM call from the CLI (Claude Code or Codex) is routed through a local LiteLLM proxy on `:4000` instead of hitting Anthropic/OpenAI directly.
2. LiteLLM's `generic_api` callback POSTs the full `StandardLoggingPayload` for each call to `webhook` on `:8010`.
3. `webhook` computes a compact event from the raw body (no ClickHouse access - see `AGENTS.md`) and pushes it onto a `redis` queue; optionally (`CAPTURE_ENABLED`, off by default) it also captures the raw body to `services/webhook/captures/` for offline inspection. Agent/skill invocations are recovered from the payload itself, not from a CLI-side hook.
4. `webhook-worker` drains that queue in batches and is the only thing that actually writes to ClickHouse on `:8123` for this traffic - a few large inserts instead of one per request, so ClickHouse isn't hit directly by request volume (see "Why a queue in front of ClickHouse" in `AGENTS.md`).
5. Grafana on `:3000` queries ClickHouse directly for every panel; there's no caching layer, so a dashboard refresh always reflects current table state.
6. Reads go the other way: `webhook`/`webhook-worker` are write-only, so a CLI session reads data back out (e.g. `/whatsup` in Claude Code) via the `mcp-server` MCP server on `:8001`, registered in `.mcp.json`.

## Getting started

### Environment variables

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

| Variable                  | Required? | What it's for                                                                                                                                                                                                                                                                           |
| ------------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CLICKHOUSE_USER`         | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |
| `CLICKHOUSE_PASSWORD`     | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |
| `CLICKHOUSE_DATABASE`     | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |
| `LITELLM_MASTER_KEY`      | required  | litellm - admin credential for `/ui` and `/key/generate`; real Anthropic/OpenAI keys and per-person virtual keys are managed through the UI instead, see "LiteLLM" below. Also used by webhook to call LiteLLM's `/key/info` when verifying `hooks/report_git_branch.py`'s virtual key. |
| `LITELLM_DB_PASSWORD`     | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |

Everything else in `docker-compose.yml` (ports, ClickHouse host, etc.) has a sane default - you only need to touch `.env` for the rows above; see "Configuration" under "Reference" below for the full list.
Remote model sources (Ollama, a reranker, etc.) aren't `.env` variables at all - see "Remote model sources" under "LiteLLM" below.
Your personal LiteLLM key does **not** go in `.env` at all - see the next step.

### Start the stack

```bash
make start
```

### Wait until it's healthy

```bash
make status
```

`make status` runs a plain `docker ps` (every container on the host, not scoped to this stack) - look for the `receipt-goblin-*` containers among the results.
`mcp-server` and `grafana` won't start until `clickhouse` shows `healthy`; `webhook` and `webhook-worker` also wait on `redis` (all `depends_on: condition: service_healthy`).
Re-run the command above until all `receipt-goblin-*` containers show up and `clickhouse`/`redis` are no longer `starting`/`unhealthy`.

### Logs

```bash
make logs
```

### Issue yourself a personal key and route a coding agent through the proxy

1. Open http://localhost:4000/ui and log in with `admin` / your `LITELLM_MASTER_KEY`.
2. **Keys** → **Create New Key** → restrict `Models` to whichever names the agent(s) you use need → copy the generated `sk-...` key.
3. Run `make env`, copy its output, replace the `<virtual key>` placeholders with the key from step 2, and paste the result into `~/.zshrc`/`~/.bashrc` so every new shell picks it up - see "Routing Claude Code through it" / "Routing Codex CLI through it" under "LiteLLM" below for what it exports.

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
make stop
```

To also delete the ClickHouse data volume (next `up` re-applies `schema.sql` from scratch), run `docker compose down -v` directly instead of `make stop` - `make`'s `stop` target doesn't forward extra flags to the underlying `docker compose down`.

## Troubleshooting

| Symptom                                                                       | Likely cause / fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `webhook`/`grafana` stuck in `Created`, never start                           | Their `depends_on: condition: service_healthy` is blocking on the `clickhouse` healthcheck (`webhook`/`webhook-worker` also wait on `redis`). Run `docker compose ps` - if `clickhouse` shows `unhealthy`, check `docker inspect receipt-goblin-clickhouse --format '{{json .State.Health}}'` for the actual healthcheck error, and confirm ClickHouse itself is fine with `docker exec receipt-goblin-clickhouse clickhouse-client --user default --password "$CLICKHOUSE_PASSWORD" --query "SELECT 1"`. The image ships `wget`, not `curl` - the healthcheck uses `wget --spider`.                                                                                  |
| `webhook` can't reach ClickHouse (once running)                               | `webhook` itself doesn't talk to ClickHouse for `/api/v1/metrics` anymore (see "How data flows" above) - check `docker compose logs redis` / `docker compose logs webhook-worker` for the actual connection error instead. `webhook`'s `/health` route still runs `SELECT 1` against ClickHouse plus a Redis `PING` and reports whichever exception hit first.                                                                                                                                                                                                                                                                                                        |
| `/whatsup` fails or times out                                                 | Confirm `mcp-server` is `healthy`/running (`docker compose ps`) and reachable at `http://localhost:8001/mcp`; check `docker compose logs mcp-server`. Claude Code only picks up `.mcp.json` changes on the next session start.                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| No rows landing in ClickHouse at all                                          | Confirm the CLI is actually routed through LiteLLM (`ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_HEADERS` set, see "Routing Claude Code through it" below), then check `docker compose logs litellm` for callback errors, `docker compose logs webhook` for enqueue exceptions, and `docker compose logs webhook-worker` for batch-insert exceptions - `ingest_events_batch` never raises out of the worker loop, it only logs, so a parsing bug shows up as a log line, not a stuck consumer. Also check `redis-cli -h localhost XLEN webhook:events` - a growing, never-draining backlog points at `webhook-worker` being stuck rather than `webhook` failing to enqueue. |
| Dashboard edits stop saving after a Grafana upgrade                           | Grafana 13.1.0 (bumped from 11.2.0 for tabs support - see "Dynamic dashboards" below) had a known OSS 12.4.0 bug where "Dynamic Dashboards" broke *provisioned* dashboards on save ([grafana/grafana#119450](https://github.com/grafana/grafana/issues/119450)) - our exact setup (`type: file` provider, `allowUiUpdates: true` in `services/grafana/provisioning/dashboards/dashboard.yml`). Unconfirmed whether 13.1.0 still has it; if UI edits silently fail to persist, that's the first thing to check.                                                                                                                                                        |
| Grafana stops responding after a few clicks/panel loads (no crash in browser) | Check `docker inspect receipt-goblin-grafana --format '{{.State.OOMKilled}} {{.State.ExitCode}}'` - Grafana 13.1.0 is meaningfully heavier than 11.2.0 (alerting scheduler, zanzana authz, bleve search indexing, app registry, background plugin auto-updater) and can hit `mem_limit: 512m` within a couple of dashboard interactions (`OOMKilled=true`, exit 137). There's a `restart: always` policy, so an OOM-killed container comes back on its own - raise `grafana`'s `mem_limit` in `docker-compose.yml` if it recurs.                                                                                                                                      |
| No `agent_name`/`skill_name` on events                                        | Recovered from the LiteLLM payload itself, not a CLI-side hook - see `AGENTS.md` (`_agent_invocations_from_messages`/`_skill_name_from_last_turn` in `clickhouse_ingest.py`). A subagent's own rows only resolve `agent_name` once the orchestrator's `Agent` tool_use/tool_result pair has itself been ingested and upserted into `agent_invocations` - a subagent call that reaches `webhook` before that happens will have `agent_invocation_id` set but blank `agent_name`.                                                                                                                                                                                       |
| Grafana panel shows a query error                                             | The `grafana-clickhouse-datasource` plugin's query JSON shape has changed across versions; open the panel in edit mode - the SQL in `rawSql` is otherwise plain, portable ClickHouse SQL.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Claude Code via the LiteLLM proxy fails with `x-api-key header is required`   | Missing `ANTHROPIC_CUSTOM_HEADERS`, or `LITELLM_MASTER_KEY` isn't set - see "Routing Claude Code through it" under "LiteLLM" below.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

## Reference

Everything below is background/design detail, not needed day-to-day - see `AGENTS.md` for the rules that actually constrain how you edit this repo.

### Configuration

| Variable                      | Default                              | Used by                                                                                                                                                                                                                                                                                                                                                                                                              |
| ----------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **ClickHouse**                |                                      |                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `CLICKHOUSE_DATABASE`         | required                             | clickhouse, webhook, mcp-server, grafana                                                                                                                                                                                                                                                                                                                                                                             |
| `CLICKHOUSE_USER`             | required                             | clickhouse, webhook, mcp-server, grafana                                                                                                                                                                                                                                                                                                                                                                             |
| `CLICKHOUSE_PASSWORD`         | required                             | clickhouse, webhook, mcp-server, grafana                                                                                                                                                                                                                                                                                                                                                                             |
| `CLICKHOUSE_HOST`             | `clickhouse`                         | webhook, mcp-server, grafana                                                                                                                                                                                                                                                                                                                                                                                         |
| `CLICKHOUSE_PORT`             | `8123`                               | webhook, mcp-server, grafana                                                                                                                                                                                                                                                                                                                                                                                         |
| `CLICKHOUSE_HTTP_PORT`        | `8123`                               | host port mapping for clickhouse's HTTP interface                                                                                                                                                                                                                                                                                                                                                                    |
| `CLICKHOUSE_NATIVE_PORT`      | `9000`                               | host port mapping for clickhouse's native protocol                                                                                                                                                                                                                                                                                                                                                                   |
| **Redis**                     |                                      |                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `REDIS_HOST`                  | `redis`                              | webhook, webhook-worker - queue between the two, see "How data flows" above                                                                                                                                                                                                                                                                                                                                          |
| `REDIS_PORT`                  | `6379`                               | webhook, webhook-worker                                                                                                                                                                                                                                                                                                                                                                                              |
| **Webhook**                   |                                      |                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `CAPTURE_ENABLED`             | `false`                              | webhook - write every raw POST body to `CAPTURE_DIR`, see "Inspecting captured traffic" below. Off by default: real prompt/response content, and one file per request adds disk I/O to the hot path. **Not currently passed through by `docker-compose.yml`'s `webhook` service** - setting it in `.env` alone has no effect; add it under `webhook`'s `environment:` in `docker-compose.yml` to actually enable it. |
| `CAPTURE_DIR`                 | `/app/captures`                      | webhook - only read when `CAPTURE_ENABLED=true`. Same caveat as above - not wired into `docker-compose.yml`'s `webhook` service today.                                                                                                                                                                                                                                                                               |
| `WEBHOOK_PORT`                | `8010`                               | host port mapping for webhook                                                                                                                                                                                                                                                                                                                                                                                        |
| `WEBHOOK_URL`                 | `http://webhook:8000/api/v1/metrics` | litellm - where it POSTs the `StandardLoggingPayload` for each call                                                                                                                                                                                                                                                                                                                                                  |
| **MCP server**                |                                      |                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `MCP_SERVER_PORT`             | `8001`                               | host port mapping for mcp-server                                                                                                                                                                                                                                                                                                                                                                                     |
| **Grafana**                   |                                      |                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `GRAFANA_PORT`                | `3000`                               | host port mapping for grafana                                                                                                                                                                                                                                                                                                                                                                                        |
| **LiteLLM**                   |                                      |                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `LITELLM_PORT`                | `4000`                               | host port mapping for litellm                                                                                                                                                                                                                                                                                                                                                                                        |
| `LITELLM_MASTER_KEY`          | required                             | litellm - admin credential for `/ui` and `/key/generate`; real Anthropic/OpenAI keys and per-person virtual keys are managed through the UI instead, see "LiteLLM" below                                                                                                                                                                                                                                             |
| `LITELLM_DB_PASSWORD`         | required                             | litellm, litellm-db - Postgres password for LiteLLM's own virtual-keys/budgets database                                                                                                                                                                                                                                                                                                                              |
| `LITELLM_BASE_URL`            | `http://litellm:4000`                | webhook - internal docker network address for calling LiteLLM's `/key/info`. Not user-configurable, fixed in `docker-compose.yml`.                                                                                                                                                                                                                                                                                   |
| **Git branch reporting hook** |                                      |                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `AGENT_CLI_TRACKING_API_URL`  | required                             | `hooks/report_git_branch.py` - not a `docker-compose.yml`/`.env` variable, exported into your shell instead (see `make env` and "Git branch/repo" below). No fallback: if unset, the hook crashes (`KeyError`, non-zero exit) instead of guessing a URL.                                                                                                                                                             |
| `LITELLM_VIRTUAL_KEY`         | required                             | `hooks/report_git_branch.py` - personal virtual key (see `make env`), sent as `Authorization: Bearer` on every git-branch report; webhook verifies it against LiteLLM's own `/key/info` before accepting the report. No fallback: hook crashes if unset, same as `AGENT_CLI_TRACKING_API_URL`.                                                                                                                       |

`CLICKHOUSE_PASSWORD` is required (not just non-empty by convention - `docker-compose.yml` refuses to start without it): ClickHouse restricts the `default` user to localhost-only access whenever user/password are unset, which breaks the other containers connecting over the Docker network.
`*_PORT` variables only change the **host** side of each port mapping - the container-internal port stays fixed, so services keep reaching each other over the `receipt-goblin` Docker network regardless of what you set these to.

Each service also has a `mem_limit`: `clickhouse` 2g (paired with `services/clickhouse/config.d/memory.xml`'s 0.85 ratio so it respects the cgroup limit instead of trying to use host RAM), `litellm` 2g, `grafana` 512m (see the Grafana OOM row under "Troubleshooting" above), `redis` 768m (`--maxmemory 700mb` - see `AGENTS.md` "Why a queue in front of ClickHouse" for the sizing math), `mcp-server`/`webhook-worker` 256m each, `webhook` 128m, `litellm-db` 256m.

### Schema

| Table                | Purpose                                                                                                                                                                                                                                                                                                                                                                             |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent_events`       | One row per LiteLLM call, full `raw_payload` JSON (the `StandardLoggingPayload`, minus `messages`).                                                                                                                                                                                                                                                                                 |
| `agent_usage`        | One row per model call: tokens, plus `cost`/`input_cost`/`output_cost` straight from LiteLLM's own `response_cost`/`cost_breakdown` - cache-pricing-aware and never derived locally (a manually-maintained `model_pricing` table + `ASOF JOIN` used to compute cost instead, and was removed after it was found to overcount by several times whenever prompt caching was in play). |
| `agent_messages`     | One row per call, holding `prompt_text`/`response_text`.                                                                                                                                                                                                                                                                                                                            |
| `session_git_branch` | One row per session, `git_branch`/`git_repo` reported by `hooks/report_git_branch.py` at `SessionStart` and, in Claude Code, `CwdChanged` - not from LiteLLM, see below. Join on `session_id` against the tables above.                                                                                                                                                             |

### Per-request signals on `agent_usage`

Beyond token counts, each usage row also carries a few fields read straight off LiteLLM's `StandardLoggingPayload`, added because token/cost alone can't tell a normal completion from a truncated or refused one, or show which cache tier actually got written:

| Column                                                 | Source                                                                                    | Why                                                                                                                                                               |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `stop_reason`                                          | `response.choices[0].finish_reason`                                                       | `end_turn` vs `max_tokens` vs `refusal` vs `tool_use` - a `max_tokens` row means the reply got cut off, not just that it was expensive.                           |
| `cache_creation_1h_tokens`, `cache_creation_5m_tokens` | `usage.prompt_tokens_details.cache_creation_token_details.ephemeral_{1h,5m}_input_tokens` | 1h and 5m ephemeral cache writes are priced differently; `cache_creation_tokens` stays their sum for the existing cost/token panels, these two are the breakdown. |

There's no request-level "reasoning effort" field anywhere in the payload (checked - `grep`ed real captures for `effort`/`reasoning_effort`/`budget`, none exist).
Model choice (`agent_usage.model`) is the closest proxy: cheaper/faster models are already picked per-agent via `model:` in an agent's frontmatter (e.g. `.claude/agents/test-coder.md` uses `claude-haiku-4-5`), and panels 16/17 already break cost/tokens down by model.

### Message-level text

`agent_events.raw_payload` carries the full `StandardLoggingPayload` minus `messages` (the ever-growing full conversation history - also on disk verbatim in `services/webhook/captures/*.json` when `CAPTURE_ENABLED` is on, see "Inspecting captured traffic" below).
`agent_messages` adds what's missing from that: the last user message's text and the model's own reply text for that call, via `_last_user_text()`/`_flatten_content()` in `clickhouse_ingest.py`.
A row is only written when at least one of `prompt_text`/`response_text` is non-empty.

### Git branch/repo (`session_git_branch`)

Every other table here is populated from LiteLLM's `StandardLoggingPayload`, which never sees the calling CLI's working directory or git state.
`session_git_branch` is the one exception: `hooks/report_git_branch.py` reads the branch via `git rev-parse --abbrev-ref HEAD` and the repo via the `origin` remote's URL (falling back to the working tree's toplevel directory name when there's no `origin`) in the session's `cwd`, and POSTs both straight to `webhook`'s `/api/v1/session-git-branch` route - bypassing LiteLLM entirely.
It runs at `SessionStart` (registered in `.claude/settings.json` and `.codex/hooks.json`) and, in Claude Code only, again on `CwdChanged` - Codex CLI has no equivalent hook event, so a Codex session still only reports once, at start.
That means it's still not fully live even in Claude Code: only a `cd`/directory switch re-triggers the hook, not a plain `git checkout` within the same directory.
Each report is authenticated: the hook sends its personal LiteLLM virtual key as `Authorization: Bearer <key>`, and `webhook` checks that key against LiteLLM's own `/key/info` (rejecting blocked or expired keys with a 401) before writing the row - reusing LiteLLM's existing key store instead of a separate signing scheme.
It needs `AGENT_CLI_TRACKING_API_URL` and `LITELLM_VIRTUAL_KEY` set in your shell to know where to POST to and to authenticate - `make env` prints `export` lines for both (see "Issue yourself a personal key" above); neither has a fallback, so an unset/missing variable crashes the hook (`KeyError`, non-zero exit) rather than guessing a URL or skipping auth.

### MCP server (`mcp-server`)

Listens on `:8001/mcp` (FastMCP `streamable-http` transport). Two tools:

- `whatsup(hours: int = 24)` - three fixed queries (total tokens, total cost from `agent_usage.cost`, top 5 spenders). Read-only by construction - never runs arbitrary SQL from the model.
- `query(sql: str, max_rows: int = 200)` - arbitrary SQL from the model, for the `clickhouse-analyst` subagent (see `.claude/agents/clickhouse-analyst.md`) and ad hoc lookups. There's no separate read-only ClickHouse user (`docker-compose.yml` uses one shared user for webhook/mcp-server/grafana), so `_validate_readonly_sql()` in `server.py` is the only thing enforcing read-only: single statement, must start with `SELECT`/`WITH`, no DDL/DML keywords anywhere in the query (word-boundary matched, so it also catches them inside subqueries), no `system`/`information_schema`/`mysql` database access, no remote/file/URL/other-DB table functions (`remote`, `url`, `file`, `s3`, `mysql`, `postgresql`, etc. - these read data from outside ClickHouse entirely, a DDL/DML keyword check alone wouldn't catch them), and must reference at least one of this stack's own tables. Results are always wrapped in an outer `LIMIT` (default/max 200, hard cap 1000) so a forgotten `LIMIT` in the model's query can't return unbounded rows.

`src/server.py` exposes `app = mcp.streamable_http_app()` at module level, run via `uvicorn src.server:app` (see `services/mcp-server/Dockerfile`) - deliberately *not* mounted under a separate FastAPI app, since the official `mcp` SDK has a known bug there (session manager never initializes when `streamable_http_app()` is mounted as a sub-app, requests 404/507 - [modelcontextprotocol/python-sdk#1367](https://github.com/modelcontextprotocol/python-sdk/issues/1367)).
Same dev/prod split as `webhook` below: `docker-compose.yml` still `build`s `services/mcp-server/Dockerfile` (deps baked into the image), then bind-mounts `services/mcp-server/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build mcp-server`. Built and run standalone (no compose, no `--reload`), it's the same self-contained image `Dockerfile` describes.

### Frontmatter format

Subagents and Skills are identified differently by Claude Code (frontmatter `name:` vs. directory name - confirmed against actual Claude Code behavior, not assumed), so their version convention differs too:

**Subagents** (`.claude/agents/*.md`) - frontmatter `name:` is the actual invocation identifier (the filename doesn't have to match), so it doubles as the version tag: `<name>_v<version>`, no separate `version:` field.

```
---
name: test-researcher_v1.0.0
description: ...
---
```

**Skills** (`.claude/skills/<dirname>/SKILL.md`) - the *directory name* is the invocation identifier (`/<dirname>`); frontmatter `name:` is purely a cosmetic display label and does not affect invocation. In practice `name:` is kept versioned and identical to the directory name (`<name>_v<version>`), same convention as Subagents, and there's no separate `version:` field:

```
---
name: test-linter_v2.0.0
description: ...
---
```

`agent_registry`/`skill_registry` were dropped (`DROP TABLE`, not just left empty) - they were only ever populated by the retired transcript-reading hooks pipeline and had been sitting empty since.

### Grafana dashboard panels

"Agents Overview" - each panel's own `description` field (info icon in the Grafana UI, or `services/grafana/dashboards/agents_overview.json` directly) is the source of truth for what it shows and why. Don't duplicate panel descriptions here - they drift out of sync with the dashboard JSON otherwise; edit the panel's own `description` instead.

### Dynamic dashboards / tabs

Grafana bumped from `11.2.0` to `13.1.0` in `docker-compose.yml` to get native dashboard tabs ("Dynamic dashboards", GA'd April 2026 - new v2 dashboard schema, tabs as a first-class layout option alongside rows).
The dashboard's former row-based grouping was converted into tabs via the Grafana UI (open the dashboard, the new editor migrates v1→v2 on load, then drag/convert rows into tabs) rather than by hand-authoring the v2 JSON schema directly, since it was new enough that hand-rolling it blind would have been error-prone.
Known risk to watch: [grafana/grafana#119450](https://github.com/grafana/grafana/issues/119450) reported Dynamic Dashboards breaking *provisioned* dashboards on save in OSS 12.4.0 - our setup (`type: file` provider, `allowUiUpdates: true`) matches that exactly; unconfirmed whether 13.1.0 still has it.

Seven template variables in order: `$agent_name`, `$skill_name`, `$command_name`, `$mcp_tool`, `$model`, `$user_id`, `$session_id` (the session picker's own query is scoped by selected user(s), so `$user_id` must precede it).
`$model` needs no `= ''` escape hatch since `agent_usage` rows are always real model calls; same for `$user_id`/`$session_id` against `agent_events`.
`$mcp_tool`'s dropdown label strips the `mcp__` prefix but filters on the real full `tool_name`.

### Debugging ingestion

Field extraction from the LiteLLM payload is best-effort and can drift across LiteLLM versions.
`docker compose logs -f webhook` shows one log line per exception raised while enqueueing (`build_event`/`queue_client.enqueue`, never re-raised, so a parsing bug never breaks LiteLLM's ack); `docker compose logs -f webhook-worker` shows the same for the batched-insert side (`ingest_events_batch`). `redis-cli -h localhost XLEN webhook:events` shows the current backlog - non-zero-but-draining is normal, non-zero-and-growing means `webhook-worker` has fallen behind or died. Setting `CAPTURE_ENABLED=true` (off by default, see "Configuration" below) also lands every raw POST body verbatim under `services/webhook/captures/` for offline replay (see "Inspecting captured traffic" below).

## LiteLLM

A local LiteLLM gateway (`litellm` + `litellm-db` + `webhook` services in `docker-compose.yml`) sits in front of both CLIs so their traffic can be logged, and centrally billed, before it leaves the machine.
This gateway *is* how the ClickHouse tracking stack described above gets its data now - `webhook` is the only ingestion path (see "How data flows" above).

The model names are meant to be stable regardless of what's actually billing them: `claude-sonnet-5`/`claude-haiku-4-5`/`claude-opus-4-8`/`claude-fable-5`/`gpt-5-codex`/`gpt-5` are what you pick in Claude Code's own model selector, put in agent/skill frontmatter `model:` fields, and set as Codex CLI's model - everywhere - and that stays true whether a name is currently backed by OAuth passthrough (no Anthropic key on hand yet) or a real, centrally-held provider key added later through the admin UI.
People get a personal LiteLLM *virtual key* either way, and per-key budgets/rate-limits/model access are enforced entirely by LiteLLM - see "Issue yourself a personal key and route a coding agent through the proxy" under "Getting started" above.
`litellm-db` (Postgres) is what makes virtual keys persistent - without a database, LiteLLM either refuses to generate them or keeps them in memory only, gone on the next restart.

### Model name mapping

The whole point of picking `model_name` values up front is that agent/skill frontmatter and both CLIs' model settings reference these same names, unaware of what's actually behind them:

| Virtual name (use everywhere) | Real model                   | Backend right now                                                        |
| ----------------------------- | ---------------------------- | ------------------------------------------------------------------------ |
| `claude-sonnet-5`             | `anthropic/claude-sonnet-5`  | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet) |
| `claude-haiku-4-5`            | `anthropic/claude-haiku-4-5` | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet) |
| `claude-opus-4-8`             | `anthropic/claude-opus-4-8`  | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet) |
| `claude-fable-5`              | `anthropic/claude-fable-5`   | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet) |
| `gpt-5-codex`                 | `openai/gpt-5-codex`         | Not defined yet - needs a real `OPENAI_API_KEY`                          |
| `gpt-5`                       | `openai/gpt-5`               | Not defined yet - needs a real `OPENAI_API_KEY`                          |

This table is the file-based (git-tracked) half of the mapping, and it's enough on its own for Claude-only skills/agents shared across sessions - no admin UI setup required beyond issuing personal keys.
The `anthropic/`, `openai/`, `ollama/` prefix on every "Real model" value above is mandatory, not cosmetic - LiteLLM parses `litellm_params.model` as `<provider>/<model>` to pick which provider adapter handles the call, so a bare `gemma3:4b` or `claude-sonnet-5` (no prefix) fails to route rather than falling back to a sensible default.

It stops being enough the day a skill/agent's frontmatter needs to resolve to *different* real models depending on which CLI runs it (e.g. Codex should hit `gpt-5-codex` for a name that means "the good model", while Claude Code should hit `claude-sonnet-5` for that exact same name) - `model_name` in `config.yaml` is a single flat namespace, it can't branch on which CLI asked.
That branching is what LiteLLM's **Team/Key Model Aliases** are for: a Team (or an individual key) can remap an alias to a different real `model_name`, so the same alias resolves differently depending on which key made the call.
Unlike everything above, model aliases are **not** expressible in `config.yaml` - they're Team/Key configuration, which only exists once created through `/ui` or the API, persisted in `litellm-db`.
There's no reason to set this up before `gpt-5-codex`/`gpt-5` actually exist (a real `OPENAI_API_KEY` gets added) - until then, a Team alias would just point at a model that doesn't work yet.
Once it's needed: **Teams** → create e.g. `claude-users` with Model Alias `SHARED_NAME → claude-sonnet-5`, and `codex-users` with `SHARED_NAME → gpt-5-codex`; issue personal keys scoped to the matching team.

### Remote model sources

Models served by a separate machine on the LAN - not a `docker-compose.yml` service, and unlike the Anthropic entries above, no OAuth passthrough involved, LiteLLM talks to that host directly - are plain, hand-written LiteLLM config files under `services/litellm/user_configs/`, not `.env` variables.
Two are set up already: `ollama/reasoning`/`ollama/embeddings` (Ollama) and `reranker` (a HuggingFace/TEI-compatible rerank server, see below).
Each lives in its own file in that directory (real host/model values, e.g. `services/litellm/user_configs/config.ollama.yaml`) - any filename ending `.yaml` works, no other naming convention required.
Everything under `services/litellm/user_configs/*.yaml` is gitignored - these hold this machine's actual LAN address, so treat them like `.env`: real values, never committed.
`services/litellm/user_configs/config.yaml.tmpl` (committed - `.tmpl`, not `.yaml`, so it's excluded from both git and the merge below) is the format spec/example: one template covering both sources, with `<OLLAMA HOST>`/`<OLLAMA PORT>`/`<RERANKER HOST>`/etc. placeholders to fill in - copy the relevant `model_list` entries out of it into your own file under `user_configs/` and replace the placeholders.
`services/litellm/docker-entrypoint.sh` merges every `*.yaml` file it finds in `user_configs/` into `config.yaml` via LiteLLM's own `include:` directive ([config file docs](https://docs.litellm.ai/docs/proxy/config_management)) - delete a source's file entirely and its models simply don't exist at all (not "exist but fail"), instead of every request needing to discover that no such host is configured.
Adding a third remote source never needs a `docker-compose.yml` or `docker-entrypoint.sh` change - add a `.yaml` file under `user_configs/` (same shape as `config.yaml.tmpl`) and restart the `litellm` container; the whole `services/litellm` directory is already bind-mounted in.

The Ollama tag behind `ollama/reasoning`/`ollama/embeddings` must already be pulled on the Ollama host (`ollama pull gemma3:4b`, `ollama pull embeddinggemma:300m`) - LiteLLM doesn't pull models itself.
Ollama must also be listening on `0.0.0.0`, not just `localhost`, on its own host, or the `litellm` container can't reach it across the LAN.
`reranker`'s `model` must use LiteLLM's `huggingface/<repo>` provider prefix, e.g. `huggingface/BAAI/bge-reranker-v2-m3` - LiteLLM's `huggingface/` rerank provider speaks the raw HuggingFace Text Embeddings Inference (TEI) wire protocol to `api_base` - request `{query, texts: [...]}`, response a bare JSON array `[{index, score}]` (no `results` wrapper, unlike Cohere-style rerank APIs) - so the host just needs to be a TEI-compatible rerank server, no LiteLLM-side transformation code is needed.

### Right now: no Anthropic/OpenAI key yet

`claude-sonnet-5`/`claude-haiku-4-5`/`claude-opus-4-8`/`claude-fable-5` are defined in `services/litellm/config.yaml`'s `model_list` with no `api_key` - `model_group_settings.forward_client_headers_to_llm_api` forwards the caller's own `claude login` subscription token straight to Anthropic instead.
`gpt-5-codex`/`gpt-5` have no equivalent (OpenAI has nothing like Anthropic's OAuth passthrough), so they simply don't exist yet - add them once a real `OPENAI_API_KEY` shows up.

### Routing Claude Code through it

`make env` (see "Getting started" above) prints `export` statements with a `<virtual key>` placeholder for `ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_HEADERS`/etc. (`LITELLM_PORT` in `.env` control the URL if you changed it from the default). Model choice isn't part of this - Claude Code picks its own model through its normal interface, same as always.
Then `claude login` (subscription OAuth, Pro/Max/Team) as usual.

`ANTHROPIC_CUSTOM_HEADERS` is required even though nothing else guards these routes: without a distinct header proving something *else* authenticated to LiteLLM, it can't tell the incoming `Authorization` (the subscription token) apart from its own auth and strips it before forwarding - Anthropic then replies `x-api-key header is required` (see [BerriAI/litellm#19618](https://github.com/BerriAI/litellm/issues/19618)).
`general_settings.litellm_key_header_name: x-litellm-api-key` in `services/litellm/config.yaml` is what makes LiteLLM read the virtual key from that header, checking it against the budget/model/rate-limit rules on the key, independently of whatever gets forwarded to Anthropic.

### Routing Codex CLI through it

Once `gpt-5-codex`/`gpt-5` exist (Codex has no subscription-passthrough option, so this can't happen before a real `OPENAI_API_KEY` is added), issue a personal virtual key the same way (**Keys** → **Create New Key**, `Models` restricted to `gpt-5-codex`/`gpt-5`).
The same `make env` (see above) also prints `OPENAI_API_BASE`/`OPENAI_API_KEY` lines from that key - Codex (and other OpenAI-SDK-based tools) read it directly, no custom header needed on that side. Set Codex's own model setting to `gpt-5-codex` or `gpt-5`.

### Inspecting captured traffic

`webhook` logs one line per captured/enqueued payload (or per exception, see "Debugging ingestion" above) - `docker compose logs -f webhook` while driving a session through either CLI.
It listens on host port `8010` (container port `8000`), reachable inside the `receipt-goblin` Docker network as `webhook:8000`.

Set `CAPTURE_ENABLED=true` under `webhook`'s `environment:` in `docker-compose.yml` (off by default, and **not** currently forwarded from `.env` - see "Configuration" below) to also have every hit land as its own timestamped JSON file under `services/webhook/captures/` on the host (bind-mounted, not a Docker volume - `ls services/webhook/captures/` works directly, no `docker exec` needed), raw as received.
`log_format: json_array` in `services/litellm/config.yaml` means each file is usually a list of `StandardLoggingPayload` objects, not a single one.
This directory is gitignored - it's real prompt/response content, not something to commit.
`docker-compose.yml` still `build`s `services/webhook/Dockerfile` (deps baked into the image), then bind-mounts `services/webhook/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build webhook`. `captures/` is mounted separately (it's runtime output, not source) so it lands on the host either way.
Built and run standalone (no compose, no `--reload`, no bind mounts) - `docker build -t webhook . && docker run -p 8000:8000 webhook` - it's the same self-contained image `Dockerfile` describes.
