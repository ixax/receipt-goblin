ifneq (,$(wildcard ./.env))
    include .env
endif

ENV_VARS := $(shell [ -f .env ] && sed 's/=.*//' .env)
unexport $(ENV_VARS)

PORT := $(if $(strip $(LITELLM_PORT)),$(LITELLM_PORT),4000)
# Full proxy URI, in case LiteLLM isn't on localhost (a shared/remote host) -
# LITELLM_PORT alone can't express that, so this takes precedence when set.
URI := $(if $(strip $(LITELLM_URI)),$(LITELLM_URI),http://localhost:$(PORT))
.PHONY: start stop env test

start:
	docker compose up -d --build

status:
	docker ps

stop:
	docker compose down

# Runs webhook/tests (pure clickhouse_ingest.py functions, no live
# ClickHouse needed - see webhook/tests/conftest.py). Needs
# webhook/requirements-dev.txt installed in .venv first: `pip install -r
# webhook/requirements-dev.txt`. webhook/pytest.ini forces per-test verbose
# output (-v) and silences dependency warnings (urllib3/clickhouse-connect
# deprecation noise unrelated to this repo's own code).
test:
	.venv/bin/python -m pytest -c webhook/pytest.ini webhook/tests

# Prints export statements to route Claude Code, Codex, and other OpenAI/
# Anthropic-SDK-based tools through the local LiteLLM proxy. Not stored in
# .env, so the printed `<virtual key>` is a placeholder - copy the output,
# replace it with your personal key, and paste the result into
# ~/.zshrc / ~/.bashrc (see README "Routing Claude Code through it").
env:
	@echo 'export ANTHROPIC_BASE_URL="$(URI)"'
	@echo 'export OPENAI_API_BASE="$(URI)"'
	@echo 'export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer <virtual key>"'
	@echo 'export OPENAI_API_KEY="<virtual key>"'
