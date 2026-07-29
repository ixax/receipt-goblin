# Architecture rationale

Cross-cutting deep-dive reference for `AGENTS.md` - content that spans more than one service.
Everything that belonged to a single service (queue/worker split, gateway, per-file breakdowns, `fastjson` status) moved to `agent_docs/services/<name>.md` - see `AGENTS.md`'s "Project Structure & Directory Rules" for the full list for one entry point per service.

## Dev/prod split

- `docker-compose.yml` is prod-default: no service in it has `command:`/`entrypoint:`, or a bind mount for source/config (only named data volumes and genuine host-system mounts like `docker.sock`, `/proc`/`/sys`).
  `services/litellm/user_configs/*.yaml` (gitignored, may carry real remote-model hosts/keys) is baked into the `litellm` image at build time in prod too - not bind-mounted, so a new/changed source needs `docker compose build litellm` in prod.
- Everything needing a live-editing workflow (bind-mounted `services/*/src`, `config.yml`, Grafana's dashboards/provisioning, `litellm`'s `user_configs/` only, `--reload` for `webhook`) instead lives in `docker-compose.dev.yml`, an override file layered on top.
  `mcp-dev` is the one exception to "override": it has no definition at all in `docker-compose.yml` - `docker-compose.dev.yml` is its full service definition (build/image/environment/healthcheck/depends_on/networks), not just a bind-mount/`--reload` overlay on top of a base one.
  `mcp-stats` is NOT part of this exception - it's a normal prod service, fully defined in `docker-compose.yml`, with no `docker-compose.dev.yml` override at all today.
- `Makefile`'s `ENVIRONMENT` (`.env`, default `development`) picks which files `check-env` puts in `COMPOSE_FILES`: anything but exactly `production` layers both files; `production` uses `docker-compose.yml` alone.
  Every target depends on `check-env`, which prints `⚠️ ENVIRONMENT=...` before anything else runs.
  `ENVIRONMENT` is captured from the shell/`.env` *before* `include .env` runs and restored after, specifically so a shell-exported `ENVIRONMENT=production make start` isn't silently overwritten by `.env`'s own `ENVIRONMENT=development` default (`include`'s plain `=` assignment is file-origin, which GNU Make lets override an environment-origin variable - the reverse of what you'd expect).
- Static per-image config (Grafana provisioning, LiteLLM's `config.yaml`, `redis.conf`, etc.) is still baked into each service's own image via `COPY` in its Dockerfile, in both dev and prod, unchanged.
  What moved is *runtime role selection* for the one image serving five different compose services/roles (`webhook`/`webhook-worker`/`metrics-reparse`/`clickhouse-migrate`/`loadtest`, all built from `services/webhook/Dockerfile`) - see `agent_docs/services/webhook.md`'s "`APP_ROLE` dispatch" for the mechanism.
  `litellm` and `redis` get **no** dev override at all - both environments always run the same baked image.

## Codex CLI adapter notes

`agent_docs/harness-index.md` lists every skill/agent for Codex discovery.
Read it when no explicit name was given.
Codex has no `Task` tool.
Read the target agent file and follow it inline, or isolate noisy work via `codex exec`.
Route a noisy agent to a cheaper model via a LiteLLM alias/virtual key, never frontmatter `model:`.
