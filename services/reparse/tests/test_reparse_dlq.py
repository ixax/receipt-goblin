"""Unit tests for reparse_dlq.py's decode-then-dispatch wrapper around
common.ingest_db.replay_dlq_row() and its keyset-pagination loop over
ingest_dlq.
No real ClickHouse connection - replay_dlq_row and get_client are
monkeypatched."""

import json

from src import reparse_dlq


def test_replay_one_success_decodes_and_calls_replay_dlq_row(monkeypatch):
    calls = []
    monkeypatch.setattr(reparse_dlq, "replay_dlq_row", lambda client, stage, row: calls.append((client, stage, row)))

    fake_client = object()
    raw_row = json.dumps(["call-1", "session-1"])
    ok = reparse_dlq._replay_one(fake_client, "agent_events", "call-1", raw_row)

    assert ok is True
    assert len(calls) == 1
    client, stage, row = calls[0]
    assert client is fake_client
    assert stage == "agent_events"
    assert row == ["call-1", "session-1"]


def test_replay_one_unsuccess_malformed_json_skips_replay_dlq_row(monkeypatch):
    monkeypatch.setattr(reparse_dlq, "replay_dlq_row", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("replay_dlq_row should not be called")
    ))

    ok = reparse_dlq._replay_one(object(), "agent_events", "call-1", "{not valid json")

    assert ok is False


def test_replay_one_unsuccess_replay_dlq_row_raising_is_caught(monkeypatch):
    def _boom(client, stage, row):
        raise ValueError("simulated insert failure")

    monkeypatch.setattr(reparse_dlq, "replay_dlq_row", _boom)

    raw_row = json.dumps(["call-1"])
    ok = reparse_dlq._replay_one(object(), "agent_events", "call-1", raw_row)

    assert ok is False


class _FakePagingClient:
    """Returns `rows_by_cursor` pages keyed by the query's cursor
    parameters, then an empty page once exhausted - mirrors
    reparse_dlq()'s keyset pagination on (occurred_at, litellm_call_id)."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.queries = []

    def query(self, query, parameters=None):
        self.queries.append(parameters)

        class _Result:
            pass

        result = _Result()
        result.result_rows = self.pages.pop(0) if self.pages else []
        return result


def test_reparse_dlq_success_pages_through_chunks_until_empty(monkeypatch):
    replayed = []
    monkeypatch.setattr(reparse_dlq, "replay_dlq_row", lambda client, stage, row: replayed.append(row[0]))

    pages = [
        [("2026-01-01T00:00:00", "agent_events", "call-1", json.dumps(["call-1"])),
         ("2026-01-01T00:00:01", "agent_events", "call-2", json.dumps(["call-2"]))],
        [],
    ]
    fake_client = _FakePagingClient(pages)
    monkeypatch.setattr(reparse_dlq, "get_client", lambda: fake_client)

    count = reparse_dlq.reparse_dlq("agent_events")

    assert count == 2
    assert replayed == ["call-1", "call-2"]
    # second query's cursor advanced past the first page's last row
    assert fake_client.queries[1]["cursor_id"] == "call-2"


def test_reparse_dlq_unsuccess_no_matching_rows_returns_zero(monkeypatch):
    monkeypatch.setattr(reparse_dlq, "replay_dlq_row", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("replay_dlq_row should not be called")
    ))
    monkeypatch.setattr(reparse_dlq, "get_client", lambda: _FakePagingClient([[]]))

    assert reparse_dlq.reparse_dlq("no-such-stage") == 0
