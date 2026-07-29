"""Shared logging setup for every service under services/ - one format/level
convention instead of each entrypoint retyping its own basicConfig() call.
"""
import logging
import os


def create_logger(name: str | None = None) -> logging.Logger:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return logging.getLogger(name)
