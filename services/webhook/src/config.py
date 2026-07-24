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

# Only set on clickhouse-migrate, for migrate.py's _ensure_app_user
# bootstrap. Optional so importing this module doesn't crash other services.
CLICKHOUSE_BOOTSTRAP_USER = os.environ.get("CLICKHOUSE_BOOTSTRAP_USER")
CLICKHOUSE_BOOTSTRAP_PASSWORD = os.environ.get("CLICKHOUSE_BOOTSTRAP_PASSWORD")

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])

# Verifies hooks/report_git_branch.py's Authorization header against
# LiteLLM's /key/info (server.py receive_git_branch).
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]

# webhook-worker's own /metrics port; not read by webhook/mcp-server/reparse.
WORKER_METRICS_PORT = int(os.environ.get("WORKER_METRICS_PORT", "9200"))

CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", "/app/captures"))
# Off by default: raw bodies contain real prompt/response content and
# per-request file writes add hot-path I/O. Set true for local debugging.
CAPTURE_ENABLED = os.environ.get("CAPTURE_ENABLED", "false").lower() == "true"

# Queue mechanics; sizing rationale for each value lives in config.yml.
_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())

STREAM_KEY = _config["stream_key"]
CONSUMER_GROUP = _config["consumer_group"]
MAXLEN = _config["maxlen"]
BATCH_SIZE = _config["batch_size"]
FLUSH_INTERVAL_MS = _config["flush_interval_ms"]
STALE_IDLE_MS = _config["stale_idle_ms"]
REPARSE_CHUNK_SIZE = _config["reparse_chunk_size"]
