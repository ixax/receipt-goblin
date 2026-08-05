"""Unit tests for reparse_dlq.py's decode-then-dispatch wrapper around
common.ingest_db.replay_dlq_row() and its LIMIT-only pagination loop over
ingest_dlq_unresolved.
No real ClickHouse connection - replay_dlq_row, mark_dlq_rows_resolved, and
get_client are monkeypatched."""

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
    """Returns successive `pages` on each query(), then an empty page once
    exhausted - mirrors reparse_dlq()'s plain LIMIT query against
    ingest_dlq_unresolved (no cursor: each page's rows would actually be
    excluded from the next page by mark_dlq_rows_resolved having run
    against a real view, but this fake just hands back whatever pages it
    was given)."""

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
    resolved_calls = []
    monkeypatch.setattr(reparse_dlq, "mark_dlq_rows_resolved", lambda client, rows: resolved_calls.append(rows))

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
    # No cursor parameters - ingest_dlq_unresolved pagination is cursor-free.
    assert all("cursor_id" not in (params or {}) for params in fake_client.queries)
    assert resolved_calls == [[
        ("2026-01-01T00:00:00", "agent_events", "call-1"),
        ("2026-01-01T00:00:01", "agent_events", "call-2"),
    ]]


def test_reparse_dlq_unsuccess_no_matching_rows_returns_zero(monkeypatch):
    monkeypatch.setattr(reparse_dlq, "replay_dlq_row", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("replay_dlq_row should not be called")
    ))
    monkeypatch.setattr(reparse_dlq, "get_client", lambda: _FakePagingClient([[]]))

    assert reparse_dlq.reparse_dlq("no-such-stage") == 0


def test_reparse_dlq_unsuccess_zero_progress_page_stops_without_looping(monkeypatch):
    # Every row in the page fails replay - mark_dlq_rows_resolved must never
    # be called for a still-failing row, and since none of those rows ever
    # drop out of ingest_dlq_unresolved, the loop must stop after one page
    # rather than re-fetching the exact same page forever.
    monkeypatch.setattr(reparse_dlq, "replay_dlq_row", lambda *a, **kw: (_ for _ in ()).throw(
        ValueError("simulated permanent failure")
    ))
    resolved_calls = []
    monkeypatch.setattr(reparse_dlq, "mark_dlq_rows_resolved", lambda client, rows: resolved_calls.append(rows))

    pages = [
        [("2026-01-01T00:00:00", "agent_events", "call-1", json.dumps(["call-1"])),
         ("2026-01-01T00:00:01", "agent_events", "call-2", json.dumps(["call-2"]))],
        [("2026-01-01T00:00:00", "agent_events", "call-1", json.dumps(["call-1"])),
         ("2026-01-01T00:00:01", "agent_events", "call-2", json.dumps(["call-2"]))],
    ]
    fake_client = _FakePagingClient(pages)
    monkeypatch.setattr(reparse_dlq, "get_client", lambda: fake_client)

    count = reparse_dlq.reparse_dlq("agent_events")

    assert count == 2
    assert len(fake_client.queries) == 1
    assert resolved_calls == []
