#!/bin/sh
# Reads the auth password from the environment at container start instead of
# a compose-level `command:` CLI arg - REDIS_PASSWORD is a runtime secret and
# can't be baked into the image at build time.
set -eu

# No fallback enforced here - matches the previous compose `command:`, which
# also silently passed through an empty LANGFUSE_REDIS_PASSWORD (default,
# see docker-compose.yml's x-langfuse-redis-password anchor) until the user
# fills in .env to enable the `langfuse` profile for real.
exec redis-server --requirepass "${REDIS_PASSWORD:-}" --maxmemory-policy noeviction
