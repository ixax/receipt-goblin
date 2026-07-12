import json
import os
import sys
from pathlib import Path

os.environ.setdefault("CLICKHOUSE_HOST", "test-host")
os.environ.setdefault("CLICKHOUSE_PORT", "9000")
os.environ.setdefault("CLICKHOUSE_USER", "test-user")
os.environ.setdefault("CLICKHOUSE_PASSWORD", "test-password")
os.environ.setdefault("CLICKHOUSE_DATABASE", "test-db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

CAPTURES_DIR = Path(__file__).resolve().parent / "captures"


def load_capture(name: str, index: int = 0) -> dict:
    """Loads payload `index` from tests/captures/<name>.json - a real
    LiteLLM StandardLoggingPayload captured by webhook/src/server.py, saved
    verbatim as one array entry per POST body."""
    data = json.loads((CAPTURES_DIR / f"{name}.json").read_text())
    items = data if isinstance(data, list) else [data]
    return items[index]
