"""Env-derived connection settings plus queue-mechanics constants from
config.yml. Defaults for CLICKHOUSE_*/REDIS_* live only in docker-compose.yml
(see AGENTS.md) - this module reads them, doesn't default them.
"""
import os
from pathlib import Path

import yaml

CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])

# Verifies hooks/report_git_branch.py's Authorization header against
# LiteLLM's /key/info (server.py receive_git_branch).
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]

# webhook-worker's own /metrics port; not read by webhook/mcp-server/reparse.
# Fixed, not env-configurable: docker-compose.yml never passes
# WORKER_METRICS_PORT into webhook-worker's environment, so an env var here
# would be dead weight - prometheus.yml's scrape target is hardcoded to
# match (webhook-worker:9200).
WORKER_METRICS_PORT = 9200

# Where loadtest.py reads its replay corpus from - a dedicated Docker
# volume in prod, written by the separate loadtest-fixtures service
# (services/loadtest-fixtures/, see AGENTS.md) and mounted ro here.
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", "/app/loadtest_fixtures"))

# Queue mechanics; sizing rationale for each value lives in config.yml.
_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())

STREAM_KEY = _config["stream_key"]
CONSUMER_GROUP = _config["consumer_group"]
MAXLEN = _config["maxlen"]
BATCH_SIZE = _config["batch_size"]
FLUSH_INTERVAL_MS = _config["flush_interval_ms"]
STALE_IDLE_MS = _config["stale_idle_ms"]
REPARSE_CHUNK_SIZE = _config["reparse_chunk_size"]
