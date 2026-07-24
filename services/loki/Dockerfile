# Config baked in at build time (this repo's convention), not bind-mounted.
FROM grafana/loki:3.3.2

COPY config.yaml /etc/loki/config.yaml

EXPOSE 3100

CMD ["-config.file=/etc/loki/config.yaml"]
