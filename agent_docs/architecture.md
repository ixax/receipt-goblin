# Architecture rationale

Cross-cutting deep-dive reference for `AGENTS.md`.
Covers content spanning more than one service.
Single-service content (queue/worker split, gateway, per-file breakdowns, `fastjson` status) moved to `agent_docs/services/<name>.md` - see `AGENTS.md`'s "Project Structure & Directory Rules" for the per-service entry-point list.

## Dev/prod split

- `docker-compose.yml` is prod-default: no service has `command:`/`entrypoint:` or a bind mount for source/config (only named data volumes and genuine host-system mounts like `docker.sock`, `/proc`/`/sys`).
  `services/litellm/user_configs/*.yaml` (gitignored, may carry real remote-model hosts/keys) is baked into the `litellm` image at build time in prod too, not bind-mounted, so a changed source needs `docker compose build litellm` in prod.
- Everything needing a live-editing workflow (bind-mounted `services/*/src`, `config.yml`, Grafana's dashboards/provisioning, `litellm`'s `user_configs/` only, `--reload` for `webhook`) lives in `docker-compose.dev.yml`, an override file layered on top.
  `mcp-dev` is the one exception: it has no definition at all in `docker-compose.yml` - `docker-compose.dev.yml` is its full service definition (build/image/environment/healthcheck/depends_on/networks), not just a bind-mount/`--reload` overlay.
  `mcp-stats` is not part of this exception - it's a normal prod service, fully defined in `docker-compose.yml`, with no dev override today.
- `Makefile`'s `ENVIRONMENT` (`.env`, default `development`) picks which files `check-env` puts in `COMPOSE_FILES`: anything but exactly `production` layers both files; `production` uses `docker-compose.yml` alone.
  Every target depends on `check-env`, which prints `⚠️ ENVIRONMENT=...` first.
  `ENVIRONMENT` is captured from the shell/`.env` before `include .env` runs, and restored after.
  This stops a shell-exported `ENVIRONMENT=production make start` from being silently overwritten by `.env`'s own `development` default: `include`'s plain `=` assignment is file-origin, which GNU Make lets override an environment-origin variable - the reverse of what you'd expect.
- Static per-image config (Grafana provisioning, LiteLLM's `config.yaml`, `redis.conf`, etc.) is still baked into each service's own image via `COPY` in its Dockerfile, in both dev and prod, unchanged.
  `webhook`/`webhook-worker`/`metrics-reparse`/`clickhouse-migrate`/`loadtest` used to be one image sharing runtime role selection via `APP_ROLE` (`services/webhook/Dockerfile` + `docker-entrypoint.sh`) - the webhook-worker-split refactor (see `plans/webhook-worker-split.md`) gave each its own independent Dockerfile/image/tag instead (`services/webhook/`, `services/worker/`, `services/reparse/`, `services/migrate/`, `services/loadtest/`), all pulling shared code from `services/_common/src/`.
  `litellm` and `redis` get no dev override at all: both environments always run the same baked image.

## Image tags

`VERSIONS.yml` holds each service's `SERVICE_TAG: X.Y.Z-{build}`.
`scripts/resolve_image_version.py` resolves these into `.image-tags.mk`.
`{build}` is the commit hash, except `observability`/`langfuse`, which use a static SEMVER.
Bump a tag when its Dockerfile or image code changes.

## Codex CLI adapter notes

`agent_docs/harness-index.md` lists every skill/agent for Codex discovery; read it when no explicit name was given.
Codex has no `Task` tool: read the target agent file and follow it inline, or isolate noisy work via `codex exec`.
Route a noisy agent to a cheaper model via a LiteLLM alias/virtual key, never frontmatter `model:`.

## Agent/skill/command attribution

`agent_name`/`skill_name`/`command_name` on `agent_events`/`agent_usage` rows are Claude Code-only concepts (see `_agent_invocations_from_messages`/`_active_skill_name_and_version`/`_active_command_name_and_version` in `services/_common/src/ingest_parsing.py`).
Codex CLI traffic has no equivalent and always lands with all three blank - not a gap to fix.

`agent_name` is joined from `agent_invocations` via a per-request `x-claude-code-agent-id` header, which has a known race: a spawned subagent's first call can outrun the orchestrator's own ingest.
`make reparse-all` re-runs ingestion against `ingest_raw.raw_payload_full` afterward and fixes it up.

`skill_name`/`command_name` both propagate backward through a turn's tool-result continuation chain, so every downstream row (not just the one that triggered the skill/command) carries the attribution.
There is currently no dashboard panel in `agents_overview.json` surfacing "untagged"/unattributed work as its own visible category - panel-48 does something similar, but only for its own narrow purpose.
