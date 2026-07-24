# Prod image - config/entrypoint baked in at build time (this repo's
# convention - see AGENTS.md / feedback memory "no config volume mounts"), not
# bind-mounted. A config.yaml/entrypoint change means rebuilding this image
# (`docker compose build litellm`), not editing a mounted file.
#
# user_configs/*.yaml is the one exception, deliberately NOT baked in here -
# those files are gitignored and may carry real remote-model hosts/keys the
# user doesn't want permanently embedded in an image layer, so
# docker-compose.yml keeps a scoped bind mount for just that subdirectory
# (see docker-entrypoint.sh's merge logic). Only the tracked format-spec
# template is baked, for `docker build` to succeed standalone even with an
# empty user_configs/.
FROM ghcr.io/berriai/litellm:main-latest

COPY config.yaml /app/litellm-config/config.yaml
COPY custom_callbacks.py /app/litellm-config/custom_callbacks.py
COPY --chmod=0755 docker-entrypoint.sh /app/litellm-config/docker-entrypoint.sh
COPY scripts/ /app/litellm-config/scripts/
COPY user_configs/config.yaml.tmpl /app/litellm-config/user_configs/config.yaml.tmpl

ENTRYPOINT ["/bin/sh", "/app/litellm-config/docker-entrypoint.sh"]
CMD ["--port", "4000"]
