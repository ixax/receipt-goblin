# Config baked in at build time (this repo's convention - see AGENTS.md /
# feedback memory "no config volume mounts"), not bind-mounted. No secrets or
# per-env values in prometheus.yml (every target is an internal Docker DNS
# name), so no entrypoint templating is needed either - just COPY it in.
FROM prom/prometheus:v3.0.1

COPY prometheus.yml /etc/prometheus/prometheus.yml

EXPOSE 9090
