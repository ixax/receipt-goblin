# Prod image - config/entrypoint baked in at build time (this repo's
# convention), not bind-mounted.
#
# user_configs/*.yaml is the exception, NOT baked in - those files are
# gitignored and may carry real remote-model hosts/keys, so
# docker-compose.yml bind-mounts just that subdirectory instead. Only the
# tracked template is baked, so `docker build` still works standalone.
FROM ghcr.io/berriai/litellm:main-latest

COPY config.yaml /app/litellm-config/config.yaml
COPY custom_callbacks.py /app/litellm-config/custom_callbacks.py
COPY --chmod=0755 docker-entrypoint.sh /app/litellm-config/docker-entrypoint.sh
COPY scripts/ /app/litellm-config/scripts/
COPY user_configs/config.yaml.tmpl /app/litellm-config/user_configs/config.yaml.tmpl

ENTRYPOINT ["/bin/sh", "/app/litellm-config/docker-entrypoint.sh"]
CMD ["--port", "4000"]
