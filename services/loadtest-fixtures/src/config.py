"""Env-derived connection settings plus fixture-extraction tuning from
config.yml. This service is deliberately isolated from services/webhook -
its own image, own dependency set, own config module (see AGENTS.md) - so
every env var it reads is prefixed LOADTEST_FIXTURES_, never the bare
CLICKHOUSE_*/etc. names webhook's own config.py uses, to keep the two
containers' environments from silently colliding or being confused for one
another.
"""
import os
from pathlib import Path

import yaml

CLICKHOUSE_HOST = os.environ["LOADTEST_FIXTURES_CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ["LOADTEST_FIXTURES_CLICKHOUSE_PORT"])
CLICKHOUSE_USER = os.environ["LOADTEST_FIXTURES_CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["LOADTEST_FIXTURES_CLICKHOUSE_PASSWORD"]
CLICKHOUSE_DATABASE = os.environ["LOADTEST_FIXTURES_CLICKHOUSE_DATABASE"]

# Output dir - a dedicated Docker volume in prod, mounted rw here and ro
# into the `loadtest` service (see docker-compose.yml).
FIXTURES_DIR = Path(os.environ.get("LOADTEST_FIXTURES_DIR", "/app/loadtest_fixtures"))

_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())
FIXTURES_CHUNK_SIZE = _config["fixtures_chunk_size"]
FIXTURES_TTL_HOURS = _config["fixtures_ttl_hours"]
