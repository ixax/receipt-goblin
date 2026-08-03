# Config baked in at build time (this repo's convention), not bind-mounted.
# No secrets/per-env values in prometheus.yml (all targets are internal
# Docker DNS names), so no entrypoint templating is needed - just COPY it in.
FROM prom/prometheus:v3.0.1

COPY prometheus.yml /etc/prometheus/prometheus.yml
COPY scripts/archive_old_blocks.sh /scripts/archive_old_blocks.sh

EXPOSE 9090
