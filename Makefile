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
.PHONY: check-env start stop restart env test langfuse-up langfuse-down langfuse-logs reparse reparse-all \
	backup-clickhouse backup-litellm backup-grafana backup-all \
	restore-clickhouse restore-litellm restore-grafana \
	observability-up observability-down observability-logs observability-status

# The six langfuse-* services (see docker-compose.yml) all carry
# `profiles: [langfuse]`, so `docker compose down` doesn't accept a bare
# --profile filter for a scoped teardown (it tears down core services too -
# see the langfuse-down comment below) - list them explicitly instead.
LANGFUSE_SERVICES := langfuse-web langfuse-worker langfuse-db langfuse-clickhouse langfuse-minio langfuse-redis

# The observability-stack services (see docker-compose.yml) all carry
# `profiles: [observability]` - same reasoning as LANGFUSE_SERVICES above,
# list them explicitly so a scoped up/down/logs/status never touches core.
OBSERVABILITY_SERVICES := prometheus blackbox redis-exporter loki alloy cadvisor node-exporter

# Every other target depends on this so the active environment is always
# printed loudly before anything else runs - ENVIRONMENT=production is a
# one-word typo away from silently landing on dev's compose files (or
# vice versa), so this can't be easy to miss.
check-env:
	@echo "⚠️  ENVIRONMENT=$(ENVIRONMENT)"

start up: check-env
	docker compose $(COMPOSE_FILES) up -d --build --force-recreate

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
# --reload, like webhook-worker. Run `make start` instead if
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
# webhook against LiteLLM's own /key/info). Not stored in .env, so the
# printed `<virtual key>` is a placeholder - copy the output, replace it
# with your personal key, and paste the result into ~/.zshrc / ~/.bashrc
# (see README "Routing Claude Code through it").
env: check-env
	@echo 'export LITELLM_VIRTUAL_KEY="<virtual key>"'
	@echo 'export LITELLM_AUTH_HEADER="Bearer $(URI)"'
	@echo 'export ANTHROPIC_BASE_URL="$(URI)"'
	@echo 'export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: $$LITELLM_AUTH_HEADER"'
	@echo 'export OPENAI_API_BASE="$(URI)"'
	@echo 'export AGENT_CLI_TRACKING_API_URL="$(INGEST_URI)"'

# Reparses event_sources into agent_events/agent_usage/agent_messages/
# agent_invocations using the current classification logic - see
# services/webhook/src/reparse.py. ReplacingMergeTree-safe to re-run any
# number of times. Requires SESSION=<session_id>; use `make reparse-all` to
# reparse everything instead.
reparse: check-env
	@if [ -z "$(SESSION)" ]; then echo "usage: make reparse SESSION=<session_id>"; exit 1; fi
	docker compose $(COMPOSE_FILES) run --rm -e SESSION_ID=$(SESSION) webhook-reparse

reparse-all: check-env
	docker compose $(COMPOSE_FILES) run --rm webhook-reparse

# Backup/restore for clickhouse, litellm-db, and grafana-data - see
# services/backup/README.md for the full playbook, including why restore
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

# DESTRUCTIVE - see services/backup/README.md before running any of these.
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
