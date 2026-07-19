#!/bin/sh
# Smoke-tests the LiteLLM proxy: lists registered models and exercises
# chat/embeddings/rerank against a given model_name. Reads
# LITELLM_MASTER_KEY/LITELLM_PORT from .env at the repo root. Exists so
# testing a model after a config change is one command, not a hand-written
# curl reinvented each time - see .claude/agents/litellm-tester.md, which
# uses this instead of generating its own curl.
#
# Usage:
#   test-models.sh list
#   test-models.sh chat   <model_name>
#   test-models.sh embed  <model_name>
#   test-models.sh rerank <model_name>
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# shellcheck disable=SC1091
. "$REPO_ROOT/.env"

BASE_URL="http://localhost:${LITELLM_PORT:-4000}"
AUTH_HEADER="x-litellm-api-key: Bearer ${LITELLM_MASTER_KEY}"

list_models() {
    curl -sf -m 10 "$BASE_URL/v1/models" -H "$AUTH_HEADER"
}

test_chat() {
    model="$1"
    curl -sf -m 20 "$BASE_URL/v1/chat/completions" \
        -H "$AUTH_HEADER" -H "Content-Type: application/json" \
        -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly one word: OK\"}],\"max_tokens\":10}"
}

test_embeddings() {
    model="$1"
    curl -sf -m 20 "$BASE_URL/v1/embeddings" \
        -H "$AUTH_HEADER" -H "Content-Type: application/json" \
        -d "{\"model\":\"$model\",\"input\":\"smoke test\"}"
}

test_rerank() {
    model="$1"
    curl -sf -m 20 "$BASE_URL/v1/rerank" \
        -H "$AUTH_HEADER" -H "Content-Type: application/json" \
        -d "{\"model\":\"$model\",\"query\":\"capital of France\",\"documents\":[\"Berlin is the capital of Germany.\",\"Paris is the capital of France.\"]}"
}

case "${1:-}" in
    list) list_models ;;
    chat) test_chat "${2:?model_name required}" ;;
    embed) test_embeddings "${2:?model_name required}" ;;
    rerank) test_rerank "${2:?model_name required}" ;;
    *)
        echo "usage: $0 {list|chat <model_name>|embed <model_name>|rerank <model_name>}" >&2
        exit 1
        ;;
esac
