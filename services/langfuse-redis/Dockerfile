# Stock image, no config to bake - only the startup script (below), which
# reads REDIS_PASSWORD from the environment at runtime and replaces what
# used to be an inline compose `command:` embedding the password directly.
FROM redis:7-alpine

COPY --chmod=0755 entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
