from pathlib import Path

import yaml

# Queue mechanics; sizing rationale for each value lives in queue.yml.
# queue.yml sits beside src/ (services/_common/queue.yml), not inside it -
# config data doesn't belong under source.
#
# Two candidate locations, since __file__.resolve() behaves differently
# per environment.
# In Docker, `common/` is a flat copy of this file's own src/ (COPY
# services/_common/src/ ./common/, plus a second COPY for queue.yml itself
# - see each service's Dockerfile), so queue.yml sits two parents up.
# Locally, `services/common` is a symlink to services/_common/src.
# .resolve() follows it to the real services/_common/src/config/queue.py,
# so queue.yml (one level above src/) sits three parents up.
_here = Path(__file__).resolve()
for _candidate in (_here.parent.parent / "queue.yml", _here.parent.parent.parent / "queue.yml"):
    if _candidate.exists():
        _queue_yml = _candidate
        break
else:
    raise FileNotFoundError(f"queue.yml not found near {_here}")

_config = yaml.safe_load(_queue_yml.read_text())

STREAM_KEY = _config["stream_key"]
CONSUMER_GROUP = _config["consumer_group"]
MAXLEN = _config["maxlen"]
BATCH_SIZE = _config["batch_size"]
FLUSH_INTERVAL_MS = _config["flush_interval_ms"]
STALE_IDLE_MS = _config["stale_idle_ms"]
SIDE_STREAM_KEY = _config["side_stream_key"]
SIDE_MAXLEN = _config["side_maxlen"]
