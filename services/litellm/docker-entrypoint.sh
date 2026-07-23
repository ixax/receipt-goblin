#!/bin/sh
# Merges every *.yaml file in user_configs/ into config.yaml via LiteLLM's
# own `include:` directive
# (https://docs.litellm.ai/docs/proxy/config_management). These are plain,
# hand-written LiteLLM config - real host/model values, no env var
# indirection, no naming convention required - so a remote model simply
# doesn't exist as a model_name unless its .yaml file actually exists in
# user_configs/ (see user_configs/config.yaml.tmpl for the format
# spec/example to copy from). Adding a new remote model source never needs
# an entrypoint.sh or docker-compose.yml edit - just drop a .yaml file in
# user_configs/ and restart the container.
#
# All matched files go under a SINGLE `include:` list, not one `include:`
# key per file - YAML mappings can't have duplicate keys, so writing a
# separate `include:` block per file silently drops every file but the
# last one (only the last `include:` key survives parsing).
set -eu

CONFIG_DIR=/app/litellm-config
BASE_CONFIG="$CONFIG_DIR/config.yaml"
USER_CONFIG_DIR="$CONFIG_DIR/user_configs"
EFFECTIVE_CONFIG=/tmp/litellm-config.yaml

cp "$BASE_CONFIG" "$EFFECTIVE_CONFIG"

# custom_callbacks.py (services/litellm/custom_callbacks.py) is referenced
# from config.yaml as a bare `custom_callbacks.session_id_handler` module
# path, which litellm resolves relative to the config file it was started
# with - copy it next to EFFECTIVE_CONFIG (not just $CONFIG_DIR) so that
# resolution works against the merged /tmp config too.
if [ -f "$CONFIG_DIR/custom_callbacks.py" ]; then
    cp "$CONFIG_DIR/custom_callbacks.py" "$(dirname "$EFFECTIVE_CONFIG")/custom_callbacks.py"
fi

found=""
for f in "$USER_CONFIG_DIR"/*.yaml; do
    [ -f "$f" ] || continue
    found="$found $f"
done

if [ -n "$found" ]; then
    echo "docker-entrypoint.sh: merging user_configs:$found" >&2
    printf 'include:\n' >> "$EFFECTIVE_CONFIG"
    for f in $found; do
        printf '  - %s\n' "$f" >> "$EFFECTIVE_CONFIG"
    done
else
    echo "docker-entrypoint.sh: no user_configs/*.yaml found - no remote models merged" >&2
fi

# Seed a dummy, non-functional auth.json for litellm's built-in `chatgpt`
# provider (llms/chatgpt/authenticator.py). Without any auth.json, a request
# with no forwarded Codex/ChatGPT token (see custom_callbacks.py's
# ChatGPTAuthForwardHandler) would make the Authenticator fall through to its
# interactive device-code login flow and hang the request instead of failing
# cleanly. This container runs as root ($HOME=/root) and the litellm-config
# mount is read-only, so this can't just be a committed file under
# services/litellm/ - it's written fresh on every start instead. It is never
# a real credential (only per-caller forwarded tokens are), so an
# unauthenticated call just gets a real 401/403 from chatgpt.com.
CHATGPT_AUTH_DIR="${CHATGPT_TOKEN_DIR:-$HOME/.config/litellm/chatgpt}"
mkdir -p "$CHATGPT_AUTH_DIR"
cat > "$CHATGPT_AUTH_DIR/${CHATGPT_AUTH_FILE:-auth.json}" <<'EOF'
{
  "access_token": "dummy-no-op-token-not-a-real-credential",
  "account_id": "dummy-account-id",
  "expires_at": 4102444800
}
EOF

exec docker/prod_entrypoint.sh --config "$EFFECTIVE_CONFIG" "$@"
