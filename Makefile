# Captured before `include .env` so a shell-exported `ENVIRONMENT=production
# make start` still wins over .env's own ENVIRONMENT=development default -
# `include`'s plain `=` assignment is file-origin, which normally overrides
# an environment-origin variable (the opposite of the override you'd expect
# from `VAR=x make target`), so it's restored below once .env has loaded.
ENVIRONMENT_FROM_SHELL := $(ENVIRONMENT)

ifneq (,$(wildcard ./.env))
    include .env
endif

ifneq ($(ENVIRONMENT_FROM_SHELL),)
ENVIRONMENT := $(ENVIRONMENT_FROM_SHELL)
endif

ENV_VARS := $(shell [ -f .env ] && sed 's/=.*//' .env)
unexport $(ENV_VARS)

# Anything other than exactly "production" is dev - default/empty included.
# docker-compose.dev.yml layers dev's live source/config bind mounts (and
# webhook/mcp-server's --reload) back on top of docker-compose.yml, which is
# the prod-default file (no command:/entrypoint:/source volumes at all).
ENVIRONMENT ?= development
ifeq ($(ENVIRONMENT),production)
COMPOSE_FILES := -f docker-compose.yml
else
COMPOSE_FILES := -f docker-compose.yml -f docker-compose.dev.yml
endif

PORT := $(if $(strip $(LITELLM_PORT)),$(LITELLM_PORT),4000)
# Full proxy URI, in case LiteLLM isn't on localhost (a shared/remote host) -
# LITELLM_PORT alone can't express that, so this takes precedence when set.
URI := $(if $(strip $(LITELLM_URI)),$(LITELLM_URI),http://localhost:$(PORT))

WEBHOOK_PORT := $(if $(strip $(WEBHOOK_PORT)),$(WEBHOOK_PORT),8010)
# Same override pattern as URI above, for hosts where webhook isn't on localhost.
INGEST_URI := $(if $(strip $(AGENT_CLI_TRACKING_API_URL)),$(AGENT_CLI_TRACKING_API_URL),http://localhost:$(WEBHOOK_PORT))

# Optional - if you've already put your personal virtual key in .env (see
# .env.example), `make env` substitutes it in below instead of printing a
# `<virtual key>` placeholder to hand-edit.
VKEY := $(if $(strip $(LITELLM_VIRTUAL_KEY)),$(LITELLM_VIRTUAL_KEY),<virtual key>)

# One <SERVICE>_TAG env var per VERSION.yml key (see that file's own
# comment for the templating rules), exported so docker-compose.yml's
# per-service `image: ...:${..._TAG:-latest}` lines all interpolate -
# scripts/resolve_image_version.py is the single source of truth for the
# resolution logic, shared with `make build` below rather than
# reimplemented in Make.
#
# Regenerated via a `$(shell ... > file)` side effect (not captured into a
# Make variable) specifically so the real newlines between each
# `export FOO_TAG=...` line survive onto disk - `$(shell ...)`'s return
# value collapses every newline into a space, so a direct
# `$(eval $(shell resolve_image_version.py))` mangles all but the first
# line into that one variable's value instead of exporting each
# separately (confirmed: only the first var ever came through, the rest
# silently got swallowed as extra tokens in its value). `include`ing a
# real file has no such collapsing - each line is its own statement.
$(shell python3 scripts/resolve_image_version.py > .image-tags.mk)
include .image-tags.mk

.PHONY: check-env start stop restart up-no-deps env test build langfuse-up langfuse-down langfuse-logs reparse reparse-all print-reparse-final-hint \
	backup-clickhouse backup-litellm backup-grafana backup-all \
	restore-clickhouse restore-litellm restore-grafana \
	observability-up observability-down observability-logs observability-status loadtest

# The six langfuse-* services (see docker-compose.yml) all carry
# `profiles: [langfuse]`, so `docker compose down` doesn't accept a bare
# --profile filter for a scoped teardown (it tears down core services too -
# see the langfuse-down comment below) - list them explicitly instead.
LANGFUSE_SERVICES := langfuse-web langfuse-worker langfuse-db langfuse-clickhouse langfuse-minio langfuse-redis

# The observability-stack services (see docker-compose.yml) all carry
# `profiles: [observability]` - same reasoning as LANGFUSE_SERVICES above,
# list them explicitly so a scoped up/down/logs/status never touches core.
OBSERVABILITY_SERVICES := prometheus blackbox redis-exporter loki alloy cadvisor node-exporter nginx-exporter

# Every other target depends on this so the active environment is always
# printed loudly before anything else runs - ENVIRONMENT=production is a
# one-word typo away from silently landing on dev's compose files (or
# vice versa), so this can't be easy to miss.
check-env:
	@echo "⚠️  ENVIRONMENT=$(ENVIRONMENT)"
	@python3 scripts/resolve_image_version.py | sed 's/^export /⚠️  /'

# SERVICE is optional - `make up` (re)creates the whole stack, `make up
# SERVICE=webhook` scopes --build/--force-recreate to just that one
# service, same convention as `make build SERVICE=...` below.
start up: check-env
	docker compose $(COMPOSE_FILES) up -d --build --force-recreate $(SERVICE)

# SERVICE is required here (unlike `make up`) - recreates just that one
# service with --no-deps, so a config/image change (mem_limit, cpus, an env
# var) applies without cascading into recreating its depends_on chain too
# (clickhouse/redis, if the service in question depends on them). Only
# safe when those dependencies are already up and healthy - --no-deps skips
# checking that, it just assumes it. Compare `make restart` above, which
# restarts (not recreates) every running container in place - fine for a
# bind-mounted source edit, useless for a compose-level config change like
# this one, since restart doesn't re-read docker-compose.yml.
up-no-deps: check-env
	docker compose $(COMPOSE_FILES) up -d --build --no-deps $(SERVICE)

# SERVICE is optional - `make build` builds every service, `make build
# SERVICE=redis` (or webhook-1, etc.) scopes it to just that one.
# resolve_image_version.py's export up top already put every image group's
# tag in this recipe's environment, so `docker compose build` just needs
# pointing at the target(s); it resolves each service's own
# `image: ...:${..._TAG:-latest}` the same way `make up` would.
build: check-env
	docker compose $(COMPOSE_FILES) build $(SERVICE)

status: check-env
	docker compose $(COMPOSE_FILES) ps

stop down: check-env langfuse-down observability-down
	docker compose $(COMPOSE_FILES) down

logs: check-env
	docker compose $(COMPOSE_FILES) logs -f

# Opt-in Langfuse stack (see README "Langfuse"). `make up`/`make down` call
# these automatically; run them directly if you only want to bounce Langfuse
# without touching the core stack.
langfuse-up: check-env
	docker compose $(COMPOSE_FILES) --profile langfuse up -d --build $(LANGFUSE_SERVICES)

# `docker compose --profile langfuse down` (no service args) tears down the
# core stack too, since --profile langfuse activates langfuse *in addition
# to* default (no-profile) services - passing $(LANGFUSE_SERVICES) explicitly
# scopes it to just the six Langfuse containers.
langfuse-down: check-env
	docker compose $(COMPOSE_FILES) --profile langfuse down $(LANGFUSE_SERVICES)

langfuse-logs: check-env
	docker compose $(COMPOSE_FILES) --profile langfuse logs -f $(LANGFUSE_SERVICES)

# Opt-in observability stack (Prometheus/Blackbox/redis-exporter/Loki/Alloy -
# see README "Observability"). `make up`/`make down` call observability-down
# automatically on teardown; run these directly to bounce just this stack
# without touching the core services.
observability-up: check-env
	docker compose $(COMPOSE_FILES) --profile observability up -d --build $(OBSERVABILITY_SERVICES)

# `docker compose --profile observability down` (no service args) tears down
# the core stack too, since --profile observability activates observability
# *in addition to* default (no-profile) services - passing
# $(OBSERVABILITY_SERVICES) explicitly scopes it to just those containers.
observability-down: check-env
	docker compose $(COMPOSE_FILES) --profile observability down $(OBSERVABILITY_SERVICES)

observability-logs: check-env
	docker compose $(COMPOSE_FILES) --profile observability logs -f $(OBSERVABILITY_SERVICES)

observability-status: check-env
	docker compose $(COMPOSE_FILES) --profile observability ps $(OBSERVABILITY_SERVICES)

# Restarts running containers in place (not a rebuild) - picks up edits to
# bind-mounted source (services/webhook/src, etc.) for services without
# --reload, like worker. Run `make start` instead if
# requirements.txt/Dockerfile changed.
restart: check-env
	docker compose $(COMPOSE_FILES) restart

# Runs services/webhook/tests (pure clickhouse_ingest.py functions, no live
# ClickHouse needed - see services/webhook/tests/conftest.py). Needs
# services/webhook/requirements-dev.txt installed in .venv first: `pip install -r
# services/webhook/requirements-dev.txt`. services/webhook/pytest.ini forces per-test verbose
# output (-v) and silences dependency warnings (urllib3/clickhouse-connect
# deprecation noise unrelated to this repo's own code).
test: check-env
	.venv/bin/python -m pytest -c services/webhook/pytest.ini services/webhook/tests

# Prints export statements to route Claude Code, Codex, and other OpenAI/
# Anthropic-SDK-based tools through the local LiteLLM proxy, plus
# AGENT_CLI_TRACKING_API_URL/LITELLM_VIRTUAL_KEY for hooks/report_git_branch.py
# (neither has a fallback - the hook crashes if they aren't exported;
# LITELLM_VIRTUAL_KEY also authenticates that hook's report, checked by
# webhook against LiteLLM's own /key/info), followed by a ready-to-paste
# config block for each CLI (see README "Configuring via config files instead
# of shell exports"). The shell-export lines above are still required for
# Codex regardless of the config.toml block below - Codex hooks just inherit
# whatever environment the shell that launched `codex` already has, there's
# no config.toml equivalent of Claude's "env" block for that. `<virtual key>`
# is a placeholder unless LITELLM_VIRTUAL_KEY is already set in .env, in
# which case it's substituted everywhere below.
env: check-env
	@echo '# --- ~/.zshrc / ~/.bashrc (paste as-is, or use the config blocks below instead) ---'
	@echo 'export LITELLM_VIRTUAL_KEY="$(VKEY)"'
	@echo 'export LITELLM_AUTH_HEADER="Bearer $(VKEY)"'
	@echo 'export ANTHROPIC_BASE_URL="$(URI)"'
	@echo 'export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: $$LITELLM_AUTH_HEADER"'
	@echo 'export OPENAI_API_BASE="$(URI)"'
	@echo 'export AGENT_CLI_TRACKING_API_URL="$(INGEST_URI)"'
	@echo ''
	@echo '# --- ~/.codex/config.toml (merge in, keep any hooks/mcp_servers already there) ---'
	@echo '# Only covers model routing - the export lines above are still needed'
	@echo '# in your shell for hooks/report_git_branch.py, see comment above.'
	@echo 'model_provider = "litellm"'
	@echo ''
	@echo '[model_providers.litellm]'
	@echo 'name = "LiteLLM"'
	@echo 'base_url = "$(URI)"'
	@echo 'wire_api = "responses"'
	@echo 'requires_openai_auth = true'
	@echo 'env_http_headers = { "x-litellm-api-key" = "LITELLM_AUTH_HEADER" }'
	@echo ''
	@echo '# --- ~/.claude/settings.json ("env" block - merge in, keep any hooks already there) ---'
	@echo '{'
	@echo '  "env": {'
	@echo '    "ANTHROPIC_BASE_URL": "$(URI)",'
	@echo '    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: Bearer $(VKEY)",'
	@echo '    "AGENT_CLI_TRACKING_API_URL": "$(INGEST_URI)",'
	@echo '    "LITELLM_VIRTUAL_KEY": "$(VKEY)"'
	@echo '  }'
	@echo '}'

# Reparses event_sources into agent_events/agent_usage/agent_messages/
# agent_invocations using the current classification logic - see
# services/webhook/src/reparse.py. ReplacingMergeTree-safe to re-run any
# number of times. Requires SESSION=<session_id>; use `make reparse-all` to
# reparse everything instead.
#
# Each reparse re-inserts a fresh row per event, so until ClickHouse merges
# the old+new parts, dashboards reading these tables without FINAL (e.g. the
# Trace panel) can show transient duplicate rows - print-reparse-final-hint
# reminds to collapse them explicitly instead of waiting on a background merge.
reparse: check-env
	@if [ -z "$(SESSION)" ]; then echo "usage: make reparse SESSION=<session_id>"; exit 1; fi
	docker compose $(COMPOSE_FILES) run --rm -e SESSION_ID=$(SESSION) webhook-reparse
	@$(MAKE) print-reparse-final-hint

reparse-all: check-env
	docker compose $(COMPOSE_FILES) run --rm webhook-reparse
	@$(MAKE) print-reparse-final-hint

print-reparse-final-hint:
	@echo ''
	@echo 'Reparse re-inserted rows into ReplacingMergeTree tables - until the'
	@echo 'next background merge, dashboards reading them without FINAL can show'
	@echo 'transient duplicate rows. To collapse them now, run:'
	@echo ''
	@echo '  docker exec receipt-goblin-clickhouse clickhouse-client -q "OPTIMIZE TABLE agent_events FINAL; OPTIMIZE TABLE agent_usage FINAL; OPTIMIZE TABLE agent_messages FINAL; OPTIMIZE TABLE agent_invocations FINAL; OPTIMIZE TABLE ai_gateway_users FINAL; OPTIMIZE TABLE ai_gateway_groups FINAL"'
	@echo ''
	@echo '(event_sources is deliberately excluded - it is large and OPTIMIZE FINAL on it risks OOM.)'

# Replays real captured traffic (.capture/) against webhook's own
# POST /api/v1/metrics at a ramping concurrency profile, to see how
# worker/redis/clickhouse cope - see services/webhook/src/loadtest.py
# for the full model. Bypasses LiteLLM/the real Claude API entirely.
# Defaults reproduce loadtest.py's own defaults (ramp 10->100 users over 10
# steps/1 min each, then hold 5 min) if no vars are set - override any of
# them, e.g.:
#   make loadtest END_USERS=250 DURATION_MINUTES=30 SPEED=5
#   make loadtest TARGET_URL=https://staging.example.com/api/v1/metrics
loadtest: check-env
	docker compose $(COMPOSE_FILES) run --rm \
	  --name receipt-goblin-webhook-loadtest \
	  -e TARGET_URL=$(or $(TARGET_URL),http://load-balancer:8000/api/v1/metrics) \
	  -e START_USERS=$(or $(START_USERS),10) \
	  -e END_USERS=$(or $(END_USERS),100) \
	  -e RAMP_STEPS=$(or $(RAMP_STEPS),10) \
	  -e RAMP_STEP_MINUTES=$(or $(RAMP_STEP_MINUTES),1) \
	  -e HOLD_MINUTES=$(or $(HOLD_MINUTES),5) \
	  -e DURATION_MINUTES=$(or $(DURATION_MINUTES),0) \
	  -e SPEED=$(or $(SPEED),1.0) \
	loadtest

# Backup/restore for clickhouse, litellm-db, and grafana-data - see
# README.md's "Backup & restore" section for the full playbook, including why restore
# needs the target container stopped first (not automated here - the
# backup container never touches the Docker socket, see docker-compose.yml's
# `backup` service comment). Files land under $BACKUP_DIR (default
# .backups/) on the host, kept until removed by hand (no auto-pruning).
backup-clickhouse: check-env
	docker compose $(COMPOSE_FILES) run --rm backup ./scripts/backup_clickhouse.sh

backup-litellm: check-env
	docker compose $(COMPOSE_FILES) run --rm backup ./scripts/backup_litellm.sh

backup-grafana: check-env
	docker compose $(COMPOSE_FILES) run --rm backup ./scripts/backup_grafana.sh

# Runs all three - this is the target cron should call.
backup-all: check-env
	docker compose $(COMPOSE_FILES) run --rm backup ./scripts/backup_all.sh

# DESTRUCTIVE - see README.md's "Backup & restore" section before running any of these.
# Requires FILE=<name under $BACKUP_DIR/<service>/> and stopping the
# relevant container first for litellm/grafana (clickhouse can stay up).
restore-clickhouse: check-env
	@if [ -z "$(FILE)" ]; then echo "usage: make restore-clickhouse FILE=<file under .backups/clickhouse/>"; exit 1; fi
	docker compose $(COMPOSE_FILES) run --rm backup ./scripts/restore_clickhouse.sh "$(FILE)" --yes

restore-litellm: check-env
	@if [ -z "$(FILE)" ]; then echo "usage: make restore-litellm FILE=<file under .backups/litellm/> (stop litellm first)"; exit 1; fi
	docker compose $(COMPOSE_FILES) run --rm backup ./scripts/restore_litellm.sh "$(FILE)" --yes

restore-grafana: check-env
	@if [ -z "$(FILE)" ]; then echo "usage: make restore-grafana FILE=<file under .backups/grafana/> (stop grafana first)"; exit 1; fi
	docker compose $(COMPOSE_FILES) run --rm backup ./scripts/restore_grafana.sh "$(FILE)" --yes
