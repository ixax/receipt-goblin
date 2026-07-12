"""Single place for every value webhook/webhook-worker can be tuned by -
env-derived connection settings and the hardcoded queue-mechanics constants
alike. docker-compose.yml is the only place CLICKHOUSE_*/REDIS_* defaults
live (see AGENTS.md "No per-service env defaults") - this module just reads
them, it doesn't default them itself.
"""
import os
from pathlib import Path

CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["CLICKHOUSE_DATABASE"]

REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])

CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", "/app/captures"))
# Off by default - raw POST bodies contain real prompt/response content and
# writing one file per request adds disk I/O to the hot path. Set
# CAPTURE_ENABLED=true (e.g. for local debugging) to have server.py write
# them to CAPTURE_DIR again.
CAPTURE_ENABLED = os.environ.get("CAPTURE_ENABLED", "false").lower() == "true"

# Queue mechanics - webhook (producer, queue_client.enqueue) / webhook-worker
# (consumer, worker.run) - see AGENTS.md "Why a queue in front of ClickHouse"
# for the sizing math behind these numbers.
STREAM_KEY = "webhook:events"
CONSUMER_GROUP = "clickhouse-ingest"
# Approximate cap on the stream's length, not a hard byte budget - sized so
# that even a full stream (~100KB/event observed) stays within the redis
# service's mem_limit. Past this, XADD trims the oldest entries: acceptable
# for this best-effort tracking pipeline, a backlog this deep means the
# worker or ClickHouse is already stuck.
MAXLEN = 5000
BATCH_SIZE = 500  # XREADGROUP COUNT - events per worker batch/insert
BLOCK_MS = 2000  # XREADGROUP BLOCK - max wait for a batch to fill
# XAUTOCLAIM threshold - reclaim entries a prior consumer XREADGROUP'd but
# never XACK'd for this long (covers a worker crashing/restarting mid-batch).
STALE_IDLE_MS = 5 * 60 * 1000
