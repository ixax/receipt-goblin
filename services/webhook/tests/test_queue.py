"""Unit tests for queue.py's enqueue_raw() fast-path/bundle-split
branching - the raw-bytes passthrough that keeps webhook's request path
from parsing/re-serializing every payload (see module + function
docstrings). get_async_redis() is monkeypatched to a fake client so no
real Redis is needed.
Moved out of _common/tests/test_queue_client.py when the producer side
split into its own module - see
plans/common-module-cleanup-refactor.md.

No pytest-asyncio dependency here - the project doesn't have it installed,
so each async coroutine under test is driven with asyncio.run() directly."""

import asyncio
import json

import pytest
from src import queue


def _run(coro):
    return asyncio.run(coro)


class _FakeAsyncRedis:
    def __init__(self):
        self.xadd_calls: list[tuple] = []

    async def xadd(self, stream_key, fields, maxlen=None, approximate=None):
        self.xadd_calls.append((stream_key, fields, maxlen, approximate))

    def pipeline(self, transaction=False):
        calls = self.xadd_calls

        class _Pipeline:
            def xadd(self, stream_key, fields, maxlen=None, approximate=None):
                calls.append((stream_key, fields, maxlen, approximate))

            async def execute(self):
                return [f"{index}-0" for index in range(len(calls))]

        return _Pipeline()


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeAsyncRedis()
    monkeypatch.setattr(queue, "get_async_redis", lambda: fake)
    return fake


def test_enqueue_raw_success_single_object_goes_straight_through_unparsed(fake_redis):
    body = json.dumps({"litellm_call_id": "abc"}).encode()

    _run(queue.enqueue_raw(body))

    assert len(fake_redis.xadd_calls) == 1
    stream_key, fields, _, _ = fake_redis.xadd_calls[0]
    assert stream_key == queue.STREAM_KEY
    # Passed through byte-for-byte, no json.loads/json.dumps round-trip.
    assert fields == {"event": body}


def test_enqueue_raw_success_leading_whitespace_before_brace_still_fast_path(fake_redis):
    body = b"   \n{\"litellm_call_id\": \"abc\"}"

    _run(queue.enqueue_raw(body))

    assert len(fake_redis.xadd_calls) == 1
    assert fake_redis.xadd_calls[0][1] == {"event": body}


def test_enqueue_raw_success_bundled_array_splits_into_one_entry_per_payload(fake_redis):
    body = json.dumps([
        {"litellm_call_id": "one"},
        {"litellm_call_id": "two"},
    ]).encode()

    _run(queue.enqueue_raw(body))

    assert len(fake_redis.xadd_calls) == 2
    decoded_events = [json.loads(call[1]["event"]) for call in fake_redis.xadd_calls]
    assert decoded_events == [{"litellm_call_id": "one"}, {"litellm_call_id": "two"}]


def test_enqueue_raw_unsuccess_malformed_bundled_array_drops_silently(fake_redis):
    body = b"[not valid json"

    _run(queue.enqueue_raw(body))

    assert fake_redis.xadd_calls == []


def test_enqueue_raw_unsuccess_xadd_failure_does_not_raise(monkeypatch):
    class _BoomRedis:
        async def xadd(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(queue, "get_async_redis", lambda: _BoomRedis())
    body = json.dumps({"litellm_call_id": "abc"}).encode()

    _run(queue.enqueue_raw(body))  # must not raise


def test_enqueue_success_skips_non_dict_items(fake_redis):
    _run(queue.enqueue([{"litellm_call_id": "abc"}, "not-a-dict", 42]))

    assert len(fake_redis.xadd_calls) == 1
    assert json.loads(fake_redis.xadd_calls[0][1]["event"]) == {"litellm_call_id": "abc"}


def test_enqueue_side_success_tags_payload_with_kind(fake_redis):
    _run(queue.enqueue_side("git_branch", {"session_id": "s1", "git_branch": "main"}))

    assert len(fake_redis.xadd_calls) == 1
    stream_key, fields, maxlen, _ = fake_redis.xadd_calls[0]
    assert stream_key == queue.SIDE_STREAM_KEY
    assert maxlen == queue.SIDE_MAXLEN
    assert fields["kind"] == "git_branch"
    assert json.loads(fields["event"]) == {"session_id": "s1", "git_branch": "main"}


def test_enqueue_side_unsuccess_xadd_failure_does_not_raise(monkeypatch):
    class _BoomRedis:
        async def xadd(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(queue, "get_async_redis", lambda: _BoomRedis())

    _run(queue.enqueue_side("plan_proposal", {"session_id": "s1"}))  # must not raise


def test_enqueue_usage_event_success_tags_source_adapter(fake_redis):
    payload = {"schema_version": 1, "event_id": "req-1"}

    _run(queue.enqueue_usage_event(payload))

    assert len(fake_redis.xadd_calls) == 1
    stream_key, fields, maxlen, _ = fake_redis.xadd_calls[0]
    assert stream_key == queue.STREAM_KEY
    assert maxlen == queue.MAXLEN
    assert fields["adapter"] == "claude_transcript"
    assert json.loads(fields["event"]) == payload


def test_enqueue_usage_events_success_pipelines_whole_batch(monkeypatch):
    calls = []

    class _Pipeline:
        def xadd(self, stream_key, fields, maxlen=None, approximate=None):
            calls.append((stream_key, fields, maxlen, approximate))

        async def execute(self):
            return ["1-0", "2-0"]

    class _Redis:
        def pipeline(self, transaction=False):
            assert transaction is False
            return _Pipeline()

    monkeypatch.setattr(queue, "get_async_redis", lambda: _Redis())

    _run(queue.enqueue_usage_events([{"event_id": "one"}, {"event_id": "two"}]))

    assert len(calls) == 2
    assert all(call[0] == queue.STREAM_KEY for call in calls)
    assert [json.loads(call[1]["event"])["event_id"] for call in calls] == ["one", "two"]


def test_enqueue_usage_events_unsuccess_propagates_redis_failure(monkeypatch):
    class _Pipeline:
        def xadd(self, *args, **kwargs):
            return None

        async def execute(self):
            raise ConnectionError("redis down")

    class _Redis:
        def pipeline(self, transaction=False):
            return _Pipeline()

    monkeypatch.setattr(queue, "get_async_redis", lambda: _Redis())

    with pytest.raises(ConnectionError, match="redis down"):
        _run(queue.enqueue_usage_events([{"event_id": "one"}]))
