import os
import sys
from pathlib import Path

# server.py reads these at import time (Defaults live in docker-compose.yml;
# always set by container start, no fallback needed - see server.py) - set
# harmless placeholders so importing `src.server` doesn't need a real
# ClickHouse instance or container environment.
os.environ.setdefault("CLICKHOUSE_HOST", "test-host")
os.environ.setdefault("CLICKHOUSE_PORT", "9000")
os.environ.setdefault("CLICKHOUSE_MCP_USER", "test-user")
os.environ.setdefault("CLICKHOUSE_MCP_PASSWORD", "test-password")
os.environ.setdefault("CLICKHOUSE_DATABASE", "test-db")
os.environ.setdefault("MCP_DEV_PORT", "8001")

# Add paths in order: this service first, then services root (mirrors
# services/worker/tests/conftest.py's pattern - `from src import server`
# resolves against the service dir, `from common import ...` inside
# server.py resolves against the services root).
_service_dir = str(Path(__file__).resolve().parent.parent)
_services_dir = str(Path(__file__).resolve().parent.parent.parent)

_new_path = [_service_dir, _services_dir]
_new_path.extend(p for p in sys.path if p not in (_service_dir, _services_dir))
sys.path[:] = _new_path
