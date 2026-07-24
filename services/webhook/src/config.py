"""Single place for every value webhook/webhook-worker can be tuned by -
env-derived connection settings, and the queue-mechanics constants loaded
from config.yml. docker-compose.yml is the only place CLICKHOUSE_*/REDIS_*
defaults live (see AGENTS.md "No per-service env defaults") - this module
just reads them, it doesn't default them itself.
"""
import os
from pathlib import Path

import yaml

CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]

# Only set on clickhouse-migrate (see docker-compose.yml) - used solely by
# migrate.py's _ensure_app_user bootstrap step to create/refresh
# CLICKHOUSE_USER above via SQL. Optional here (unlike the vars above) so
# importing this module doesn't crash webhook/webhook-worker/mcp-server/
# reparse, none of which receive these env vars.
CLICKHOUSE_BOOTSTRAP_USER = os.environ.get("CLICKHOUSE_BOOTSTRAP_USER")
CLICKHOUSE_BOOTSTRAP_PASSWORD = os.environ.get("CLICKHOUSE_BOOTSTRAP_PASSWORD")

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])

# For verifying hooks/report_git_branch.py's Authorization header against
# LiteLLM's own /key/info - see server.py receive_git_branch.
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]

# webhook-worker's own /metrics (prometheus_client.start_http_server) - see
# worker.py. Not read by webhook/mcp-server/reparse.
WORKER_METRICS_PORT = int(os.environ.get("WORKER_METRICS_PORT", "9200"))

CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", "/app/captures"))
# Off by default - raw POST bodies contain real prompt/response content and
# writing one file per request adds disk I/O to the hot path. Set
# CAPTURE_ENABLED=true (e.g. for local debugging) to have server.py write
# them to CAPTURE_DIR again.
CAPTURE_ENABLED = os.environ.get("CAPTURE_ENABLED", "false").lower() == "true"

# Queue mechanics - see config.yml (sizing rationale for each value lives
# there now, since that's the file you actually edit to tune them).
_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())

STREAM_KEY = _config["stream_key"]
CONSUMER_GROUP = _config["consumer_group"]
MAXLEN = _config["maxlen"]
BATCH_SIZE = _config["batch_size"]
FLUSH_INTERVAL_MS = _config["flush_interval_ms"]
STALE_IDLE_MS = _config["stale_idle_ms"]
REPARSE_CHUNK_SIZE = _config["reparse_chunk_size"]
