# Observability profile (`alloy`, `blackbox`, `loki`, `node-exporter`, `prometheus`)

Standard Prometheus/Loki/Alloy/blackbox-exporter/node-exporter, opt-in `observability` compose profile feeding Grafana's "Host"/"Containers"/reliability panels.
No custom logic worth documenting beyond each tool's own docs - config lives under `services/<name>/`, managed via `dev-ops`.
