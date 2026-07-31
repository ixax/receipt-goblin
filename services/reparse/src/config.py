from pathlib import Path

import yaml

_config = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yml").read_text())
REPARSE_CHUNK_SIZE = _config["reparse_chunk_size"]
