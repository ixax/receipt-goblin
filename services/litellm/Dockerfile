FROM ghcr.io/berriai/litellm:main-latest

USER root
RUN mkdir -p /app/litellm-config

# LITELLM_LOCAL_MODEL_COST_MAP/LITELLM_MODEL_COST_MAP_PATH deliberately unset:
# litellm's default behavior already fetches
# model_prices_and_context_window.json from GitHub at startup.
# Pinning a baked-in copy here would disable that auto-update instead of
# enabling it.

USER 1000

COPY config.yaml /app/litellm-config/config.yaml
COPY custom_callbacks.py /app/litellm-config/custom_callbacks.py
COPY --chmod=0755 docker-entrypoint.sh /app/litellm-config/docker-entrypoint.sh
COPY scripts/ /app/litellm-config/scripts/
COPY user_configs/ /app/litellm-config/user_configs/

ENTRYPOINT ["/bin/sh", "/app/litellm-config/docker-entrypoint.sh"]
CMD ["--port", "4000"]
