"""Unit tests for worker.py's _decode_into - the queued-payload decode/
build_event() path that replaced webhook's own inline parsing (see
worker.py module docstring). No real Redis/ClickHouse - build_event() and
the Prometheus counters are monkeypatched."""

import json

import pytest

from src import worker


@pytest.fixture(autouse=True)
def _reset_decode_failures():
    before = worker.DECODE_FAILURES._value.get()
    yield
    worker.DECODE_FAILURES._value.set(before)


def test_decode_into_success_appends_message_id_and_built_event(monkeypatch):
    monkeypatch.setattr(worker, "build_event", lambda payload: {"built": payload["litellm_call_id"]})
    entries = [("1-0", {"event": json.dumps({"litellm_call_id": "abc"})})]
    message_ids: list[str] = []
    events: list[dict] = []

    worker._decode_into(entries, message_ids, events)

    assert message_ids == ["1-0"]
    assert events == [{"built": "abc"}]


def test_decode_into_unsuccess_bad_json_acks_but_drops_event(monkeypatch):
    monkeypatch.setattr(worker, "build_event", lambda payload: pytest.fail("should not be called"))
    entries = [("1-0", {"event": "{not json"})]
    message_ids: list[str] = []
    events: list[dict] = []

    worker._decode_into(entries, message_ids, events)

    # message_id is still tracked (so xack happens and the bad entry isn't
    # redelivered forever), but no event was produced from it.
    assert message_ids == ["1-0"]
    assert events == []


def test_decode_into_unsuccess_build_event_raises_acks_but_drops_event(monkeypatch):
    def _boom(payload):
        raise ValueError("malformed payload")

    monkeypatch.setattr(worker, "build_event", _boom)
    entries = [("1-0", {"event": json.dumps({"litellm_call_id": "abc"})})]
    message_ids: list[str] = []
    events: list[dict] = []

    worker._decode_into(entries, message_ids, events)

    assert message_ids == ["1-0"]
    assert events == []


def test_decode_into_unsuccess_missing_event_field_still_tracks_message_id(monkeypatch):
    monkeypatch.setattr(worker, "build_event", lambda payload: pytest.fail("should not be called"))
    entries = [("1-0", {})]
    message_ids: list[str] = []
    events: list[dict] = []

    worker._decode_into(entries, message_ids, events)

    assert message_ids == ["1-0"]
    assert events == []


def test_decode_into_success_one_bad_payload_does_not_drop_others(monkeypatch):
    monkeypatch.setattr(worker, "build_event", lambda payload: {"built": payload["litellm_call_id"]})
    entries = [
        ("1-0", {"event": json.dumps({"litellm_call_id": "good-1"})}),
        ("2-0", {"event": "{not json"}),
        ("3-0", {"event": json.dumps({"litellm_call_id": "good-2"})}),
    ]
    message_ids: list[str] = []
    events: list[dict] = []

    worker._decode_into(entries, message_ids, events)

    assert message_ids == ["1-0", "2-0", "3-0"]
    assert events == [{"built": "good-1"}, {"built": "good-2"}]
