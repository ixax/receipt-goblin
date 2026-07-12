ifneq (,$(wildcard ./.env))
    include .env
endif

ENV_VARS := $(shell [ -f .env ] && sed 's/=.*//' .env)
ifneq ($(MAKECMDGOALS),clear_env)
    unexport $(ENV_VARS)
endif

PORT := $(if $(strip $(LITELLM_PORT)),$(LITELLM_PORT),4000)

.PHONY: validate_key check_env set_env clear_env

validate_key:
	@if [ -z "$(strip $(LITELLM_VIRTUAL_KEY))" ]; then \
		echo "❌ Error: LITELLM_VIRTUAL_KEY is missing or empty in your .env file!" >&2; \
		exit 1; \
	fi

check_env: validate_key
	@echo "Active Shell Environment Values:"
	@echo "  ANTHROPIC_BASE_URL       = $${ANTHROPIC_BASE_URL:-[Not Set]}"
	@echo "  ANTHROPIC_MODEL          = $${ANTHROPIC_MODEL:-[Not Set]}"
	@echo "  LITELLM_VIRTUAL_KEY      = $${LITELLM_VIRTUAL_KEY:-[Not Set]}"
	@echo "  ANTHROPIC_CUSTOM_HEADERS = $${ANTHROPIC_CUSTOM_HEADERS:-[Not Set]}"

set_env: validate_key
	@echo 'Copy-paste and execute:'
	@echo 'export ANTHROPIC_BASE_URL="http://localhost:$(PORT)"'
	@echo 'export ANTHROPIC_MODEL="claude-sonnet-5"'
	@echo 'export LITELLM_VIRTUAL_KEY="$(LITELLM_VIRTUAL_KEY)"'
	@echo 'export ANTHROPIC_CUSTOM_HEADERS="x-litellm-api-key: Bearer $(LITELLM_VIRTUAL_KEY)"'

clear_env:
	@echo 'Copy-paste and execute'
	@echo 'unset ANTHROPIC_BASE_URL'
	@echo 'unset ANTHROPIC_MODEL'
	@echo 'unset LITELLM_VIRTUAL_KEY'
	@echo 'unset ANTHROPIC_CUSTOM_HEADERS'

start:
	docker compose up -d --build

stop:
	docker compose down
