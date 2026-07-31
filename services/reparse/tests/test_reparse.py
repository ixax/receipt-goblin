"""Unit tests for reparse.py's decode-then-dispatch wrapper around
common.ingest_db.reparse_event() and its keyset-pagination loop over
ingest_raw.
No real ClickHouse connection - reparse_event and get_client are
monkeypatched."""

import json

from src import reparse


def test_reparse_one_success_decodes_and_calls_reparse_event(monkeypatch):
    calls = []
    monkeypatch.setattr(reparse, "reparse_event", lambda client, payload, call_id, session_id, now: calls.append(
        (client, payload, call_id, session_id)
    ))

    fake_client = object()
    raw_payload_full = json.dumps({"litellm_call_id": "call-1", "messages": []})
    reparse._reparse_one(fake_client, "call-1", "session-1", raw_payload_full)

    assert len(calls) == 1
    client, payload, call_id, session_id = calls[0]
    assert client is fake_client
    assert payload == {"litellm_call_id": "call-1", "messages": []}
    assert call_id == "call-1"
    assert session_id == "session-1"


def test_reparse_one_unsuccess_malformed_json_skips_reparse_event(monkeypatch):
    monkeypatch.setattr(reparse, "reparse_event", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("reparse_event should not be called")
    ))

    reparse._reparse_one(object(), "call-1", "session-1", "{not valid json")  # must not raise


def test_reparse_one_unsuccess_reparse_event_raising_is_caught(monkeypatch):
    def _boom(client, payload, call_id, session_id, now):
        raise ValueError("simulated insert failure")

    monkeypatch.setattr(reparse, "reparse_event", _boom)

    raw_payload_full = json.dumps({"litellm_call_id": "call-1"})
    reparse._reparse_one(object(), "call-1", "session-1", raw_payload_full)  # must not raise


class _FakePagingClient:
    """Returns `rows_by_cursor` pages keyed by the query's cursor
    parameter, then an empty page once exhausted - mirrors reparse()'s
    keyset pagination on litellm_call_id."""

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


def test_reparse_success_pages_through_chunks_until_empty(monkeypatch):
    reparsed = []
    monkeypatch.setattr(reparse, "reparse_event", lambda client, payload, call_id, session_id, now: reparsed.append(call_id))

    pages = [
        [("call-1", "session-1", json.dumps({"litellm_call_id": "call-1"})),
         ("call-2", "session-1", json.dumps({"litellm_call_id": "call-2"}))],
        [],
    ]
    fake_client = _FakePagingClient(pages)
    monkeypatch.setattr(reparse, "get_client", lambda: fake_client)

    count = reparse.reparse("session-1")

    assert count == 2
    assert reparsed == ["call-1", "call-2"]
    # second query's cursor advanced past the first page's last call_id
    assert fake_client.queries[1]["cursor"] == "call-2"


def test_reparse_unsuccess_no_matching_rows_returns_zero(monkeypatch):
    monkeypatch.setattr(reparse, "reparse_event", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("reparse_event should not be called")
    ))
    monkeypatch.setattr(reparse, "get_client", lambda: _FakePagingClient([[]]))

    assert reparse.reparse("no-such-session") == 0
