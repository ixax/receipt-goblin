# Config baked in at build time (this repo's convention), not bind-mounted.
# No secrets/per-env values in prometheus.yml (all targets are internal
# Docker DNS names), so no entrypoint templating is needed - just COPY it in.
FROM prom/prometheus:v3.0.1

COPY prometheus.yml /etc/prometheus/prometheus.yml

EXPOSE 9090
