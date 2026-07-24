# Config baked in at build time (this repo's convention), not bind-mounted.
FROM grafana/alloy:v1.5.1

COPY config.alloy /etc/alloy/config.alloy

CMD ["run", "--server.http.listen-addr=0.0.0.0:12345", "/etc/alloy/config.alloy"]
