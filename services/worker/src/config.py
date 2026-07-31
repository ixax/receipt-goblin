# webhook-worker's own /metrics port; not read by webhook/mcp-dev/mcp-stats/
# reparse.
# Fixed, not env-configurable: docker-compose.yml never passes
# WORKER_METRICS_PORT into webhook-worker's environment, so an env var here
# would be dead weight - prometheus.yml's scrape target is hardcoded to
# match (webhook-worker:9200).
WORKER_METRICS_PORT = 9200
