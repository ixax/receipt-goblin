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

exec docker/prod_entrypoint.sh --config "$EFFECTIVE_CONFIG" "$@"
