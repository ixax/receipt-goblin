import os
import sys
import types
from pathlib import Path

os.environ.setdefault("CLICKHOUSE_HOST", "test-host")
os.environ.setdefault("CLICKHOUSE_PORT", "9000")
os.environ.setdefault("CLICKHOUSE_DATABASE", "test-db")
os.environ.setdefault("CLICKHOUSE_BOOTSTRAP_USER", "test-bootstrap")
os.environ.setdefault("CLICKHOUSE_BOOTSTRAP_PASSWORD", "test-password")

_service_dir = str(Path(__file__).resolve().parent.parent)
_services_dir = str(Path(__file__).resolve().parent.parent.parent)
sys.path[:0] = [_service_dir, _services_dir]

# Git symlinks are checked out as text files on this Windows workspace.
# Build the same `common` namespace that Docker creates from _common/src.
common = types.ModuleType("common")
common.__path__ = [str(Path(_services_dir) / "_common" / "src")]
sys.modules.setdefault("common", common)
