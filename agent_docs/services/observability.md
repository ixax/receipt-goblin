# Observability profile (`alloy`, `blackbox`, `cadvisor`, `loki`, `node-exporter`, `nginx-exporter`, `prometheus`, `redis-exporter`)

Standard Prometheus/Loki/Alloy/blackbox-exporter/cadvisor/node-exporter/nginx-exporter/redis-exporter, opt-in `observability` compose profile feeding Grafana's "Host"/"Containers"/reliability panels.
No custom logic beyond each tool's own docs.
Config lives under `services/<name>/`, managed via `dev-ops`.
All 8 services are defined in `docker-compose.observability.yml`, loaded automatically by `make observability-*` targets alongside the core `docker-compose.yml` - not in the core file itself.
