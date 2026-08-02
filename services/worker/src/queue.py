"""Redis Streams consumer side - webhook-worker (worker.py) reads back what
webhook (server.py) XADDs unmodified onto the stream.
Split out of common/queue_client.py - see
plans/common-module-cleanup-refactor.md.
"""
import redis

from common.config.redis import REDIS_HOST, REDIS_PORT

_sync_client = None


def get_redis() -> redis.Redis:
    """Used by webhook-worker - a plain blocking consumer loop, no event
    loop to share. decode_responses=True since it only reads back JSON text."""
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    return _sync_client
