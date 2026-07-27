# Agent Tracking Stack

## Minimal resource requirements

Memory: 8 GiB. CPU: 4.

Local stack for tracking cost and efficiency of AI coding agents (Claude Code and Codex CLI), with full call-chain tracing - agent, skill, and MCP tool usage are all tracked, not just top-level agent activity.
See `AGENTS.md` for architecture and schema details.

## Overview

### How data flows

1. Every LLM call from the CLI (Claude Code or Codex) is routed through a local LiteLLM proxy on `:4000` instead of hitting Anthropic/OpenAI directly.
2. LiteLLM's `generic_api` callback POSTs the full `StandardLoggingPayload` for each call to `webhook` on `:8010`.
3. `webhook` pushes the raw body onto a `redis` queue completely unmodified - no parsing, no ClickHouse access, request-path work is I/O only (see `AGENTS.md`); optionally (`CAPTURE_ENABLED`, off by default) it also captures each individual event to its own file under `services/webhook/captures/<session_id>/` for offline inspection.
4. `webhook-worker` drains that queue in batches, computes a compact event per payload (agent/skill invocations recovered from the payload itself, not from a CLI-side hook) and is the only thing that actually writes to ClickHouse on `:8123` for this traffic - a few large inserts instead of one per request, so ClickHouse isn't hit directly by request volume (see "Why a queue in front of ClickHouse" in `AGENTS.md`).
5. Grafana on `:3000` queries ClickHouse directly for every panel; there's no caching layer, so a dashboard refresh always reflects current table state.
6. Reads go the other way: `webhook`/`webhook-worker` are write-only, so a CLI session reads data back out (e.g. `/whatsup` in Claude Code) via the `mcp-server` MCP server on `:8001`, registered in `.mcp.json`.

### Services at a glance

Once the stack is up, open **[`http://localhost`](http://localhost)** - a landing page with a clickable link to every service, with the actually configured ports (port 80, `services/load-balancer/index.html.template`, rendered into the real page with the current `.env` ports on every container start - see `docker-entrypoint.d/50-render-index-html.sh`).

All of these are published by the single `load-balancer` (nginx) service now, not by each service's own container - see "Configuration" under "Reference" below.

## Getting started

### Environment variables

Recommended: run `make init` first - it interactively asks for the database name, a bootstrap superuser, and a username/password for each of the five ClickHouse roles (generating any password you leave blank), copies `.env.example` to `.env` if it doesn't exist yet, writes all of that in, then brings up just `clickhouse` long enough to create those users before stopping it again.
When it finishes, continue to "Start the stack" below.

```bash
make init
```

If you'd rather fill in `.env` by hand instead:

```bash
cp .env.example .env
```

| Variable                               | Required? | What it's for                                                                                                                                                                                                                                                                           |
| -------------------------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CLICKHOUSE_DATABASE`                  | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |
| `CLICKHOUSE_INGEST_USER`/`_PASSWORD`   | required  | webhook/worker/webhook-reparse's identity - see "Configuration" under "Reference" below.                                                                                                                                                                                                |
| `CLICKHOUSE_GRAFANA_USER`/`_PASSWORD`  | required  | grafana's identity - see "Configuration" under "Reference" below.                                                                                                                                                                                                                       |
| `CLICKHOUSE_MCP_USER`/`_PASSWORD`      | required  | mcp-server's identity - see "Configuration" under "Reference" below.                                                                                                                                                                                                                    |
| `CLICKHOUSE_BACKUP_USER`/`_PASSWORD`   | required  | backup's identity - see "Configuration" under "Reference" below.                                                                                                                                                                                                                        |
| `CLICKHOUSE_LOADTEST_USER`/`_PASSWORD` | required  | loadtest's identity - see "Configuration" under "Reference" below.                                                                                                                                                                                                                      |
| `CLICKHOUSE_LOADTEST_DATABASE`         | required  | loadtest's own dedicated database, separate from `CLICKHOUSE_DATABASE` - see "Load testing" below.                                                                                                                                                                                      |
| `CLICKHOUSE_BOOTSTRAP_USER`            | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |
| `CLICKHOUSE_BOOTSTRAP_PASSWORD`        | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |
| `LITELLM_MASTER_KEY`                   | required  | litellm - admin credential for `/ui` and `/key/generate`; real Anthropic/OpenAI keys and per-person virtual keys are managed through the UI instead, see "LiteLLM" below. Also used by webhook to call LiteLLM's `/key/info` when verifying `hooks/report_git_branch.py`'s virtual key. |
| `LITELLM_DB_PASSWORD`                  | required  | `docker-compose.yml` refuses to start without it - see "Configuration" under "Reference" below.                                                                                                                                                                                         |

Everything else in `docker-compose.yml` (ports, ClickHouse host, etc.) has a sane default - you only need to touch `.env` for the rows above; see "Configuration" under "Reference" below for the full list.
Remote model sources (Ollama, a reranker, etc.) aren't `.env` variables at all - see "Remote model sources" under "LiteLLM" below.
Your personal LiteLLM key does **not** go in `.env` at all - see the next step.

### Dev vs prod

`ENVIRONMENT` in `.env` (default `development`) decides which mode `make` runs in - every `make` target prints `⚠️  ENVIRONMENT=...` first so it's always obvious which one is active.

- **`development`** (default) layers `docker-compose.dev.yml` on top of `docker-compose.yml`, bind-mounting live source/config into the containers and enabling `--reload` for `webhook`/`mcp-server` - editing `services/webhook/src/*.py` or a Grafana dashboard JSON takes effect without a rebuild.
- **`production`** (`ENVIRONMENT=production make up`, or set `ENVIRONMENT=production` in `.env`) uses `docker-compose.yml` alone - every service then runs the image built entirely from its own `Dockerfile`, with no source/config bind mounts and no `command:`/`entrypoint:` overrides; picking up a code change requires rebuilding (`ENVIRONMENT=production make up` again).

### Start the stack

```bash
make start
```

This starts the core stack only. Langfuse and observability never start automatically - must be run explicitly via `make langfuse-up` and `make observability-up` respectively. Run `make langfuse-up`/`make langfuse-down`/`make langfuse-logs` directly to manage Langfuse on its own without touching the core stack; `make stop`/`make down` will tear down both Langfuse and observability automatically as a courtesy.

### Issue yourself a personal key and route a coding agent through the proxy

1. Open http://localhost:4000/ui and log in with `admin` / your `LITELLM_MASTER_KEY`.
2. **Models + Endpoints** (http://localhost:4000/ui/models-and-endpoints) → check the model you need is actually listed. If it isn't, don't add it here - go set it up under "Remote model sources" / "Model name mapping" under "LiteLLM" below instead, then come back.
3. **Teams** (http://localhost:4000/ui/teams) → **Create New Team**, if none exists yet - a key can't be created without a team to belong to.
4. **Keys** → **Create New Key** → pick the team from step 3 → restrict `Models` to whichever names the agent(s) you use need → copy the generated `sk-...` key.
5. Run `make setup-client` and either paste its shell-export lines into `~/.zshrc`/`~/.bashrc`, or merge its Codex/Claude Code config blocks into `~/.codex/config.toml`/`~/.claude/settings.json` instead (no shell rc edit needed) - see "Routing Claude Code through it" / "Routing Codex CLI through it" / "Configuring via config files instead of shell exports" under "LiteLLM" below. Add the key from step 4 as `LITELLM_VIRTUAL_KEY` in `.env` first (see `.env.example`) and `make setup-client` fills it in everywhere automatically; otherwise replace the printed `<virtual key>` placeholders by hand.

To confirm the proxy is actually seeing traffic: **Usage** (http://localhost:4000/ui/usage) for per-key/per-model spend and request counts, **Logs** (http://localhost:4000/ui/logs) for individual request/response payloads.

### Build or start a single service

`SERVICE` is optional on both `make build` and `make up`:

```bash
make build SERVICE=webhook   # just (re)build the webhook image, don't start it
make up SERVICE=webhook      # (re)build and (re)start just webhook
make build                   # build every service's image
make up                      # (re)build and (re)start the whole stack
make start SERVICE=webhook   # start webhook with existing image (no rebuild)
make start                   # bring up the whole stack with existing images
```

`up` rebuilds and recreates containers - use this when you've changed a Dockerfile, config baked into the image, or a service's `environment:` in the compose files. `start` just brings up whatever's already built - faster when you only want to resume after a `make stop`.

Always go through `make build`/`make up` rather than calling `docker compose build`/`docker compose up` directly - the `Makefile` resolves each service's image tag from `VERSIONS.yml` first (`scripts/resolve_image_version.py`) and exports it before invoking `docker compose`; a raw `docker compose build`/`up` skips that resolution and leaves you with an image tagged out of step with `VERSIONS.yml`.

### Wait until it's healthy

```bash
make status
```

`make status` runs `scripts/wait_for_stack_healthy.py`, which polls every service `docker compose config --services` lists (the ones this stack's default profile actually starts) until each is either `healthy` or - for one-shot services like `clickhouse-migrate`, which has no healthcheck - exited with code `0`.
On a real terminal it redraws a live `✔ Container <name>   OK   <elapsed>s` table in place (green `OK`, red `FAILED`), the same ANSI cursor-movement trick `docker compose up` itself uses - no extra package needed for that. Piped output (a log file, CI) falls back to one compact "waiting on: ..." line per poll instead.
It prints `All services healthy. OK.` and exits `0` once everything's up; if a service crashes or reports `unhealthy`, it exits `1` immediately with that service's last 80 log lines printed, instead of waiting out the full timeout on an already-known failure (it also gives up after 180s either way, printing logs for whatever's still not done).
Runnable any time, not just right after `make up` - `mcp-server` and `grafana` won't start until `clickhouse` shows `healthy`; `webhook`/`webhook-worker` also wait on `redis` (all `depends_on: condition: service_healthy`), so a clean `make status` run is a good general "is the stack actually up" check.

### Logs

```bash
make logs
```

### Open Grafana

http://localhost:3000/d/agents-overview

Anonymous viewer access is enabled by default - no login needed.

### Create additional ClickHouse users (optional)

Nothing here is required on first startup - `make init` already provisions all five ClickHouse roles (ingest/grafana/mcp/backup/loadtest, from `.env`, see "Configuration" under "Reference" below) once, and every service in this stack connects as its own least-privilege role.
Only reach for this if you want a *different* ClickHouse login - e.g. a personal one for the `/play` web UI, or one scoped to read a single table.

```bash
docker compose exec clickhouse /scripts/create_user.sh
```

It prompts for a username and password (leave the password blank to have one generated for you), then asks:

- **Grant Play/Dashboard UI access?** - read-only `SELECT` on `system.*`.
  Needed for the schema-browser sidebar on `http://localhost:8123/play` and ClickHouse's built-in `/dashboard` page to list tables/queries at all.
  Without it those pages load but stay empty - none of the five app roles have broad `system.*` SELECT (only `grafana` gets `system.tables`, only `mcp` gets `system.query_log`), since none of them need to introspect the server, only query their own scope.
- **Create a new database for this user?** - optional.
  Say yes and give it a name, and the script creates that database and then asks specifically whether this user should get full (not just read-only) access to it - no need to separately name it in the next question.
- **Database or table for full data access** - only asked if you didn't just create one above.
  Blank skips this (UI-only user); otherwise pick `default` (the whole database) or something narrower like `default.agent_events`, then say whether that access should include writes or stay read-only.

Every `CREATE USER`/`GRANT` statement it's about to run is printed before anything executes, with a final `y/N` confirmation - nothing is granted implicitly.
The script runs as the bootstrap superuser (see "Configuration" under "Reference" below for what that account is) - only that identity can create users or grant access.

## Usage

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

`make stop` also calls `make langfuse-down`, so it stops Langfuse too if it's running. Use `make langfuse-down` on its own if you only want to stop Langfuse and leave the core stack up.

## Backup & restore

Backs up and restores the three services in this stack that hold state not
reproducible from the repo: `clickhouse` (all tracking data), `litellm-db`
(LiteLLM's virtual keys/budgets/spend logs), and `grafana`'s `grafana.db`
(users/orgs, API keys, alert rules - dashboards themselves are already
source-controlled JSON, see `services/grafana/dashboards/`).

Everything runs through the `backup` tools-profile service
(`docker-compose.yml`) - it never uses `docker exec` or the Docker socket:
`clickhouse`/`litellm-db` are reached over the `receipt-goblin` network,
`grafana-data` is mounted directly as the same named volume the `grafana`
service itself uses. Files land under `$BACKUP_DIR` (`.env`, default
`.backups/` at the repo root) as `.backups/clickhouse/`,
`.backups/litellm/`, `.backups/grafana/`. **No automatic pruning** - backups
accumulate until you remove them by hand.

**One-time setup.** `clickhouse`'s BACKUP/RESTORE disk
(`services/clickhouse/config.d/backups.xml`) and its `$BACKUP_DIR/clickhouse`
bind mount only take effect once that container is recreated:

```bash
docker compose up -d --build clickhouse
```

This briefly restarts `clickhouse` only - unlike `litellm`, nothing else
depends on avoiding a restart here, but it's still worth doing at a quiet
moment since Grafana panels will show gaps for the few seconds it's down.

**Manual backup.**

```bash
make backup-clickhouse   # BACKUP DATABASE via clickhouse-client, safe on a live server
make backup-litellm      # pg_dump against litellm-db, safe on a live server
make backup-grafana      # sqlite3 .backup against grafana.db, safe on a live server
make backup-all          # all three - this is what cron should call
```

None of the three needs any container stopped - each uses a mechanism
that's safe to run against a live, in-use service (ClickHouse's own
`BACKUP` statement, a consistent `pg_dump` snapshot, SQLite's backup API).

**Restore. Destructive.** Each restore drops/overwrites the live target -
don't run these against anything but a throwaway/verification target unless
you actually mean to roll back to that snapshot. List available files first:
`ls .backups/clickhouse/`, `ls .backups/litellm/`, `ls .backups/grafana/`
(or under `$BACKUP_DIR` if you set one).

- *ClickHouse* - safe to run with `clickhouse` still up (drops and recreates
  the database as part of the restore, so any query mid-flight during the
  restore will simply fail, not corrupt anything):

  ```bash
  make restore-clickhouse FILE=clickhouse_default_20260724-030000.zip
  ```

- *LiteLLM* - `litellm` writes to `litellm-db` continuously, so stop it
  first so the restore isn't racing live writes (`litellm-db` itself must
  stay up, the restore connects to it):

  ```bash
  docker compose stop litellm
  make restore-litellm FILE=litellm_20260724-030000.dump
  docker compose start litellm
  ```

- *Grafana* - swapping `grafana.db` under a live server isn't safe, so stop
  `grafana` first:

  ```bash
  docker compose stop grafana
  make restore-grafana FILE=grafana_20260724-030000.db
  docker compose start grafana
  ```

**Cron.** Point cron at `make backup-all` from the repo root (needs
`docker`/`make` on `PATH` for cron's environment, which is usually sparser
than an interactive shell - use absolute paths or source your shell profile
if `make`/`docker` aren't found):

```
0 3 * * * cd /path/to/receipt-goblin && make backup-all >> .backups/cron.log 2>&1
```

Never point cron at a `restore-*` target - restore is a manual, deliberate
operation only.

## Load testing

`make loadtest` replays real captured traffic from `.capture/` (see "Inspecting captured traffic" below) straight at `webhook`'s own `POST /api/v1/metrics`, at a ramping concurrency profile - for answering "how does `webhook-worker`/`redis`/`clickhouse` cope under N concurrent Claude users?" without spending any API budget. It deliberately bypasses LiteLLM and the real Claude/Anthropic API entirely: `/api/v1/metrics` needs no LiteLLM virtual key (unlike `/api/v1/session-git-branch` and `/api/v1/plan-proposal`), so this only exercises `webhook -> redis -> webhook-worker -> clickhouse`.

The `loadtest` container connects to ClickHouse as its own dedicated `loadtest` role (`CLICKHOUSE_LOADTEST_USER`/`_PASSWORD`), scoped to a separate `CLICKHOUSE_LOADTEST_DATABASE` (default `loadtest`) rather than the app's real `CLICKHOUSE_DATABASE` - full (`ALL`) rights there, but nothing on real data.
`loadtest.py` itself doesn't touch ClickHouse directly yet (traffic still goes through `webhook` over HTTP - see below), but the `loadtest-runner` agent that drives this checks the dedicated database exists before every run and alerts if it doesn't, rather than creating it - it's provisioned by `clickhouse-migrate`/`make init`, see "Configuration" under "Reference" below.

One "virtual user" repeatedly picks a random real captured session and replays its whole event sequence in order, waiting the real (or `--speed`-scaled) gap between events, before picking another session and continuing - modeling one person's continuous usage, not a single request. Each event file's bytes are sent completely unmodified (no `id`/`trace_id` rewriting, no timestamp shifting) - this is a load test, not a data-integrity test, so concurrent replays of the same captured session will collide on `ingest_raw`' `ReplacingMergeTree` key and under-report actual replayed volume there; treat the tool's own final report (requests sent, status codes, latency) as the real throughput signal, not ClickHouse row growth.

The load profile ramps rather than starting flat: it begins at `START_USERS`, adds more every `RAMP_STEP_MINUTES`, up to `END_USERS`, then holds. `DURATION_MINUTES=0` (default) means "figure out the total yourself" (`ramp time + HOLD_MINUTES`); any positive value is the total length outright, with the hold portion derived from it instead. The fully resolved schedule (every step's target user-count and firing time) prints to the console before anything runs, and each step's ramp-up logs live as it happens.

```bash
make loadtest                                              # defaults: ramp 10->100 users over 10 steps/1 min each, then hold 5 min
make loadtest END_USERS=250 DURATION_MINUTES=30 SPEED=5     # 250 users, 30 min total, 5x faster than real cadence
make loadtest TARGET_URL=https://staging.example.com/api/v1/metrics
```

All variables are passed as `make loadtest VAR=value` (or exported in the shell before calling `make`):

| Variable            | Default                                    | Meaning                                                                                                                                                                                                                                                                                                |
| ------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `TARGET_URL`        | `http://load-balancer:8000/api/v1/metrics` | Where captured events get POSTed - point it at any reachable host, including staging/prod.                                                                                                                                                                                                             |
| `START_USERS`       | `10`                                       | Concurrent virtual users running from t=0.                                                                                                                                                                                                                                                             |
| `END_USERS`         | `100`                                      | Ceiling the ramp climbs to and then holds at.                                                                                                                                                                                                                                                          |
| `RAMP_STEPS`        | `10`                                       | How many increments to split the `START_USERS -> END_USERS` climb into - the tool derives how many users each step adds (`ceil((END_USERS - START_USERS) / RAMP_STEPS)`), not a manual step-size flag.                                                                                                 |
| `RAMP_STEP_MINUTES` | `1`                                        | Minutes between ramp steps. Together with `RAMP_STEPS`, fixes how long the ramp itself takes: `RAMP_STEPS * RAMP_STEP_MINUTES`.                                                                                                                                                                        |
| `HOLD_MINUTES`      | `5`                                        | Extra minutes to keep running at `END_USERS` after the ramp finishes. Only used to compute total length when `DURATION_MINUTES` is `0`.                                                                                                                                                                |
| `DURATION_MINUTES`  | `0`                                        | `0` = auto-compute total as `ramp time + HOLD_MINUTES`. Any positive value is the total run length outright, and `HOLD_MINUTES` is derived from it instead (`DURATION_MINUTES - ramp time`; if that's shorter than the ramp needs, the run just ends mid-ramp, never reaching `END_USERS` - no error). |
| `SPEED`             | `1.0`                                      | Divides all real inter-event gaps. `1.0` = realistic cadence, `>1` = faster/compressed, `0` = no waiting at all (max-throughput burst mode).                                                                                                                                                           |

`services/webhook/src/loadtest.py`'s module docstring has the full model if you need more detail than this table.

While a run is in flight, watch:
- `worker_stream_depth` on `webhook-worker`'s `:9200/metrics` (Redis Stream backlog - the best "is the worker keeping up" signal), plus `worker_pending_count`, `worker_flush_latency_seconds`, `worker_decode_failures_total`.
- `webhook`'s own FastAPI Instrumentator metrics on `:8000/metrics` (client-perceived POST latency doubles as "is the queue backpressuring").
- `redis-exporter`'s stock Redis memory stats.
- ClickHouse's `:9363` Prometheus endpoint, or the `clickhouse-analyst` agent, for `ingest_raw` row growth.

## Troubleshooting

| Symptom                                                                                                                                                                                                                                              | Likely cause / fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `webhook`/`grafana` stuck in `Created`, never start                                                                                                                                                                                                  | Their `depends_on: condition: service_healthy` is blocking on the `clickhouse` healthcheck (`webhook`/`webhook-worker` also wait on `redis`). Run `docker compose ps` - if `clickhouse` shows `unhealthy`, check `docker inspect receipt-goblin-clickhouse --format '{{json .State.Health}}'` for the actual healthcheck error, and confirm ClickHouse itself is fine with `docker exec receipt-goblin-clickhouse clickhouse-client --user "$CLICKHOUSE_BOOTSTRAP_USER" --password "$CLICKHOUSE_BOOTSTRAP_PASSWORD" --query "SELECT 1"` (the `default` user doesn't exist - see "Configuration" under "Reference" below). The image ships `wget`, not `curl` - the healthcheck uses `wget --spider`. |
| `webhook` can't reach ClickHouse (once running)                                                                                                                                                                                                      | `webhook` itself doesn't talk to ClickHouse for `/api/v1/metrics` anymore (see "How data flows" above) - check `docker compose logs redis` / `docker compose logs webhook-worker` for the actual connection error instead. `webhook`'s `/health` route still runs `SELECT 1` against ClickHouse plus a Redis `PING` and reports whichever exception hit first.                                                                                                                                                                                                                                                                                                                                       |
| `/whatsup` fails or times out                                                                                                                                                                                                                        | Confirm `mcp-server` is `healthy`/running (`docker compose ps`) and reachable at `http://localhost:8001/mcp`; check `docker compose logs mcp-server`. Claude Code only picks up `.mcp.json` changes on the next session start.                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| No rows landing in ClickHouse at all                                                                                                                                                                                                                 | Confirm the CLI is actually routed through LiteLLM (`ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_HEADERS` set, see "Routing Claude Code through it" below), then check `docker compose logs litellm` for callback errors, `docker compose logs webhook` for enqueue exceptions, and `docker compose logs webhook-worker` for batch-insert exceptions - `ingest_events_batch` never raises out of the worker loop, it only logs, so a parsing bug shows up as a log line, not a stuck consumer. Also check `redis-cli -h localhost XLEN webhook:events` - a growing, never-draining backlog points at `webhook-worker` being stuck rather than `webhook` failing to enqueue.                                |
| Dashboard edits stop saving after a Grafana upgrade                                                                                                                                                                                                  | Grafana 13.1.0 (bumped from 11.2.0 for tabs support - see "Dynamic dashboards" below) had a known OSS 12.4.0 bug where "Dynamic Dashboards" broke *provisioned* dashboards on save ([grafana/grafana#119450](https://github.com/grafana/grafana/issues/119450)) - our exact setup (`type: file` provider, `allowUiUpdates: true` in `services/grafana/provisioning/dashboards/dashboard.yml`). Unconfirmed whether 13.1.0 still has it; if UI edits silently fail to persist, that's the first thing to check.                                                                                                                                                                                       |
| Grafana stops responding after a few clicks/panel loads (no crash in browser)                                                                                                                                                                        | Check `docker inspect receipt-goblin-grafana --format '{{.State.OOMKilled}} {{.State.ExitCode}}'` - Grafana 13.1.0 is meaningfully heavier than 11.2.0 (alerting scheduler, zanzana authz, bleve search indexing, app registry, background plugin auto-updater) and can hit `mem_limit: 512m` within a couple of dashboard interactions (`OOMKilled=true`, exit 137). There's a `restart: always` policy, so an OOM-killed container comes back on its own - raise `grafana`'s `mem_limit` in `docker-compose.yml` if it recurs.                                                                                                                                                                     |
| No `agent_name`/`skill_name` on events                                                                                                                                                                                                               | Recovered from the LiteLLM payload itself, not a CLI-side hook - see `AGENTS.md` (`_agent_invocations_from_messages`/`_skill_name_from_last_turn` in `clickhouse_ingest.py`). A subagent's own rows only resolve `agent_name` once the orchestrator's `Agent` tool_use/tool_result pair has itself been ingested and upserted into `agent_invocations` - a subagent call that reaches `webhook` before that happens will have `agent_invocation_id` set but blank `agent_name`.                                                                                                                                                                                                                      |
| Grafana panel shows a query error                                                                                                                                                                                                                    | The `grafana-clickhouse-datasource` plugin's query JSON shape has changed across versions; open the panel in edit mode - the SQL in `rawSql` is otherwise plain, portable ClickHouse SQL.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| "Containers" tab / `$container` variable stays empty (observability profile)                                                                                                                                                                         | `cadvisor` can't identify containers under the containerd-snapshotter storage driver, which is the default in current Docker Desktop and can also be enabled under Colima - see `AGENTS.md` "cAdvisor container-level metrics" for the fix options. The "Host" tab (node_exporter) is unaffected. Tracked upstream as [google/cadvisor#3643](https://github.com/google/cadvisor/issues/3643), with an unmerged fix attempt at [google/cadvisor#3709](https://github.com/google/cadvisor/pull/3709) - worth re-checking occasionally in case either lands; no fixed timeline as of this writing.                                                                                                      |
| Claude Code via the LiteLLM proxy fails with `x-api-key header is required`                                                                                                                                                                          | Missing `ANTHROPIC_CUSTOM_HEADERS`, or `LITELLM_MASTER_KEY` isn't set - see "Routing Claude Code through it" under "LiteLLM" below.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `git_branch`/`git_repo` never show up (`session_git_branch` empty), or any direct `Authorization: Bearer <key>` call to LiteLLM's admin API (`/key/info`, `/models`, ...) gets `401 Malformed API Key passed in. Ensure Key has \`Bearer \` prefix.` | `services/litellm/config.yaml`'s `general_settings.litellm_key_header_name: x-litellm-api-key` (see "Routing Claude Code through it" below) repoints key auth at that custom header for **every** proxy route, not just the LLM ones it was added for - plain `Authorization` no longer works anywhere, including admin endpoints. `services/webhook/src/server.py`'s `_virtual_key_is_valid()` (used by `hooks/report_git_branch.py`'s `/api/v1/session-git-branch` and `hooks/report_plan_proposal.py`'s `/api/v1/plan-proposal`) sends `x-litellm-api-key: Bearer <key>` for this reason - if you're calling LiteLLM's admin API by hand, do the same.                                            |

## Reference

Everything below is background/design detail, not needed day-to-day - see `AGENTS.md` for the rules that actually constrain how you edit this repo.

### Make targets

Every target in the `Makefile`, one line each - whoever edits the `Makefile` keeps this table in sync as part of that same change (see `.claude/agents/dev-ops.md`'s "Editing the `Makefile`" section, which owns both). Most are covered in more depth elsewhere in this README - follow the section pointer for details/vars.

| Target                 | Args                              | What it does                                                                                       |
| ---------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------- |
| `init`                 |                                   | Interactive first-run ClickHouse role/user provisioning - see "Environment variables" above.       |
| `start`                | `SERVICE=<name>` (optional)       | Brings up containers with existing images, no rebuild - see "Start the stack".                     |
| `up`                   | `SERVICE=<name>` (optional)       | Rebuilds and recreates containers - see "Build or start a single service".                         |
| `restart`              |                                   | Restarts running containers in place (no rebuild) - picks up bind-mounted source edits.            |
| `up-no-deps`           | `SERVICE=<name>` (required)       | Recreates just one service, skipping its `depends_on` chain - for a config/env change, not source. |
| `build`                | `SERVICE=<name>` (optional)       | Builds image(s) without starting anything - see "Build or start a single service".                 |
| `status`               |                                   | Waits for every service to report healthy, prints pass/fail - see "Wait until it's healthy".       |
| `migrate`              |                                   | Runs just the ClickHouse migration container (`migrations/*.sql` + Dictionaries), nothing else.    |
| `stop` / `down`        |                                   | Tears down the core stack (plus Langfuse/observability, as a courtesy) - see "Stop the stack".     |
| `logs`                 |                                   | Tails logs for the whole core stack.                                                               |
| `setup-client`         |                                   | Prints shell-export/config-file snippets to route a CLI through the local LiteLLM proxy.           |
| `test`                 |                                   | Runs `services/webhook/tests` (needs `requirements-dev.txt` installed in `.venv` first).           |
| `reparse`              | `SESSION=<session_id>` (required) | Reparses one session's events from `ingest_raw` - see "Debugging ingestion".                       |
| `reparse-all`          |                                   | Reparses every event in `ingest_raw`.                                                              |
| `loadtest`             | see "Load testing" below          | Replays captured traffic at a ramping concurrency profile - see "Load testing".                    |
| `backup-clickhouse`    |                                   | Backs up ClickHouse - see "Backup & restore".                                                      |
| `backup-litellm`       |                                   | Backs up `litellm-db` - see "Backup & restore".                                                    |
| `backup-grafana`       |                                   | Backs up Grafana's `grafana.db` - see "Backup & restore".                                          |
| `backup-all`           |                                   | Runs all three backups above.                                                                      |
| `restore-clickhouse`   | `FILE=<name>` (required)          | Restores ClickHouse from a backup file - see "Backup & restore".                                   |
| `restore-litellm`      | `FILE=<name>` (required)          | Restores `litellm-db` from a backup file - see "Backup & restore".                                 |
| `restore-grafana`      | `FILE=<name>` (required)          | Restores Grafana's `grafana.db` from a backup file - see "Backup & restore".                       |
| `langfuse-up`          |                                   | Starts the opt-in Langfuse stack - see "Langfuse".                                                 |
| `langfuse-down`        |                                   | Stops just the Langfuse stack, leaving the core stack up.                                          |
| `langfuse-logs`        |                                   | Tails logs for the Langfuse stack.                                                                 |
| `observability-up`     |                                   | Starts the opt-in observability stack (Prometheus/Loki/etc.) - see "Observability".                |
| `observability-down`   |                                   | Stops just the observability stack, leaving the core stack up.                                     |
| `observability-logs`   |                                   | Tails logs for the observability stack.                                                            |
| `observability-status` |                                   | Shows container status for just the observability stack.                                           |

### Configuration

| Variable                                    | Default                                                          | Used by                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **General**                                 |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `ENVIRONMENT`                                | `development`                                                    | `Makefile` only, not `docker-compose.yml` - picks `docker-compose.yml` alone (`production`) vs. layering `docker-compose.dev.yml` on top (anything else) - see "Dev vs prod" above                                                                                                                                                                    |
| `BACKUP_DIR`                                 | `.backups`                                                       | host-side root directory the `backup` service reads/writes - see "Backup & restore" above                                                                                                                                                                                                                                                             |
| **ClickHouse**                              |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `CLICKHOUSE_DATABASE`                       | required                                                         | clickhouse, clickhouse-migrate, webhook, worker, webhook-reparse, mcp-server, grafana, backup                                                                                                                                                                                                                                                         |
| `CLICKHOUSE_INGEST_USER`/`_PASSWORD`        | required                                                         | webhook, worker, webhook-reparse - `ingest` role: `INSERT` on the whole database plus a narrow `SELECT` list, created once by `make init`, see below                                                                                                                                                                                                  |
| `CLICKHOUSE_GRAFANA_USER`/`_PASSWORD`       | required                                                         | grafana - `grafana` role: read-only (`SELECT` on the database plus `system.tables`)                                                                                                                                                                                                                                                                   |
| `CLICKHOUSE_MCP_USER`/`_PASSWORD`           | required                                                         | mcp-server - `mcp` role: read-only (`SELECT` on the database plus `system.query_log` + `SYSTEM FLUSH LOGS`, for the `profile_query` tool)                                                                                                                                                                                                             |
| `CLICKHOUSE_BACKUP_USER`/`_PASSWORD`        | required                                                         | backup - `backup` role: `BACKUP` privilege only, no `SELECT`/`INSERT`                                                                                                                                                                                                                                                                                 |
| `CLICKHOUSE_LOADTEST_USER`/`_PASSWORD`      | required                                                         | loadtest - `loadtest` role: full (`ALL`) rights, but only on `CLICKHOUSE_LOADTEST_DATABASE` below, never on `CLICKHOUSE_DATABASE`                                                                                                                                                                                                                     |
| `CLICKHOUSE_LOADTEST_DATABASE`              | required (defaults to `loadtest` via `make init`/`.env.example`) | Dedicated database load tests write into so they never touch real data in `CLICKHOUSE_DATABASE`. Created once by `make init`, never by `loadtest-runner` - it only checks the database already exists before every run and alerts if it doesn't (see "Running the load test" in `AGENTS.md`), then wipes it clean (safe: it never holds real data)    |
| `CLICKHOUSE_BOOTSTRAP_USER`                 | required                                                         | clickhouse, clickhouse-migrate - image-provisioned admin (`access_management=1`); `make init` uses it once to bootstrap the five SQL-managed roles above, `clickhouse-migrate` uses it on every startup for its own schema DDL (migrations, dashboard Dictionaries) - see below                                                                       |
| `CLICKHOUSE_BOOTSTRAP_PASSWORD`             | required                                                         | clickhouse, clickhouse-migrate                                                                                                                                                                                                                                                                                                                        |
| `CLICKHOUSE_VERSION`                        | pinned in `VERSION.yml`                                          | build arg for `services/clickhouse/Dockerfile` and `services/backup/Dockerfile` - single source of truth so the server image and the apt-pinned `clickhouse-client` in the backup image never drift apart. Not a `.env` var at all - `docker-compose.yml` requires it (`:?...`) with no fallback, so it's only ever present when set by `make` (via `VERSION.yml`); a raw `docker compose` invocation fails loudly instead of silently building an unpinned version                                                                                                                                             |
| `CLICKHOUSE_HOST`                           | `clickhouse`                                                     | webhook, mcp-server, grafana                                                                                                                                                                                                                                                                                                                          |
| `CLICKHOUSE_PORT`                           | `8123` (fixed, not user-configurable)                            | webhook, mcp-server, grafana - internal docker-network connection to `clickhouse`, never proxied by nginx                                                                                                                                                                                                                                             |
| `CLICKHOUSE_HTTP_PORT`                      | `8123`                                                           | host port mapping for clickhouse's HTTP interface                                                                                                                                                                                                                                                                                                     |
| `CLICKHOUSE_NATIVE_PORT`                    | `9000`                                                           | host port mapping for clickhouse's native protocol                                                                                                                                                                                                                                                                                                    |
| **Redis**                                   |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `REDIS_HOST`                                | `redis`                                                          | webhook, webhook-worker - queue between the two, see "How data flows" above                                                                                                                                                                                                                                                                           |
| `REDIS_PORT`                                | `6379` (fixed, not user-configurable)                            | webhook, webhook-worker - `redis` has no `ports:`/`expose:` at all, never visible on the host                                                                                                                                                                                                                                                         |
| **Webhook**                                 |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `CAPTURE_ENABLED`                           | `false`                                                          | webhook - write every event to its own file under `CAPTURE_DIR`, see "Inspecting captured traffic" below. Off by default: real prompt/response content, and one file per event adds disk I/O to the hot path. Set `CAPTURE_ENABLED=true` in `.env` to enable - `docker-compose.yml`'s `x-webhook-env` anchor reads it as `${CAPTURE_ENABLED:-false}`. |
| `CAPTURE_DIR`                               | `/app/captures`                                                  | webhook - only read when `CAPTURE_ENABLED=true`. Not wired into `docker-compose.yml`'s `webhook` service - only the in-code default in `config.py` applies, there's no `.env` override for this one.                                                                                                                                                  |
| `WEBHOOK_PORT`                              | `8010`                                                           | host port mapping for webhook                                                                                                                                                                                                                                                                                                                         |
| `WEBHOOK_URL`                               | `http://load-balancer:8000/api/v1/metrics`                       | litellm - where it POSTs the `StandardLoggingPayload` for each call                                                                                                                                                                                                                                                                                   |
| **MCP server**                              |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `MCP_SERVER_PORT`                           | `8001`                                                           | host port mapping for mcp-server                                                                                                                                                                                                                                                                                                                      |
| **Grafana**                                 |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `GRAFANA_PORT`                              | `3000`                                                           | host port mapping for grafana                                                                                                                                                                                                                                                                                                                         |
| **LiteLLM**                                 |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `LITELLM_PORT`                              | `4000`                                                           | host port mapping for litellm                                                                                                                                                                                                                                                                                                                         |
| `LITELLM_MASTER_KEY`                        | required                                                         | litellm - admin credential for `/ui` and `/key/generate`; real Anthropic/OpenAI keys and per-person virtual keys are managed through the UI instead, see "LiteLLM" below                                                                                                                                                                              |
| `LITELLM_DB_PASSWORD`                       | required                                                         | litellm, litellm-db - Postgres password for LiteLLM's own virtual-keys/budgets database                                                                                                                                                                                                                                                               |
| `LITELLM_BASE_URL`                          | `http://litellm:4000`                                            | webhook - internal docker network address for calling LiteLLM's `/key/info`. Not user-configurable, fixed in `docker-compose.yml`.                                                                                                                                                                                                                    |
| `LITELLM_URI`                                | `http://localhost:$LITELLM_PORT`                                 | `Makefile` only, not `docker-compose.yml` - full proxy URI override for `make setup-client`'s printed config, when LiteLLM isn't reachable at localhost (a shared/remote host); takes precedence over `LITELLM_PORT` wherever a URL is needed                                                                                                       |
| **Git branch reporting hook**               |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `AGENT_CLI_TRACKING_API_URL`                | required                                                         | `hooks/report_git_branch.py` - not a `docker-compose.yml`/`.env` variable, exported into your shell instead (see `make setup-client` and "Git branch/repo" below). No fallback: if unset, the hook crashes (`KeyError`, non-zero exit) instead of guessing a URL.                                                                                              |
| `LITELLM_VIRTUAL_KEY`                       | required                                                         | `hooks/report_git_branch.py` - personal virtual key (see `make setup-client`), sent as `Authorization: Bearer` on every git-branch report; webhook verifies it against LiteLLM's own `/key/info` before accepting the report. No fallback: hook crashes if unset, same as `AGENT_CLI_TRACKING_API_URL`.                                                        |
| **Observability**                           |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `PROMETHEUS_PORT`                           | `9090`                                                           | host port mapping for prometheus's own web UI (`/graph`, `/targets`) - opt-in `observability` profile, see "Observability" below.                                                                                                                                                                                                                     |
| **Langfuse**                                |                                                                  |                                                                                                                                                                                                                                                                                                                                                       |
| `LANGFUSE_PORT`                             | `3001`                                                           | host port mapping for langfuse-web                                                                                                                                                                                                                                                                                                                    |
| `LANGFUSE_CLICKHOUSE_HTTP_PORT`             | `8124`                                                           | host port mapping for langfuse-clickhouse's HTTP interface (separate instance from the agent-tracking `clickhouse` service, deliberately not `8123`)                                                                                                                                                                                                  |
| `LANGFUSE_CLICKHOUSE_NATIVE_PORT`           | `9001`                                                           | host port mapping for langfuse-clickhouse's native protocol (deliberately not `9000`)                                                                                                                                                                                                                                                                 |
| `LANGFUSE_CLICKHOUSE_USER`                  | `langfuse`                                                       | langfuse-clickhouse, langfuse-web, langfuse-worker                                                                                                                                                                                                                                                                                                    |
| `LANGFUSE_CLICKHOUSE_PASSWORD`              | empty                                                            | langfuse-clickhouse, langfuse-web, langfuse-worker - set before enabling the profile                                                                                                                                                                                                                                                                  |
| `LANGFUSE_DB_PASSWORD`                      | empty                                                            | langfuse-db, langfuse-web, langfuse-worker - Postgres password for Langfuse's own metadata database                                                                                                                                                                                                                                                   |
| `LANGFUSE_REDIS_PASSWORD`                   | empty                                                            | langfuse-redis, langfuse-web, langfuse-worker                                                                                                                                                                                                                                                                                                         |
| `LANGFUSE_MINIO_ROOT_USER`                  | `minio`                                                          | langfuse-minio, langfuse-web, langfuse-worker - S3-compatible blob store for ingested events/media                                                                                                                                                                                                                                                    |
| `LANGFUSE_MINIO_ROOT_PASSWORD`              | empty                                                            | langfuse-minio, langfuse-web, langfuse-worker                                                                                                                                                                                                                                                                                                         |
| `LANGFUSE_SALT`                             | empty                                                            | langfuse-web, langfuse-worker - `openssl rand -hex 16`                                                                                                                                                                                                                                                                                                |
| `LANGFUSE_ENCRYPTION_KEY`                   | empty                                                            | langfuse-web, langfuse-worker - `openssl rand -hex 32`, must be exactly 64 hex chars or Langfuse refuses to boot                                                                                                                                                                                                                                      |
| `LANGFUSE_NEXTAUTH_SECRET`                  | empty                                                            | langfuse-web - `openssl rand -hex 32`                                                                                                                                                                                                                                                                                                                 |
| `LANGFUSE_INIT_ORG_ID`                       | `receipt-goblin`                                                 | langfuse-web - auto-provisioned org id on first boot, see "First boot / provisioning" under "Langfuse" below                                                                                                                                                                                                                                          |
| `LANGFUSE_INIT_ORG_NAME`                     | `Receipt Goblin`                                                 | langfuse-web - auto-provisioned org display name                                                                                                                                                                                                                                                                                                       |
| `LANGFUSE_INIT_PROJECT_ID`                   | `agent-tracking`                                                 | langfuse-web - auto-provisioned project id                                                                                                                                                                                                                                                                                                             |
| `LANGFUSE_INIT_PROJECT_NAME`                 | `agent-tracking`                                                 | langfuse-web - auto-provisioned project display name                                                                                                                                                                                                                                                                                                   |
| `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`_SECRET_KEY` | empty                                                        | langfuse-web - the exact key pair Langfuse creates for the auto-provisioned project; must be byte-for-byte identical to `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` above, see the row below                                                                                                                                                                   |
| `LANGFUSE_INIT_USER_EMAIL`                   | empty                                                            | langfuse-web - auto-provisioned admin user's login email                                                                                                                                                                                                                                                                                               |
| `LANGFUSE_INIT_USER_NAME`                    | `admin`                                                          | langfuse-web - auto-provisioned admin user's display name                                                                                                                                                                                                                                                                                              |
| `LANGFUSE_INIT_USER_PASSWORD`                | empty                                                            | langfuse-web - auto-provisioned admin user's login password                                                                                                                                                                                                                                                                                            |
| `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` | empty                                                            | litellm - what it authenticates to Langfuse with; must exactly match `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`SECRET_KEY` (`LANGFUSE_INIT_*` vars, see "Langfuse" above and `.env.example`). Left unset, `litellm` still runs fine - its `langfuse` callback just fails per-call (logged, non-fatal)                                                       |

ClickHouse auth is two-tier: `CLICKHOUSE_BOOTSTRAP_USER`/`CLICKHOUSE_BOOTSTRAP_PASSWORD` are provisioned by the ClickHouse image itself (`CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1` in `docker-compose.yml`'s `clickhouse` service) and used only to bootstrap the five real app roles; the `default` user is removed entirely (image behavior whenever a non-`default` user/password is configured).
`make init` (`services/init/init_clickhouse_users.py`) is the *only* place any of this gets created - using that bootstrap user, it runs `CREATE USER OR REPLACE` for every role in `services/init/config.yml` (`ingest`, `grafana`, `mcp`, `backup`, `loadtest`) and issues all of its grants, once, each scoped to only what that service needs (see the per-role rows above), not `GRANT ALL` the way a single shared user used to be. `clickhouse-migrate` (run on every `docker compose up`) never touches users/roles/grants at all - only `services/clickhouse/migrations/*.sql` - so a fresh volume's `docker compose up` without having run `make init` first fails loudly (`ACCESS_DENIED`/`AUTHENTICATION_FAILED`) rather than silently working; that's expected, not a bug.
`webhook`/`worker`/`webhook-reparse` connect as `ingest`, `grafana` as `grafana`, `mcp-server` as `mcp`, `backup` as `backup`, `loadtest` as `loadtest` (on its own separate `CLICKHOUSE_LOADTEST_DATABASE`).
All ten role `CLICKHOUSE_*_USER`/`_PASSWORD` vars plus the two `CLICKHOUSE_BOOTSTRAP_*` vars are required - `docker-compose.yml` refuses to start without them.
`make init` is the easiest way to set all of this up - see "Getting started" above.
`*_PORT` variables only change the **host** side of each port mapping - the container-internal port stays fixed, so services keep reaching each other over the `receipt-goblin` Docker network regardless of what you set these to.
`WEBHOOK_PORT`/`LITELLM_PORT`/`GRAFANA_PORT`/`MCP_SERVER_PORT`/`CLICKHOUSE_HTTP_PORT`/`CLICKHOUSE_NATIVE_PORT`/`PROMETHEUS_PORT`/`LANGFUSE_PORT` are all published by the single `load-balancer` (nginx) service now, not by `litellm`/`grafana`/`mcp-server`/`clickhouse`/`webhook-1`/`webhook-2`/`prometheus`/`langfuse-web` themselves - see "Gateway (`load-balancer`)" in `AGENTS.md`. `http://localhost:<port>` for each still works exactly as before; only the internal routing changed. `CLICKHOUSE_PORT`/`REDIS_PORT` above are the exception - both are internal-only (never proxied by nginx, never visible on the host), so they're fixed defaults, not user-configurable.

Each service also has a `mem_limit`: `clickhouse` 2g (paired with `services/clickhouse/config.d/memory.xml`'s 0.85 ratio so it respects the cgroup limit instead of trying to use host RAM), `litellm` 2g, `grafana` 512m (see the Grafana OOM row under "Troubleshooting" above), `redis` 768m (`--maxmemory 700mb` - see `AGENTS.md` "Why a queue in front of ClickHouse" for the sizing math), `mcp-server`/`webhook-worker` 256m each, `webhook` 128m, `litellm-db` 256m, `langfuse-web` 2g (see the langfuse-web OOM comment in `docker-compose.yml` - it's a full Next.js app, notably heavier than this stack's other services), `langfuse-worker` 768m, `langfuse-clickhouse` 1g, `langfuse-db`/`langfuse-minio`/`langfuse-redis` 256m each.

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
Model choice (`agent_usage.model`) is the closest proxy: cheaper/faster models are already picked per-agent via `model:` in an agent's frontmatter (e.g. `.claude/agents/litellm-tester.md` uses `claude-haiku-4-5`), and panels 16/17 already break cost/tokens down by model.

### Message-level text

`agent_events.raw_payload` carries the full `StandardLoggingPayload` minus `messages` (the ever-growing full conversation history - also on disk verbatim, one file per event under `services/webhook/captures/<session_id>/*.json`, when `CAPTURE_ENABLED` is on, see "Inspecting captured traffic" below).
`agent_messages` adds what's missing from that: the last user message's text and the model's own reply text for that call, via `_last_user_text()`/`_flatten_content()` in `clickhouse_ingest.py`.
A row is only written when at least one of `prompt_text`/`response_text` is non-empty.

### Git branch/repo (`session_git_branch`)

Every other table here is populated from LiteLLM's `StandardLoggingPayload`, which never sees the calling CLI's working directory or git state.
`session_git_branch` is the one exception: `hooks/report_git_branch.py` reads the branch via `git rev-parse --abbrev-ref HEAD` and the repo via the `origin` remote's URL (falling back to the working tree's toplevel directory name when there's no `origin`) in the session's `cwd`, and POSTs both straight to `webhook`'s `/api/v1/session-git-branch` route - bypassing LiteLLM entirely.
It runs at `SessionStart` (registered in `.claude/settings.json` and `.codex/hooks.json`) and, in Claude Code only, again on `CwdChanged` - Codex CLI has no equivalent hook event, so a Codex session still only reports once, at start.
That means it's still not fully live even in Claude Code: only a `cd`/directory switch re-triggers the hook, not a plain `git checkout` within the same directory.
Each report is authenticated: the hook sends its personal LiteLLM virtual key as `Authorization: Bearer <key>`, and `webhook` checks that key against LiteLLM's own `/key/info` (rejecting blocked or expired keys with a 401) before writing the row - reusing LiteLLM's existing key store instead of a separate signing scheme.
It needs `AGENT_CLI_TRACKING_API_URL` and `LITELLM_VIRTUAL_KEY` set in your shell to know where to POST to and to authenticate - `make setup-client` prints `export` lines for both (see "Issue yourself a personal key" above); neither has a fallback, so an unset/missing variable crashes the hook (`KeyError`, non-zero exit) rather than guessing a URL or skipping auth.

### MCP server (`mcp-server`)

Listens on `:8001/mcp` (FastMCP `streamable-http` transport). Two tools:

- `whatsup(hours: int = 24)` - three fixed queries (total tokens, total cost from `agent_usage.cost`, top 5 spenders). Read-only by construction - never runs arbitrary SQL from the model.
- `query(sql: str, max_rows: int = 200)` - arbitrary SQL from the model, for the `clickhouse-analyst` subagent (see `.claude/agents/clickhouse-analyst.md`) and ad hoc lookups. There's no separate read-only ClickHouse user (`docker-compose.yml` uses one shared user for webhook/mcp-server/grafana), so `_validate_readonly_sql()` in `server.py` is the only thing enforcing read-only: single statement, must start with `SELECT`/`WITH`, no DDL/DML keywords anywhere in the query (word-boundary matched, so it also catches them inside subqueries), no `system`/`information_schema`/`mysql` database access, no remote/file/URL/other-DB table functions (`remote`, `url`, `file`, `s3`, `mysql`, `postgresql`, etc. - these read data from outside ClickHouse entirely, a DDL/DML keyword check alone wouldn't catch them), and must reference at least one of this stack's own tables. Results are always wrapped in an outer `LIMIT` (default/max 200, hard cap 1000) so a forgotten `LIMIT` in the model's query can't return unbounded rows.

`src/server.py` exposes `app = mcp.streamable_http_app()` at module level, run via `uvicorn src.server:app` (see `services/mcp-server/Dockerfile`) - deliberately *not* mounted under a separate FastAPI app, since the official `mcp` SDK has a known bug there (session manager never initializes when `streamable_http_app()` is mounted as a sub-app, requests 404/507 - [modelcontextprotocol/python-sdk#1367](https://github.com/modelcontextprotocol/python-sdk/issues/1367)).
Same dev/prod split as `webhook` below: `docker-compose.yml` still `build`s `services/mcp-server/Dockerfile` (deps baked into the image), then bind-mounts `services/mcp-server/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build mcp-server`. Built and run standalone (no compose, no `--reload`), it's the same self-contained image `Dockerfile` describes.

### Frontmatter format

Subagents and Skills are identified differently by Claude Code (frontmatter `name:` vs. directory name - confirmed against actual Claude Code behavior, not assumed).
Both used to bake the version into that identifier itself (`<name>_v<version>`) - abandoned because a Skill's directory name *is* its invocation identifier, so bumping the version meant renaming the directory, which could break an in-flight session still referencing the old name.
The current convention instead keeps every identifier bare and permanent, and carries the version in a marker placed wherever Claude Code actually re-injects that file's content into the conversation - see `AGENTS.md` for why each location was chosen.

**Subagents** (`.claude/agents/*.md`) - frontmatter `name:` is the actual invocation identifier (the filename doesn't have to match) and stays bare forever.
The version goes in a `<agent_version>X.Y.Z</agent_version>` marker as the very first thing in `description:` - that text is injected verbatim into every call's `messages` via the "Available agent types" system-reminder listing, confirmed against a real captured payload (the agent's own body/system-prompt is *not* logged).

```
---
name: test-researcher
description: >
  <agent_version>1.0.0</agent_version> Minimal test agent that searches for information and produces a short summary.
---
```

**Skills** (`.claude/skills/<dirname>/SKILL.md`) - the *directory name* is the invocation identifier (`/<dirname>`) and stays bare forever; frontmatter `name:` is purely a cosmetic display label and does not affect invocation.
Same marker convention as Subagents, `<skill_version>X.Y.Z</skill_version>` as the first thing in `description:`, surfaced the same way via the "available skills" listing:

```
---
name: test-linter
description: >
  <skill_version>2.0.0</skill_version> Minimal test skill that checks a file for obvious style issues.
---
```

**Commands** (`.claude/commands/*.md`) - the filename is the invocation identifier (`/foo` for `foo.md`) and stays bare forever.
There's no persistent "available commands" listing the way agents/skills have, so the marker instead lives in the command file's own body (after the frontmatter) as `<command_version>X.Y.Z</command_version>` - that body text is what gets expanded into the triggering message alongside the `<command-name>` tag Claude Code already injects:

```
---
description: Report token/cost spend and top spenders from the last 24h, from ClickHouse
---

<command_version>1.0.0</command_version>

# whatsup
...
```

A newly-created Subagent/Skill/Command starts with no version marker at all - only added once it has shipped and is being edited again.
A self-named/ad-hoc agent (`general-purpose`, `Explore`, `Plan`, ...) has no backing file and no marker either; version comes back blank in both cases.

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
`docker compose logs -f webhook` shows one log line per exception raised while enqueueing (`queue_client.enqueue` - Redis-availability issues, not parsing, since `webhook` no longer parses anything; never re-raised, so a bad payload never breaks LiteLLM's ack); `docker compose logs -f webhook-worker` shows the same for both the parsing side (`build_event`, called from `worker.py`'s `_decode_into`) and the batched-insert side (`ingest_events_batch`). `redis-cli -h localhost XLEN webhook:events` shows the current backlog - non-zero-but-draining is normal, non-zero-and-growing means `webhook-worker` has fallen behind or died. Setting `CAPTURE_ENABLED=true` (off by default, see "Configuration" below) also lands every event verbatim as its own file under `services/webhook/captures/<session_id>/` for offline replay (see "Inspecting captured traffic" below).

## LiteLLM

A local LiteLLM gateway (`litellm` + `litellm-db` + `webhook` services in `docker-compose.yml`) sits in front of both CLIs so their traffic can be logged, and centrally billed, before it leaves the machine.
This gateway *is* how the ClickHouse tracking stack described above gets its data now - `webhook` is the only ingestion path (see "How data flows" above).

The model names are meant to be stable regardless of what's actually billing them: `claude-sonnet-5`/`claude-haiku-4-5`/`claude-opus-4-8`/`claude-fable-5`/`gpt-5-codex`/`gpt-5` are what you pick in Claude Code's own model selector, put in agent/skill frontmatter `model:` fields, and set as Codex CLI's model - everywhere - and that stays true whether a name is currently backed by OAuth passthrough (no Anthropic key on hand yet) or a real, centrally-held provider key added later through the admin UI.
People get a personal LiteLLM *virtual key* either way, and per-key budgets/rate-limits/model access are enforced entirely by LiteLLM - see "Issue yourself a personal key and route a coding agent through the proxy" under "Getting started" above.
`litellm-db` (Postgres) is what makes virtual keys persistent - without a database, LiteLLM either refuses to generate them or keeps them in memory only, gone on the next restart.

### Model name mapping

The whole point of picking `model_name` values up front is that agent/skill frontmatter and both CLIs' model settings reference these same names, unaware of what's actually behind them:

| Virtual name (use everywhere)                | Real model                   | Backend right now                                                                                   |
| -------------------------------------------- | ---------------------------- | --------------------------------------------------------------------------------------------------- |
| `claude-sonnet-5`                            | `anthropic/claude-sonnet-5`  | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet)                            |
| `claude-haiku-4-5`                           | `anthropic/claude-haiku-4-5` | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet)                            |
| `claude-opus-4-8`                            | `anthropic/claude-opus-4-8`  | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet)                            |
| `claude-fable-5`                             | `anthropic/claude-fable-5`   | OAuth passthrough, `services/litellm/config.yaml` (no Anthropic key yet)                            |
| `gpt-5-codex`                                | `openai/gpt-5-codex`         | Not defined yet - needs a real `OPENAI_API_KEY`                                                     |
| `gpt-5`                                      | `openai/gpt-5`               | Not defined yet - needs a real `OPENAI_API_KEY`                                                     |
| `gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.6-luna` | `chatgpt/gpt-5.6-*`          | OAuth passthrough, `services/litellm/config.yaml` (no OpenAI key, Codex's own ChatGPT subscription) |

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
Adding a third remote source never needs a `docker-compose.yml` or `docker-entrypoint.sh` change - just add a `.yaml` file under `user_configs/` (same shape as `config.yaml.tmpl`). In dev (`docker-compose.dev.yml` bind-mounts `user_configs/` back), a restart of the `litellm` container picks it up. In prod, `user_configs/*.yaml` is baked into the image at build time along with everything else - drop the real file(s) in place first, then `docker compose build litellm` (or `make build SERVICE=litellm`) and recreate.

The Ollama tag behind `ollama/reasoning`/`ollama/embeddings` must already be pulled on the Ollama host (`ollama pull gemma3:4b`, `ollama pull embeddinggemma:300m`) - LiteLLM doesn't pull models itself.
Ollama must also be listening on `0.0.0.0`, not just `localhost`, on its own host, or the `litellm` container can't reach it across the LAN.
`reranker`'s `model` must use LiteLLM's `huggingface/<repo>` provider prefix, e.g. `huggingface/BAAI/bge-reranker-v2-m3` - LiteLLM's `huggingface/` rerank provider speaks the raw HuggingFace Text Embeddings Inference (TEI) wire protocol to `api_base` - request `{query, texts: [...]}`, response a bare JSON array `[{index, score}]` (no `results` wrapper, unlike Cohere-style rerank APIs) - so the host just needs to be a TEI-compatible rerank server, no LiteLLM-side transformation code is needed.

### Right now: no Anthropic/OpenAI key yet

`claude-sonnet-5`/`claude-haiku-4-5`/`claude-opus-4-8`/`claude-fable-5` are defined in `services/litellm/config.yaml`'s `model_list` with no `api_key` - `model_group_settings.forward_client_headers_to_llm_api` forwards the caller's own `claude login` subscription token straight to Anthropic instead.
`gpt-5-codex`/`gpt-5` (the plain OpenAI API models) have no equivalent (OpenAI's API has nothing like Anthropic's OAuth passthrough), so they simply don't exist yet - add them once a real `OPENAI_API_KEY` shows up.
`gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.6-luna` are different: they route through litellm's own `chatgpt` provider (a Codex/ChatGPT-subscription backend, not the plain OpenAI API), and *do* have OAuth passthrough today - see "Routing Codex CLI through it" below.

### Routing Claude Code through it

`make setup-client` (see "Getting started" above) prints `export` statements with a `<virtual key>` placeholder for `ANTHROPIC_BASE_URL`/`ANTHROPIC_CUSTOM_HEADERS`/etc. (`LITELLM_PORT` in `.env` control the URL if you changed it from the default). Model choice isn't part of this - Claude Code picks its own model through its normal interface, same as always.
Then `claude login` (subscription OAuth, Pro/Max/Team) as usual.

`ANTHROPIC_CUSTOM_HEADERS` is required even though nothing else guards these routes: without a distinct header proving something *else* authenticated to LiteLLM, it can't tell the incoming `Authorization` (the subscription token) apart from its own auth and strips it before forwarding - Anthropic then replies `x-api-key header is required` (see [BerriAI/litellm#19618](https://github.com/BerriAI/litellm/issues/19618)).
`general_settings.litellm_key_header_name: x-litellm-api-key` in `services/litellm/config.yaml` is what makes LiteLLM read the virtual key from that header, checking it against the budget/model/rate-limit rules on the key, independently of whatever gets forwarded to Anthropic.

### Routing Codex CLI through it

For a real, billed `OPENAI_API_KEY` backing `gpt-5-codex`/`gpt-5` (the plain OpenAI API), issue a personal virtual key the same way (**Keys** → **Create New Key**, `Models` restricted to `gpt-5-codex`/`gpt-5`) once that key exists - Codex reads `OPENAI_API_BASE`/`OPENAI_API_KEY` directly, no custom header needed on that side.

Without an OpenAI API key, `gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.6-luna` give the same OAuth-passthrough deal Claude Code already has, using each caller's own ChatGPT Plus/Pro/Team subscription instead:

1. `~/.codex/config.toml`: point the `litellm` model provider at the proxy and require it to always send a live subscription token as `Authorization` -
   ```toml
   model_provider = "litellm"

   [model_providers.litellm]
   name = "LiteLLM"
   base_url = "http://localhost:4000"
   wire_api = "responses"
   requires_openai_auth = true
   env_http_headers = { "x-litellm-api-key" = "LITELLM_AUTH_HEADER" }
   ```
   `model_provider` at the top level sets the default for every profile/session; use a `[profile]` block instead (`model_provider = "litellm"` plus `model = "gpt-5.6-luna"`) if you only want this active under a named profile rather than by default.
   `LITELLM_AUTH_HEADER` here is the same personal virtual key already exported for Claude Code (see "Routing Claude Code through it" above) - one key, both CLIs.
2. `codex login` (ChatGPT subscription OAuth) as usual.

Why this needs more than `forward_client_headers_to_llm_api`: unlike Anthropic, litellm has no built-in way to recognize a ChatGPT/Codex OAuth token (confirmed against litellm's own docs and two of its open GitHub issues - [BerriAI/litellm#23777](https://github.com/BerriAI/litellm/issues/23777), [#24500](https://github.com/BerriAI/litellm/issues/24500) - both ask for exactly this, unresolved upstream), so its `clean_headers()` proxy internals silently drop a Codex caller's `Authorization` header the same way they'd drop any other unrecognized bearer token. `services/litellm/custom_callbacks.py` closes this the same way the Anthropic case is special-cased in litellm itself: it broadens `is_anthropic_oauth_key()` (monkeypatched at callback-load time) to also recognize a ChatGPT JWT by its `https://api.openai.com/auth` claim, then a pre-call hook (`ChatGPTAuthForwardHandler`) reads the now-surviving header and sets it as `extra_headers` so the caller's own token (and derived `ChatGPT-Account-Id`) - not litellm's own single logged-in identity - is what actually authenticates to `chatgpt.com`, per call.
`docker-entrypoint.sh` seeds a static, non-functional `auth.json` for litellm's built-in `chatgpt` provider on every container start, purely so a call that arrives with no forwarded token fails cleanly with a real auth error instead of hanging on an interactive device-code login.

### Configuring via config files instead of shell exports

Model routing itself doesn't need a shell rc file - both CLIs can read it straight from their own config file instead. Whether the *shell exports* can also be retired depends on the CLI, since that's really about `hooks/report_git_branch.py`'s two vars (`AGENT_CLI_TRACKING_API_URL`, `LITELLM_VIRTUAL_KEY`), not routing:

- **Claude Code** hooks are spawned by the Claude Code process itself, and inherit whatever it was given - including a settings file's `env` block. So putting all four vars there (below) genuinely replaces `~/.zshrc`/`~/.bashrc` for Claude Code.
- **Codex** hooks (`.codex/hooks.json`'s `SessionStart`) just inherit the environment of whatever shell launched `codex` - there's no config.toml equivalent of Claude's `env` block that injects vars into Codex's own process or its hooks. (`shell_environment_policy` looks like it might do this but doesn't - it only filters what the *agent's own shell tool calls* see, a separate mechanism from hooks entirely.) So the `export AGENT_CLI_TRACKING_API_URL=...`/`export LITELLM_VIRTUAL_KEY=...` lines still need to land in your shell rc for Codex, config.toml or not.

`make setup-client` prints all of this pre-filled - the shell-export lines (headed `~/.zshrc / ~/.bashrc`, since that's where those specifically go), plus a block per CLI below - substituting your real values from `.env` (`LITELLM_PORT`/`AGENT_CLI_TRACKING_API_URL` if set, `LITELLM_VIRTUAL_KEY` if you've added it there, see `.env.example`) wherever they're already known, so there's normally nothing to hand-type from this section at all.

Values in `~/.claude/settings.json`'s `env` block are plain literal strings - Claude Code doesn't expand `$VAR`/`${VAR}` references there (confirmed against Anthropic's own open feature request for this, [anthropics/claude-code#46889](https://github.com/anthropics/claude-code/issues/46889) - unimplemented as of writing). So the virtual key has to be the actual value, not a reference to whatever's already exported in your shell; that's also why `make setup-client` substitutes it in directly rather than printing something like `"$LITELLM_VIRTUAL_KEY"`.

**Claude Code**:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000",
    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: Bearer <virtual key>",
    "AGENT_CLI_TRACKING_API_URL": "http://localhost:8010",
    "LITELLM_VIRTUAL_KEY": "<virtual key>"
  }
}
```

Put this in `~/.claude/settings.json` to route *every* Claude Code session on the machine through litellm, or in this repo's `.claude/settings.local.json` (gitignored - real keys should never land in the committed `.claude/settings.json`) to scope it to just this project. Either way, **merge** the `env` block into the file - don't replace it wholesale. Both the global file and this repo's own `.claude/settings.json`/`.codex/hooks.json` already carry hooks and MCP-server config (`SessionStart`/`CwdChanged`/`PreToolUse` entries, `enabledMcpjsonServers`, etc.) that a full overwrite would silently drop - open those files and see what sections are already there before writing.

**Codex** (routing only - the shell exports above are still required separately, see above):

```toml
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM"
base_url = "http://localhost:4000"
wire_api = "responses"
requires_openai_auth = true
env_http_headers = { "x-litellm-api-key" = "LITELLM_AUTH_HEADER" }
```

This has to go in the *global* `~/.codex/config.toml` - a project-level `.codex/config.toml` (this repo has one, currently just `mcp_servers.clickhouse`) can't take over routing even in a trusted project. Codex silently ignores `model_provider`/`model_providers`/`profile` set at that layer, precisely so a repo can't redirect your traffic on its own. Check what's already in your global `~/.codex/config.toml` (and this repo's own `.codex/hooks.json`) before merging this block in, rather than overwriting.

### Inspecting captured traffic

`webhook` logs one line per captured/enqueued payload (or per exception, see "Debugging ingestion" above) - `docker compose logs -f webhook-1 webhook-2` while driving a session through either CLI.
It listens on host port `8010` via `load-balancer` (nginx - see "Gateway (`load-balancer`)" in `AGENTS.md`), which load-balances across the two stateless `webhook-1`/`webhook-2` replicas; internally on the `receipt-goblin` Docker network that's `load-balancer:8000`, not a single `webhook:8000` hostname.

Set `CAPTURE_ENABLED=true` in `.env` (off by default - see "Configuration" below) to also have every event land as its own JSON file under `services/webhook/captures/` on the host (bind-mounted, not a Docker volume - `ls services/webhook/captures/` works directly, no `docker exec` needed), raw as received.
`log_format: json_array` in `services/litellm/config.yaml` means a single POST is usually a list of several `StandardLoggingPayload` objects, not one - `server.py` splits that list apart and writes one file per event rather than one file per POST, so a bursty batch doesn't have to be buffered in memory as a single growing list before anything hits disk.
Files are grouped into a subfolder per session - `services/webhook/captures/<session_id>/` (the same `x-claude-code-session-id`/`trace_id`/`litellm_call_id` fallback chain `clickhouse_ingest.py`'s `_session_and_trace_id` uses elsewhere, so grouping matches every other session-scoped view in this stack; falls back to a fixed `unknown/` folder when none of those resolve) - and each filename is a creation-time-sortable `<UTC timestamp with microseconds>-<8 hex chars>.json`, so `ls services/webhook/captures/<session_id>/ | sort` replays a session's events in order.
This directory is gitignored - it's real prompt/response content, not something to commit.
`docker-compose.yml` still `build`s `services/webhook/Dockerfile` (deps baked into the image), then bind-mounts `services/webhook/src` over the image's `/app/src` and overrides `command:` to add `--reload` - editing `src/server.py` restarts the server without a rebuild, but changing `requirements.txt` does need `docker compose build webhook`. `captures/` is mounted separately (it's runtime output, not source) so it lands on the host either way.
Built and run standalone (no compose, no `--reload`, no bind mounts) - `docker build -t webhook . && docker run -p 8000:8000 webhook` - it's the same self-contained image `Dockerfile` describes.

## Langfuse

A self-hosted [Langfuse](https://langfuse.com) v3 (`langfuse-web` + `langfuse-worker` + its own `langfuse-db`/`langfuse-clickhouse`/`langfuse-redis`/`langfuse-minio` - six services, fully separate from the agent-tracking stack above so schemas/versions never collide) gives a UI for LLM tracing/observability - full request/response per call, cost, latency, and errors, browsable and searchable, which ClickHouse/Grafana above don't provide (that stack is built for aggregate metrics, not reading individual call bodies).

It's fed the same way `webhook` is: `services/litellm/config.yaml`'s `litellm_settings.success_callback`/`failure_callback` include `langfuse`, one of LiteLLM's built-in logging integrations - it needs no `callback_settings` block, just `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST` in the `litellm` container's environment (set in `docker-compose.yml` from `.env`). Both integrations run off the exact same LiteLLM calls independently - Langfuse being down doesn't affect ClickHouse ingestion or vice versa.

### It's optional - the `langfuse` compose profile

All six Langfuse services carry `profiles: [langfuse]` in `docker-compose.yml`, so a plain `docker compose up -d` **never starts them** - only the core stack (`clickhouse`, `redis`, `webhook`, `webhook-worker`, `grafana`, `litellm`, `litellm-db`, `mcp-server`) comes up by default. Langfuse never starts automatically via `make up` or `make start` - start it explicitly via `make langfuse-up`:

```sh
docker compose --profile langfuse up -d --build langfuse-web langfuse-worker langfuse-db langfuse-clickhouse langfuse-minio langfuse-redis
```

However, `make down`/`make stop` call `make langfuse-down` automatically as a courtesy - it brings just those six containers down (`docker compose --profile langfuse down <the six services>` - listing them explicitly matters, since a bare `docker compose --profile langfuse down` with no service args tears down the core stack too, as `--profile langfuse` activates Langfuse *in addition to* default no-profile services). Run `make langfuse-up`/`make langfuse-down`/`make langfuse-logs` directly if you want to manage Langfuse on its own without touching the core stack, or drop straight to `docker compose --profile langfuse up -d` / `--profile langfuse down` if you're not using `make` at all (the latter takes the whole stack down together, core included).

Because of this, every `LANGFUSE_*` var - both the six-services-internal ones (`LANGFUSE_CLICKHOUSE_USER`/`PASSWORD`, `LANGFUSE_DB_PASSWORD`, `LANGFUSE_REDIS_PASSWORD`, `LANGFUSE_MINIO_ROOT_USER`/`PASSWORD`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`, `LANGFUSE_NEXTAUTH_SECRET`) and the ones `litellm` itself reads (`LANGFUSE_PUBLIC_KEY`/`SECRET_KEY`) - default to empty (`${VAR:-}`) rather than the `${VAR:?...}` required-and-fail-fast pattern used everywhere else in this file. That's deliberate: unlike `CLICKHOUSE_PASSWORD` or `LITELLM_MASTER_KEY`, nothing else in the stack needs these to boot, and `docker compose` interpolates env vars for every service defined in the file regardless of which profiles are active - a `:?` here would break `docker compose up` for anyone who hasn't set up Langfuse at all, profile or not. Leaving them unset just means: Langfuse containers won't start (no profile → moot) and, if you *do* start `litellm` without ever touching Langfuse, its `langfuse` success/failure callback quietly fails per-call (logged, not fatal - LiteLLM itself still works) since it has nothing to authenticate with. Fill in `.env` (see `.env.example`) before enabling the profile for real.

### Session grouping

Langfuse groups traces by `metadata.session_id` on the request, which nothing sets by default - without it, every call would show up as its own disconnected trace instead of grouped per CLI session, the way ClickHouse's `session_id` already groups rows (see `_session_and_trace_id` in `AGENTS.md`). `services/litellm/custom_callbacks.py` (`SessionIdHandler`, wired in via `litellm_settings.callbacks: custom_callbacks.session_id_handler`) runs pre-call and copies the same `x-claude-code-session-id` header ClickHouse already reads into `metadata.session_id`, so Langfuse's session view lines up with the same sessions Grafana's `$session_id` variable does.
`docker-entrypoint.sh` copies `custom_callbacks.py` next to the merged effective config in `/tmp` (not just `/app/litellm-config`) since LiteLLM resolves a bare `custom_callbacks.session_id_handler` module path relative to whichever config file it was actually started with.

### First boot / provisioning

`LANGFUSE_INIT_*` env vars (`LANGFUSE_INIT_ORG_ID`, `LANGFUSE_INIT_PROJECT_ID`, `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`SECRET_KEY`, `LANGFUSE_INIT_USER_EMAIL`/`PASSWORD`, etc. - see `.env.example`) make `langfuse-web` auto-provision an org, project, admin user, and API key pair on its very first boot (empty `langfuse-db`) - no manual `/setup` wizard. `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (what `litellm` authenticates with) **must be byte-for-byte identical** to `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`/`SECRET_KEY` - the latter is what makes Langfuse create exactly that key pair, not a separately-issued credential.
This only runs against an empty database - changing `LANGFUSE_INIT_*` after the first boot has no effect; manage the org/project/keys through the UI at that point instead.

### Open Langfuse

http://localhost:3001 (`LANGFUSE_PORT`, default `3001` - `3000` is already Grafana's), log in with `LANGFUSE_INIT_USER_EMAIL`/`LANGFUSE_INIT_USER_PASSWORD`.

### Two known first-boot gotchas already fixed here

Both were hit and fixed once already in this stack's `docker-compose.yml` - worth knowing about if you ever bump the `langfuse/langfuse` image and see the same symptoms again:

- **`langfuse-web` OOM at `mem_limit: 1g`.** It's a full Next.js app, notably heavier than this stack's other services - 1g gets heap-killed partway through its init-scripts pass (right after the "MCP feature registered" log lines). Fixed by raising to `2g`.
- **Healthcheck/`localhost` connection refused despite the app logging "Ready".** Next.js's standalone `server.js` binds to `$HOSTNAME` if it's set, and Docker auto-sets `HOSTNAME` to the container ID - so without an override it listens only on the container's actual IP, not `127.0.0.1`/`localhost`, and `wget http://localhost:3000/...` (or anything else hitting `localhost`) gets connection refused. Fixed two ways: `HOSTNAME: "0.0.0.0"` in `langfuse-web`'s `environment:` makes it bind everywhere, and the healthcheck itself targets `http://127.0.0.1:3000/...` explicitly rather than `localhost` (the container also has no IPv6 listener, and `localhost` resolves to `::1` first).

### Restarting `litellm` to pick up a config change

Editing `services/litellm/config.yaml`/`custom_callbacks.py` needs a `litellm` restart to take effect - **but don't do this without asking first**, even for a config-only change: `litellm` is the live proxy every CLI session on the machine currently routes through, and restarting it drops in-flight requests for anyone else using it right now (see `AGENTS.md` "Rules to not violate").
And it has to be `docker compose up -d litellm` (recreate), not `docker compose restart litellm` - `restart` reuses the container's existing environment snapshot and does **not** pick up new/changed `environment:` entries from `docker-compose.yml` (this is how `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` ended up missing the first time Langfuse was wired in here - a `restart` after adding them to the compose file left the running container without them, so `langfuse` callback had nothing to authenticate with and produced zero traces despite looking "restarted"). `docker compose config litellm | grep -i langfuse` (or `docker exec receipt-goblin-litellm env | grep -i langfuse`) is a quick way to confirm the running container actually has them.

## Observability

A second, fully separate opt-in layer - infra-level metrics and logs (CPU, memory, container health, HTTP uptime, container stdout/stderr) rather than the application-level agent/LLM data ClickHouse+Grafana above track.
Seven services, all `services/{prometheus,blackbox,redis-exporter,cadvisor,node-exporter,loki,alloy}/`, gated behind the `observability` compose profile so a plain `make start` or `make up` never brings them up.

| Service          | Responsible for                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `prometheus`     | Metrics store and scraper. Config baked in at build time (`services/prometheus/prometheus.yml`) - pulls `/metrics` every 5s from `clickhouse`, `webhook`, `mcp-server`, `webhook-worker`, `redis-exporter`, `litellm`, `cadvisor`, `node-exporter`, plus a `blackbox` job that proxies HTTP probes through `blackbox` itself.                                                                                                                                                                                                |
| `blackbox`       | `blackbox_exporter` - answers Prometheus's `blackbox` job's `/probe` requests by HTTP-probing each service's own health endpoint (`webhook`/`mcp-server`/`clickhouse`/`grafana`/`litellm`, `http_2xx` module defined in `services/blackbox/config.yml`) and reporting up/down + latency, independent of whether that service's own `/metrics` endpoint is reachable.                                                                                                                                                         |
| `redis-exporter` | Stock `oliver006/redis_exporter` image. Server-level Redis stats only - memory, connected clients, commands processed. Business-level stream metrics (queue depth, consumer lag on `webhook:events`) come from `webhook-worker`'s own `/metrics` instead, since `redis_exporter` can't see those.                                                                                                                                                                                                                            |
| `cadvisor`       | Stock `gcr.io/cadvisor/cadvisor` image. Per-container cgroup metrics (CPU/memory/network per `receipt-goblin-*` container), read-only host mounts (`docker.sock`, `/sys`, `/var/lib/docker`, ...) for that, no config of its own. Broken under the containerd-snapshotter storage driver (current Docker Desktop default, also selectable under Colima) - see `AGENTS.md` "cAdvisor container-level metrics" for why and the fix options; kept anyway since it doesn't affect the other observability services.              |
| `node-exporter`  | Host-wide metrics (CPU/memory/disk/network) via read-only mounts of `/proc`/`/sys`/`/` - the Docker Desktop Linux VM on macOS, the real host on Linux. Feeds Grafana's "Host" tab, unaffected by `cadvisor`'s storage-driver issue above.                                                                                                                                                                                                                                                                                    |
| `loki`           | Log aggregation and storage. Config baked in at build time, single-binary, filesystem-backed (`loki-data` volume) - no object storage needed at this scale. Receives pushed logs from `alloy`, queried by Grafana's `Loki` datasource.                                                                                                                                                                                                                                                                                       |
| `alloy`          | Grafana Alloy - the log collector. Discovers every running container via a read-only `docker.sock` mount (`discovery.docker`, config baked in at `services/alloy/config.alloy`), tails each one's stdout/stderr, labels it by container name, and pushes it to `loki`. A passive sink, not scoped to just the `observability` profile's own services - core-stack and Langfuse container logs land in Loki too once those containers are up, with no logging changes needed on their end since they already write to stdout. |

Grafana picks these up through two extra provisioned datasources - `services/grafana/provisioning/datasources/` - `Prometheus` (`http://prometheus:9090`) and `Loki` (`http://loki:3100`), separate from the ClickHouse datasource the "Agents Overview" dashboard's own panels use.

`prometheus`'s own web UI (`/graph` for ad-hoc PromQL, `/targets` for scrape health) is gatewayed through `load-balancer` like every other service - `http://localhost:${PROMETHEUS_PORT:-9090}` once the `observability` profile is up, see "Gateway (`load-balancer`)" in `AGENTS.md`.

### Managing the observability stack

Unlike Langfuse, `make start`/`make up` does **not** bring these services up automatically - start them explicitly:

```bash
make observability-up
make observability-logs
make observability-status
make observability-down
```

`make stop`/`make down` do call `make observability-down` on teardown, so the profile never lingers once you stop the core stack.
