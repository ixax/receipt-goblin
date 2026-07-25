# Prod image - config/entrypoint baked in at build time (this repo's
# convention), not bind-mounted.
#
# user_configs/*.yaml (gitignored, may carry real remote-model hosts/keys)
# is baked in here too - whatever's actually present on disk at build time,
# secrets included. Prod deploys are expected to drop the real *.yaml files
# under services/litellm/user_configs/ before running `docker compose build
# litellm` - see README "Remote model sources". docker-compose.dev.yml
# bind-mounts the directory back for live editing without a rebuild; prod
# needs a rebuild to pick up a new/changed source.
FROM ghcr.io/berriai/litellm:main-latest

COPY config.yaml /app/litellm-config/config.yaml
COPY custom_callbacks.py /app/litellm-config/custom_callbacks.py
COPY --chmod=0755 docker-entrypoint.sh /app/litellm-config/docker-entrypoint.sh
COPY scripts/ /app/litellm-config/scripts/
COPY user_configs/ /app/litellm-config/user_configs/

ENTRYPOINT ["/bin/sh", "/app/litellm-config/docker-entrypoint.sh"]
CMD ["--port", "4000"]
