import os
import sys
from pathlib import Path

os.environ.setdefault("CLICKHOUSE_HOST", "test-host")
os.environ.setdefault("CLICKHOUSE_PORT", "9000")
os.environ.setdefault("CLICKHOUSE_USER", "test-user")
os.environ.setdefault("CLICKHOUSE_PASSWORD", "test-password")
os.environ.setdefault("CLICKHOUSE_DATABASE", "test-db")

# Add paths in order: this service first, then services root
_service_dir = str(Path(__file__).resolve().parent.parent)
_services_dir = str(Path(__file__).resolve().parent.parent.parent)

# Rebuild sys.path to ensure the right order without duplicates
_new_path = [_service_dir, _services_dir]
_new_path.extend([p for p in sys.path if p not in (_service_dir, _services_dir)])
sys.path[:] = _new_path

CAPTURES_DIR = Path(__file__).resolve().parent.parent.parent / "_common" / "tests" / "captures"
