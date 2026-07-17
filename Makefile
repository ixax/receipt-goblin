ifneq (,$(wildcard ./.env))
    include .env
endif

ENV_VARS := $(shell [ -f .env ] && sed 's/=.*//' .env)
unexport $(ENV_VARS)

PORT := $(if $(strip $(LITELLM_PORT)),$(LITELLM_PORT),4000)
# Full proxy URI, in case LiteLLM isn't on localhost (a shared/remote host) -
# LITELLM_PORT alone can't express that, so this takes precedence when set.
URI := $(if $(strip $(LITELLM_URI)),$(LITELLM_URI),http://localhost:$(PORT))

WEBHOOK_PORT := $(if $(strip $(WEBHOOK_PORT)),$(WEBHOOK_PORT),8010)
# Same override pattern as URI above, for hosts where webhook isn't on localhost.
INGEST_URI := $(if $(strip $(AGENT_CLI_TRACKING_API_URL)),$(AGENT_CLI_TRACKING_API_URL),http://localhost:$(WEBHOOK_PORT))
.PHONY: start stop restart env test

start up:
	docker compose up -d --build

status:
	docker compose ps

stop down:
	docker compose down

# Restarts running containers in place (not a rebuild) - picks up edits to
# bind-mounted source (services/webhook/src, etc.) for services without
# --reload, like webhook-worker. Run `make start` instead if
# requirements.txt/Dockerfile changed.
restart:
	docker compose restart

# Runs services/webhook/tests (pure clickhouse_ingest.py functions, no live
# ClickHouse needed - see services/webhook/tests/conftest.py). Needs
# services/webhook/requirements-dev.txt installed in .venv first: `pip install -r
# services/webhook/requirements-dev.txt`. services/webhook/pytest.ini forces per-test verbose
# output (-v) and silences dependency warnings (urllib3/clickhouse-connect
# deprecation noise unrelated to this repo's own code).
test:
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
env:
	@echo 'export LITELLM_VIRTUAL_KEY="<virtual key>"'
	@echo 'export ANTHROPIC_BASE_URL="$(URI)"'
	@echo 'export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $$LITELLM_VIRTUAL_KEY"'
	@echo 'export OPENAI_API_BASE="$(URI)"'
	@echo 'export OPENAI_API_KEY="$$LITELLM_VIRTUAL_KEY"'
	@echo 'export AGENT_CLI_TRACKING_API_URL="$(INGEST_URI)"'
