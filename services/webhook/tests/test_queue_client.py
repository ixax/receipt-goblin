"""Unit tests for queue_client.py's enqueue_raw() fast-path/bundle-split
branching - the raw-bytes passthrough that keeps webhook's request path
from parsing/re-serializing every payload (see module + function
docstrings). get_async_redis() is monkeypatched to a fake client so no
real Redis is needed.

No pytest-asyncio dependency here - the project doesn't have it installed,
so each async coroutine under test is driven with asyncio.run() directly."""

import asyncio
import json

import pytest

from src import queue_client


def _run(coro):
    return asyncio.run(coro)


class _FakeAsyncRedis:
    def __init__(self):
        self.xadd_calls: list[tuple] = []

    async def xadd(self, stream_key, fields, maxlen=None, approximate=None):
        self.xadd_calls.append((stream_key, fields, maxlen, approximate))


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeAsyncRedis()
    monkeypatch.setattr(queue_client, "get_async_redis", lambda: fake)
    return fake


def test_enqueue_raw_success_single_object_goes_straight_through_unparsed(fake_redis):
    body = json.dumps({"litellm_call_id": "abc"}).encode()

    _run(queue_client.enqueue_raw(body))

    assert len(fake_redis.xadd_calls) == 1
    stream_key, fields, _, _ = fake_redis.xadd_calls[0]
    assert stream_key == queue_client.STREAM_KEY
    # Passed through byte-for-byte, no json.loads/json.dumps round-trip.
    assert fields == {"event": body}


def test_enqueue_raw_success_leading_whitespace_before_brace_still_fast_path(fake_redis):
    body = b"   \n{\"litellm_call_id\": \"abc\"}"

    _run(queue_client.enqueue_raw(body))

    assert len(fake_redis.xadd_calls) == 1
    assert fake_redis.xadd_calls[0][1] == {"event": body}


def test_enqueue_raw_success_bundled_array_splits_into_one_entry_per_payload(fake_redis):
    body = json.dumps([
        {"litellm_call_id": "one"},
        {"litellm_call_id": "two"},
    ]).encode()

    _run(queue_client.enqueue_raw(body))

    assert len(fake_redis.xadd_calls) == 2
    decoded_events = [json.loads(call[1]["event"]) for call in fake_redis.xadd_calls]
    assert decoded_events == [{"litellm_call_id": "one"}, {"litellm_call_id": "two"}]


def test_enqueue_raw_unsuccess_malformed_bundled_array_drops_silently(fake_redis):
    body = b"[not valid json"

    _run(queue_client.enqueue_raw(body))

    assert fake_redis.xadd_calls == []


def test_enqueue_raw_unsuccess_xadd_failure_does_not_raise(monkeypatch):
    class _BoomRedis:
        async def xadd(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(queue_client, "get_async_redis", lambda: _BoomRedis())
    body = json.dumps({"litellm_call_id": "abc"}).encode()

    _run(queue_client.enqueue_raw(body))  # must not raise


def test_enqueue_success_skips_non_dict_items(fake_redis):
    _run(queue_client.enqueue([{"litellm_call_id": "abc"}, "not-a-dict", 42]))

    assert len(fake_redis.xadd_calls) == 1
    assert json.loads(fake_redis.xadd_calls[0][1]["event"]) == {"litellm_call_id": "abc"}
