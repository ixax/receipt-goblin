from pathlib import Path

import yaml

# Queue mechanics; sizing rationale for each value lives in queue.yml.
_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "queue.yml").read_text())

STREAM_KEY = _config["stream_key"]
CONSUMER_GROUP = _config["consumer_group"]
MAXLEN = _config["maxlen"]
BATCH_SIZE = _config["batch_size"]
FLUSH_INTERVAL_MS = _config["flush_interval_ms"]
STALE_IDLE_MS = _config["stale_idle_ms"]
