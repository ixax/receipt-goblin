import json
import os
import sys
from pathlib import Path

os.environ.setdefault("CLICKHOUSE_HOST", "test-host")
os.environ.setdefault("CLICKHOUSE_PORT", "9000")
os.environ.setdefault("CLICKHOUSE_USER", "test-user")
os.environ.setdefault("CLICKHOUSE_PASSWORD", "test-password")
os.environ.setdefault("CLICKHOUSE_DATABASE", "test-db")
os.environ.setdefault("REDIS_HOST", "test-redis-host")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("LITELLM_BASE_URL", "http://test-litellm:4000")

# webhook/ (not webhook/src) so clickhouse_ingest.py's `from .config import
# ...` resolves - it needs to be imported as part of the `src` package for
# that relative import to work, not as a flat top-level module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CAPTURES_DIR = Path(__file__).resolve().parent / "captures"


def load_capture(name: str, index: int = 0) -> dict:
    """Loads payload `index` from tests/captures/<name>.json - a real
    LiteLLM StandardLoggingPayload captured by webhook/src/server.py, saved
    verbatim as one array entry per POST body."""
    data = json.loads((CAPTURES_DIR / f"{name}.json").read_text())
    items = data if isinstance(data, list) else [data]
    return items[index]
