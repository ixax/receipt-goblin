"""Unit tests for worker.py's _decode_into/_decode_side_into - the
queued-payload decode paths that replaced webhook's own inline parsing
(see worker.py module docstring).
No real Redis/ClickHouse - build_event(), the side-channel row-builders,
and the Prometheus counters are monkeypatched."""

import json

import pytest

from src import worker


@pytest.fixture(autouse=True)
def _reset_decode_failures():
    before = worker.DECODE_FAILURES._value.get()
    side_before = worker.SIDE_DECODE_FAILURES._value.get()
    yield
    worker.DECODE_FAILURES._value.set(before)
    worker.SIDE_DECODE_FAILURES._value.set(side_before)


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


def _side_buffers():
    return [], [], [], []


def test_decode_side_into_success_dispatches_git_branch_by_kind(monkeypatch):
    monkeypatch.setattr(worker, "_git_branch_row", lambda payload, now: ["row", payload["session_id"], now])
    entries = [("1-0", {"kind": "git_branch", "event": json.dumps({"session_id": "s1"})})]
    message_ids, git_branch_rows, plan_proposal_rows, alert_rows = _side_buffers()

    worker._decode_side_into(entries, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)

    assert message_ids == ["1-0"]
    assert len(git_branch_rows) == 1
    assert git_branch_rows[0][:2] == ["row", "s1"]
    assert plan_proposal_rows == []
    assert alert_rows == []


def test_decode_side_into_success_dispatches_plan_proposal_by_kind(monkeypatch):
    monkeypatch.setattr(worker, "_plan_proposal_row", lambda payload, now: ["row", payload["session_id"], now])
    entries = [("1-0", {"kind": "plan_proposal", "event": json.dumps({"session_id": "s1"})})]
    message_ids, git_branch_rows, plan_proposal_rows, alert_rows = _side_buffers()

    worker._decode_side_into(entries, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)

    assert message_ids == ["1-0"]
    assert len(plan_proposal_rows) == 1
    assert git_branch_rows == []
    assert alert_rows == []


def test_decode_side_into_success_dispatches_litellm_alert_by_kind(monkeypatch):
    monkeypatch.setattr(worker, "_litellm_alert_row", lambda payload, now: ["row", payload["event"], now])
    entries = [("1-0", {"kind": "litellm_alert", "event": json.dumps({"event": "budget_exceeded"})})]
    message_ids, git_branch_rows, plan_proposal_rows, alert_rows = _side_buffers()

    worker._decode_side_into(entries, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)

    assert message_ids == ["1-0"]
    assert len(alert_rows) == 1
    assert git_branch_rows == []
    assert plan_proposal_rows == []


def test_decode_side_into_unsuccess_unknown_kind_acks_but_drops_row():
    entries = [("1-0", {"kind": "mystery", "event": json.dumps({"session_id": "s1"})})]
    message_ids, git_branch_rows, plan_proposal_rows, alert_rows = _side_buffers()

    worker._decode_side_into(entries, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)

    assert message_ids == ["1-0"]
    assert git_branch_rows == plan_proposal_rows == alert_rows == []


def test_decode_side_into_unsuccess_bad_json_acks_but_drops_row():
    entries = [("1-0", {"kind": "git_branch", "event": "{not json"})]
    message_ids, git_branch_rows, plan_proposal_rows, alert_rows = _side_buffers()

    worker._decode_side_into(entries, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)

    assert message_ids == ["1-0"]
    assert git_branch_rows == plan_proposal_rows == alert_rows == []


def test_decode_side_into_unsuccess_missing_event_field_still_tracks_message_id():
    entries = [("1-0", {"kind": "git_branch"})]
    message_ids, git_branch_rows, plan_proposal_rows, alert_rows = _side_buffers()

    worker._decode_side_into(entries, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)

    assert message_ids == ["1-0"]
    assert git_branch_rows == plan_proposal_rows == alert_rows == []


def test_decode_side_into_success_one_bad_entry_does_not_drop_others(monkeypatch):
    monkeypatch.setattr(worker, "_git_branch_row", lambda payload, now: ["row", payload["session_id"], now])
    entries = [
        ("1-0", {"kind": "git_branch", "event": json.dumps({"session_id": "good-1"})}),
        ("2-0", {"kind": "git_branch", "event": "{not json"}),
        ("3-0", {"kind": "git_branch", "event": json.dumps({"session_id": "good-2"})}),
    ]
    message_ids, git_branch_rows, plan_proposal_rows, alert_rows = _side_buffers()

    worker._decode_side_into(entries, message_ids, git_branch_rows, plan_proposal_rows, alert_rows)

    assert message_ids == ["1-0", "2-0", "3-0"]
    assert len(git_branch_rows) == 2
