# Architecture rationale

Cross-cutting deep-dive reference for `AGENTS.md`.
Covers content spanning more than one service.
Single-service content (queue/worker split, gateway, per-file breakdowns, `fastjson` status) lives in `agent_docs/services/<name>.md` instead - see `AGENTS.md`'s Project Structure section for the per-service list.

## Dev/prod split

- `docker-compose.yml` is prod-default: no service has `command:`/`entrypoint:` or a source/config bind mount, only named data volumes and genuine host-system mounts (`docker.sock`, `/proc`, `/sys`).
  `services/litellm/user_configs/*.yaml` (gitignored, may carry real remote-model hosts/keys) is baked into the `litellm` image at build time even in prod, not bind-mounted.
  A changed source needs `docker compose build litellm` in prod.
- `docker-compose.dev.yml` is an override file layering everything needing live-editing: bind-mounted `services/*/src`, `config.yml`, Grafana's dashboards/provisioning, `litellm`'s `user_configs/` only, `--reload` for `webhook`.
  `mcp-dev` is the one exception - it has no definition in `docker-compose.yml` at all; `docker-compose.dev.yml` is its full service definition (build/image/environment/healthcheck/depends_on/networks), not just a bind-mount overlay.
  `mcp-stats` is not part of that exception - it's a normal prod service, fully defined in `docker-compose.yml`, with no dev override.
- `Makefile`'s `ENVIRONMENT` (`.env`, default `development`) picks which files `check-env` puts in `COMPOSE_FILES`: anything but exactly `production` layers both files, `production` uses `docker-compose.yml` alone.
  Every target depends on `check-env`, which prints `⚠️ ENVIRONMENT=...` first.
  `ENVIRONMENT` is captured from the shell/`.env` before `include .env` runs, then restored after.
  This stops a shell-exported `ENVIRONMENT=production make start` from being silently overwritten by `.env`'s own `development` default: `include`'s plain `=` assignment is file-origin, and GNU Make lets a file-origin assignment override an environment-origin variable - the reverse of what you'd expect.
- Static per-image config (Grafana provisioning, LiteLLM's `config.yaml`, `redis.conf`, etc.) is baked into each service's own image via `COPY` in its Dockerfile, in both dev and prod, unchanged.
  `webhook`/`webhook-worker`/`metrics-reparse`/`clickhouse-migrate`/`loadtest` used to be one image sharing runtime role selection via `APP_ROLE` (`services/webhook/Dockerfile` + `docker-entrypoint.sh`).
  The webhook-worker-split refactor (`plans/webhook-worker-split.md`) gave each its own independent Dockerfile/image/tag instead (`services/webhook/`, `services/worker/`, `services/reparse/`, `services/migrate/`, `services/loadtest/`), all pulling shared code from `services/_common/src/`.
  `litellm` and `redis` get no dev override at all - both environments always run the same baked image.

## Image tags

`VERSIONS.yml` holds each service's `SERVICE_TAG: X.Y.Z-{build}`.
`scripts/resolve_image_version.py` resolves these into `.image-tags.mk`.
`{build}` is the commit hash, except `observability`/`langfuse`, which use a static SEMVER.
Bump a tag when its Dockerfile or image code changes.

## Hybrid usage tracking

The stack has two source paths that converge before ClickHouse:

- Codex CLI/Desktop and normal Claude CLI calls go through LiteLLM.
  LiteLLM sends its full `StandardLoggingPayload` to `POST /api/v1/metrics`.
- Claude Desktop and Claude Code Remote Control keep their provider connection direct to Anthropic.
  A host-side transcript collector sends privacy-minimal `UsageEnvelopeV1` batches to authenticated `POST /api/v1/usage-events`.

Both endpoints enqueue onto the same Redis usage stream.
`webhook-worker` selects a source adapter, builds the existing ingest row bundle, and writes the same `agent_events`/`agent_usage`/`ingest_raw` tables.
Remote Control therefore keeps its native direct channel instead of depending on LiteLLM behavior it does not support.

### Client source attribution

Each source adapter resolves client attribution once before ClickHouse insertion.
The normalized result is stored as `client_product`, `client_surface`, and `ingest_path` on both `agent_events` and `agent_usage`.
`agent_usage.client_id` stores the same exact-client dictionary ID as `agent_events.event_client_id`, so token and cost panels can group client versions without joining the event table.

LiteLLM attribution accepts only allowlisted `x-rg-client-product`/`x-rg-client-surface` values, then checks the Codex originator, then falls back to known user-agent prefixes.
`codex_cli_rs/*` without an explicit Codex originator is intentionally `codex/unknown` because that user-agent is shared by CLI and Desktop.
Direct Claude attribution comes from the validated `UsageEnvelopeV1.source` value and records `claude_transcript` as its ingest path.

Grafana reads these persisted columns directly.
Its product, surface, and ingest-path variables filter token and cost panels without reclassifying raw payloads at query time.
Reparse uses the same adapters, so historical rows improve when their stored source data is unambiguous and stay `unknown` when it is not.

### Direct transcript collector

`scripts/claude_usage_collector.py` is installed as the global Claude `Stop`/`SubagentStop` hook.
It reads only complete JSONL transcript lines and advances a byte cursor per transcript.
The hook is fail-open and its network flush is time-bounded, so a collector or network failure cannot block Claude from stopping.

The collector writes extracted envelopes to a local SQLite WAL outbox before attempting HTTP delivery.
It keeps transient failures and removes a valid batch only after a successful response.
A `422` batch is retried one event at a time.
A permanently rejected single event is logged and removed so it cannot block the rest of the outbox.
Flushes default to 50 events per request and 100 events per second so a historical backfill cannot immediately overrun Redis's bounded stream.
The server independently caps a request at 100 events.

Delivery of valid events is at least once.
A timeout or partial batch enqueue can cause a retry of an already-accepted event.
The transcript `requestId` becomes `UsageEnvelopeV1.event_id` and then the existing `litellm_call_id` row key, so `ReplacingMergeTree` collapses retries for `agent_events`, `agent_usage`, and `ingest_raw`.
Duplicates can remain visible briefly until ClickHouse merges those parts.

### `UsageEnvelopeV1`

`services/_common/src/usage_envelope.py` owns the versioned direct-source contract.
It accepts only allowlisted source metadata and token counters.
Unknown fields are rejected, so prompt, response, message, and tool-argument content cannot enter this path.
The adapter also emits no `agent_messages` row.
`ingest_raw` stores the normalized envelope, not the original transcript line.

`POST /api/v1/usage-events` accepts one envelope or a batch of up to 100.
It validates the caller's LiteLLM virtual key through `/key/info` and replaces any client-supplied identity with the key's server-resolved user/team identity.

The direct adapter estimates cost from LiteLLM's live public model cost map.
It prices input, output, cache-read, and 5-minute/1-hour cache-write tokens when the model entry provides those rates.
This is an API-equivalent estimate, not the amount charged by a Claude Max subscription.
Missing pricing produces zero cost with `cost_basis: unavailable`, not a guessed rate.

Direct transcript events have no proxy timing data.
Their request latency and TTFT are therefore unavailable rather than inferred.
They also cannot reconstruct full agent/skill/command attribution without collecting conversation content.

Automatic historical backfill is safe only for transcript rows with `entrypoint == "claude-desktop"`.
Old `entrypoint == "cli"` transcripts do not record whether the call was proxied normal Claude or direct Remote Control, so replaying all of them would double-count some calls.
For current Remote Control runs, the launcher sets `CLAUDE_TRANSCRIPT_TRACKING_MODE=direct`.
Normal proxied Claude CLI transcripts are intentionally ignored.

## Codex CLI adapter notes

`agent_docs/harness-index.md` lists every skill/agent for Codex discovery - read it when no explicit name was given.
Codex has no `Task` tool: read the target agent file and follow it inline, or isolate noisy work via `codex exec`.
Route a noisy agent to a cheaper model via a LiteLLM alias/virtual key, never frontmatter `model:`.

## Agent/skill/command attribution

Full `agent_name`/`skill_name`/`command_name` attribution exists only for LiteLLM-proxied Claude Code payloads (see `_agent_invocations_from_messages`/`_active_skill_name_and_version`/`_active_command_name` in `services/_common/src/ingest_parsing.py`).
Codex CLI traffic has no equivalent and always lands with all three blank - not a gap to fix.
Direct Claude transcript events also leave these fields blank because `UsageEnvelopeV1` deliberately carries no prompt/response messages.
They can still carry the transcript's opaque `agent_id` as `agent_invocation_id`.

`agent_name` is joined from `agent_invocations` via a per-request `x-claude-code-agent-id` header, which has a known race: a spawned subagent's first call can outrun the orchestrator's own ingest.
`make reparse-all` re-runs ingestion against `ingest_raw.raw_payload_full` afterward and fixes it up.

`skill_name`/`command_name` both propagate backward through a turn's tool-result continuation chain, so every downstream row (not just the one that triggered the skill/command) carries the attribution.
No dashboard panel in `agents_overview.json` surfaces "untagged"/unattributed work as its own visible category yet - panel-48 does something similar, but only for its own narrow purpose.
