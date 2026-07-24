# Config baked in at build time (this repo's convention), not bind-mounted.
FROM prom/blackbox-exporter:v0.25.0

COPY config.yml /etc/blackbox_exporter/config.yml

EXPOSE 9115
