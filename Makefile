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
# webhook/mcp-dev's --reload) back on top of docker-compose.yml, which is
# the prod-default file (no command:/entrypoint:/source volumes at all).
ENVIRONMENT ?= development
ifeq ($(ENVIRONMENT),production)
COMPOSE_FILES := -f docker-compose.yml
else
COMPOSE_FILES := -f docker-compose.yml -f docker-compose.dev.yml
endif

OBSERVABILITY_COMPOSE_FILES := $(COMPOSE_FILES) -f docker-compose.observability.yml
LANGFUSE_COMPOSE_FILES := $(COMPOSE_FILES) -f docker-compose.langfuse.yml

PORT := $(if $(strip $(LITELLM_PORT)),$(LITELLM_PORT),4000)
# Full proxy URI, in case LiteLLM isn't on localhost (a shared/remote host) -
# LITELLM_PORT alone can't express that, so this takes precedence when set.
URI := $(if $(strip $(LITELLM_URI)),$(LITELLM_URI),http://localhost:$(PORT))

# litellm-with-fallback ports (load-balancer's `listen 4001`/`listen 4002` -
# see agent_docs/services/load-balancer.md) - same PORT/URI override pattern
# as above. setup-client below points ANTHROPIC_BASE_URL/OPENAI_API_BASE at
# these instead of plain $(URI), so a dead litellm fails over to
# api.anthropic.com/api.openai.com instead of failing the client outright.
ANTHROPIC_PROXY_PORT := $(if $(strip $(ANTHROPIC_PROXY_PORT)),$(ANTHROPIC_PROXY_PORT),4001)
ANTHROPIC_PROXY_URI := $(if $(strip $(ANTHROPIC_PROXY_URI)),$(ANTHROPIC_PROXY_URI),http://localhost:$(ANTHROPIC_PROXY_PORT))
OPENAI_PROXY_PORT := $(if $(strip $(OPENAI_PROXY_PORT)),$(OPENAI_PROXY_PORT),4002)
OPENAI_PROXY_URI := $(if $(strip $(OPENAI_PROXY_URI)),$(OPENAI_PROXY_URI),http://localhost:$(OPENAI_PROXY_PORT))

WEBHOOK_PORT := $(if $(strip $(WEBHOOK_PORT)),$(WEBHOOK_PORT),8010)
# Same override pattern as URI above, for hosts where webhook isn't on localhost.
INGEST_URI := $(if $(strip $(AGENT_CLI_TRACKING_API_URL)),$(AGENT_CLI_TRACKING_API_URL),http://localhost:$(WEBHOOK_PORT))

# Optional - if you've already put your personal virtual key in .env (see
# .env.example), `make setup-client` substitutes it in below instead of printing a
# `<virtual key>` placeholder to hand-edit.
VKEY := $(if $(strip $(LITELLM_VIRTUAL_KEY)),$(LITELLM_VIRTUAL_KEY),<virtual key>)

# One <SERVICE>_TAG env var per VERSIONS.yml key (see that file's own
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

.PHONY: check-env init start up restart up-no-deps build status migrate stop down logs setup-client test test-services test-hooks test-harness-audit harness-index langfuse-up langfuse-down langfuse-logs reparse reparse-all print-reparse-final-hint \
	backup-clickhouse backup-litellm backup-grafana backup-all \
	restore-clickhouse restore-litellm restore-grafana \
	observability-up observability-down observability-logs observability-status loadtest \
	loadtest-fixtures loadtest-fixtures-status

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

init: check-env
	python3 services/init/init_clickhouse_users.py $(COMPOSE_FILES)

# `start`: Brings up containers with existing images (no rebuild/recreate).
# `up`: Rebuilds and recreates containers - the fix for baked-in config/env/file changes.
# `logs`: Streams logs; scope to a single service with SERVICE=<name>.
# start/up/restart/down support SERVICE=<name> to scope to a single service
# (default: whole stack).
start: check-env
	docker compose $(COMPOSE_FILES) up -d $(SERVICE)

up: check-env
	docker compose $(COMPOSE_FILES) up -d --build --force-recreate $(SERVICE)

# Restarts running containers in place (not a rebuild) - picks up edits to
# bind-mounted source (services/webhook/src, etc.) for services without
# --reload, like worker. Run `make up` instead if
# requirements.txt/Dockerfile changed. SERVICE is optional (default: whole
# stack), same as start/up/logs.
restart: check-env
	docker compose $(COMPOSE_FILES) restart $(SERVICE)

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
	python3 scripts/wait_for_stack_healthy.py $(COMPOSE_FILES)

# The only way to apply ClickHouse migrations (services/clickhouse/migrations/*.sql
# + one-time dashboard Dictionaries) - no longer runs automatically as part of
# `make up`/`make start`. Operators must run `make migrate` explicitly, e.g.
# right after `make init` on a fresh clone, or after adding a new migration file,
# or before first `make start` on a fresh `make init`. Never touches ClickHouse
# users/roles/grants - that's `make init` alone, see services/init/.
migrate: check-env
	docker compose $(COMPOSE_FILES) run --rm clickhouse-migrate

# SERVICE is optional (default: whole stack). langfuse-down/observability-down
# always run regardless of SERVICE - they only tear down their own opt-in
# profile containers, not the core stack.
stop down: check-env langfuse-down observability-down
	docker compose $(COMPOSE_FILES) down $(SERVICE)

logs: check-env
	docker compose $(COMPOSE_FILES) logs -f $(SERVICE)

# Opt-in Langfuse stack (see README "Langfuse"). Langfuse never starts
# automatically - must be run explicitly. Run this directly if you want to
# bring up Langfuse without touching the core stack. `make stop`/`make down`
# will tear it down automatically as a courtesy.
langfuse-up: check-env
	docker compose $(LANGFUSE_COMPOSE_FILES) --profile langfuse up -d --build --force-recreate $(LANGFUSE_SERVICES)

# `docker compose --profile langfuse down` (no service args) tears down the
# core stack too, since --profile langfuse activates langfuse *in addition
# to* default (no-profile) services - passing $(LANGFUSE_SERVICES) explicitly
# scopes it to just the six Langfuse containers.
langfuse-down: check-env
	docker compose $(LANGFUSE_COMPOSE_FILES) --profile langfuse down $(LANGFUSE_SERVICES)

langfuse-logs: check-env
	docker compose $(LANGFUSE_COMPOSE_FILES) --profile langfuse logs -f $(LANGFUSE_SERVICES)

# Opt-in observability stack (Prometheus/Blackbox/redis-exporter/Loki/Alloy -
# see README "Observability"). Observability never starts automatically - must be
# run explicitly. Run this directly to start the observability stack. `make stop`/
# `make down` will tear it down automatically as a courtesy.
observability-up: check-env
	docker compose $(OBSERVABILITY_COMPOSE_FILES) --profile observability up -d --build --force-recreate $(OBSERVABILITY_SERVICES)

# `docker compose --profile observability down` (no service args) tears down
# the core stack too, since --profile observability activates observability
# *in addition to* default (no-profile) services - passing
# $(OBSERVABILITY_SERVICES) explicitly scopes it to just those containers.
observability-down: check-env
	docker compose $(OBSERVABILITY_COMPOSE_FILES) --profile observability down $(OBSERVABILITY_SERVICES)

observability-logs: check-env
	docker compose $(OBSERVABILITY_COMPOSE_FILES) --profile observability logs -f $(OBSERVABILITY_SERVICES)

observability-status: check-env
	docker compose $(OBSERVABILITY_COMPOSE_FILES) --profile observability ps $(OBSERVABILITY_SERVICES)

# Runs each service's test directory as its own pytest invocation, plus
# services/_common/tests (the shared ingest_parsing/ingest_db/fastjson
# suite, split out of services/webhook by the webhook-worker-split
# refactor - see plans/webhook-worker-split.md).
# Deliberately NOT one combined `pytest a b c d e` invocation: every
# service's src/ is a bare top-level `src` package (e.g. `from src import
# worker`), so a single pytest process collecting multiple services would
# cache the first one it imports in sys.modules and silently resolve every
# later service's `from .config import ...` against that first service's
# config instead of its own - confirmed via a real ImportError when this
# was tried. Separate invocations sidestep that entirely.
# No live ClickHouse needed - see each dir's conftest.py.
# Needs requirements-dev.txt installed in .venv first:
# `pip install -r requirements-dev.txt`. The root
# `pytest.ini` (shared by every service's invocation below, not owned by
# any one service) silences dependency warnings (urllib3/clickhouse-connect
# deprecation noise unrelated to this repo's own code).
# services/mcp-dev/tests is NOT included below: its `src/server.py` imports
# the `mcp` SDK (`mcp.server.fastmcp`), which requires Python >=3.10 and
# can't be installed into this repo's Python 3.9 `.venv` - run it
# separately with a Python 3.10+ interpreter/venv that has
# services/mcp-dev/requirements.txt installed
# (`python3.11 -m pytest -c pytest.ini services/mcp-dev/tests`) until the
# shared `.venv` is upgraded.
test-services:
	@for svc in webhook worker reparse loadtest _common; do \
	  out=$$(.venv/bin/python -m pytest -c pytest.ini services/$$svc/tests 2>&1); code=$$?; \
	  if [ $$code -ne 0 ]; then echo "$$out"; exit $$code; fi; \
	  summary=$$(echo "$$out" | grep -E 'passed|failed' | tail -n 1); \
	  if [ -n "$$summary" ]; then echo "$$svc: $$summary"; fi; \
	done

# Runs hooks/harness_audit/tests (pure Python unittest, no dependencies).
test-hooks:
	@out=$$(python3 -m unittest discover -s hooks/harness_audit/tests 2>&1); code=$$?; \
	if [ $$code -ne 0 ]; then echo "$$out"; exit $$code; fi; \
	echo "hooks: $$(echo "$$out" | grep -v '^$$' | tail -n 2 | tr '\n' ' ')"

# Umbrella target that runs both service and hook tests.
test: test-services test-hooks

# Backward-compat alias for test-hooks (deprecated, use test-hooks instead).
test-harness-audit: test-hooks

# Generates/refreshes agent_docs/harness-index.md (a table of every
# skill/agent's name+description+path, derived from frontmatter, for
# Codex CLI discovery).
harness-index:
	python3 scripts/sync_harness.py

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
setup-client:
	@echo '# --- ~/.zshrc / ~/.bashrc (paste as-is, or use the config blocks below instead) ---'
	@echo 'export LITELLM_VIRTUAL_KEY="$(VKEY)"'
	@echo 'export LITELLM_AUTH_HEADER="Bearer $(VKEY)"'
	@echo '# Anthropic/OpenAI-wire fallback ports, not plain litellm - see'
	@echo '# agent_docs/services/load-balancer.md. Auth headers below are NOT'
	@echo '# translated once fallen back to the real provider.'
	@echo 'export ANTHROPIC_BASE_URL="$(ANTHROPIC_PROXY_URI)"'
	@echo 'export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: $$LITELLM_AUTH_HEADER"'
	@echo 'export OPENAI_API_BASE="$(OPENAI_PROXY_URI)"'
	@echo 'export AGENT_CLI_TRACKING_API_URL="$(INGEST_URI)"'
	@echo ''
	@echo '# --- ~/.codex/config.toml (merge in, keep any hooks/mcp_servers already there) ---'
	@echo '# Only covers model routing - the export lines above are still needed'
	@echo '# in your shell for hooks/report_git_branch.py, see comment above.'
	@echo 'model_provider = "litellm"'
	@echo ''
	@echo '[model_providers.litellm]'
	@echo 'name = "LiteLLM"'
	@echo 'base_url = "$(OPENAI_PROXY_URI)"'
	@echo 'wire_api = "responses"'
	@echo 'requires_openai_auth = true'
	@echo 'env_http_headers = { "x-litellm-api-key" = "LITELLM_AUTH_HEADER" }'
	@echo ''
	@echo '# --- ~/.claude/settings.json ("env" block - merge in, keep any hooks already there) ---'
	@echo '{'
	@echo '  "env": {'
	@echo '    "ANTHROPIC_BASE_URL": "$(ANTHROPIC_PROXY_URI)",'
	@echo '    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: Bearer $(VKEY)",'
	@echo '    "AGENT_CLI_TRACKING_API_URL": "$(INGEST_URI)",'
	@echo '    "LITELLM_VIRTUAL_KEY": "$(VKEY)"'
	@echo '  }'
	@echo '}'

# Reparses ingest_raw into agent_events/agent_usage/agent_messages/
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
	docker compose $(COMPOSE_FILES) run --rm -e SESSION_ID=$(SESSION) metrics-reparse
	@$(MAKE) print-reparse-final-hint

reparse-all: check-env
	docker compose $(COMPOSE_FILES) run --rm metrics-reparse
	@$(MAKE) print-reparse-final-hint

print-reparse-final-hint:
	@echo ''
	@echo 'Reparse re-inserted rows into ReplacingMergeTree tables - until the'
	@echo 'next background merge, dashboards reading them without FINAL can show'
	@echo 'transient duplicate rows. To collapse them now, run:'
	@echo ''
	@echo '  docker exec receipt-goblin-clickhouse clickhouse-client -q "OPTIMIZE TABLE agent_events FINAL; OPTIMIZE TABLE agent_usage FINAL; OPTIMIZE TABLE agent_messages FINAL; OPTIMIZE TABLE agent_invocations FINAL; OPTIMIZE TABLE ai_gateway_users FINAL; OPTIMIZE TABLE ai_gateway_groups FINAL"'
	@echo ''
	@echo '(ingest_raw is deliberately excluded - it is large and OPTIMIZE FINAL on it risks OOM.)'

# Replays real traffic from the `loadtest_fixtures` volume (see the
# `loadtest-fixtures` target below) against webhook's own
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
	  --name receipt-goblin-loadtest \
	  -e TARGET_URL=$(or $(TARGET_URL),http://load-balancer:8000/api/v1/metrics) \
	  -e START_USERS=$(or $(START_USERS),10) \
	  -e END_USERS=$(or $(END_USERS),100) \
	  -e RAMP_STEPS=$(or $(RAMP_STEPS),10) \
	  -e RAMP_STEP_MINUTES=$(or $(RAMP_STEP_MINUTES),1) \
	  -e HOLD_MINUTES=$(or $(HOLD_MINUTES),5) \
	  -e DURATION_MINUTES=$(or $(DURATION_MINUTES),0) \
	  -e SPEED=$(or $(SPEED),1.0) \
	loadtest

# Generates test fixtures (small/medium/large) by extracting data from ClickHouse into a
# named volume mounted at `/app/loadtest_fixtures`, ready for load test consumption. No default -
# if VOLUME is unset, checks $LOADTEST_FIXTURES_VOLUME (validates, errors if invalid), then
# prompts interactively. Override with `VOLUME=small`, `VOLUME=medium`, `VOLUME=large`, etc.
# Fixtures are written to the `loadtest-fixtures-data` volume and must be regenerated if you want
# to switch volumes or update captured data. See `loadtest-runner`'s own task instructions for
# regeneration policy.
loadtest-fixtures: check-env
	docker compose $(COMPOSE_FILES) run --rm $(if $(VOLUME),-e LOADTEST_FIXTURES_VOLUME=$(VOLUME),) loadtest-fixtures

# Prints the fixture manifest (which fixtures are available in the `loadtest-fixtures-data`
# volume, their sizes, timestamps, etc.) without reading ClickHouse at all - useful for
# confirming what's already built without spawning a container that would otherwise consume
# resources.
loadtest-fixtures-status: check-env
	docker compose $(COMPOSE_FILES) run --rm loadtest-fixtures python -m src.build_fixtures --status

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
