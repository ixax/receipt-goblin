#!/bin/sh
# Conditionally merges config.ollama.yaml into config.yaml via LiteLLM's own
# `include:` directive (https://docs.litellm.ai/docs/proxy/config_management)
# - only when OLLAMA_HOST is actually set, so local_reasoning/local_embeddings
# don't exist as model_name entries at all otherwise, rather than existing
# but pointing at an unreachable host.
set -eu

BASE_CONFIG=/app/config.yaml
OLLAMA_CONFIG=/app/config.ollama.yaml
EFFECTIVE_CONFIG=/tmp/litellm-config.yaml

cp "$BASE_CONFIG" "$EFFECTIVE_CONFIG"

if [ -n "${OLLAMA_HOST:-}" ]; then
    printf 'include:\n  - %s\n' "$OLLAMA_CONFIG" >> "$EFFECTIVE_CONFIG"
fi

exec docker/prod_entrypoint.sh --config "$EFFECTIVE_CONFIG" "$@"
