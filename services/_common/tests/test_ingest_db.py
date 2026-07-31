"""Unit tests for ingest_db.py's ClickHouse-touching functions -
ingest_events_batch (runs in webhook-worker, takes build_event() outputs
read back off Redis and inserts them with one client.insert() per table).
Uses a fake in-memory client throughout; no real ClickHouse connection."""

from conftest import load_capture

from common import ingest_db as db


class _FakeClient:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))

    def query(self, query, parameters=None, **kwargs):
        class _Result:
            result_rows = []
        # _resolve_client_id's "SELECT cityHash64({v:String})" lookup - a
        # fake but deterministic/positive id so batch-level dedup can be
        # asserted without a real ClickHouse connection.
        if parameters and "v" in parameters and "cityHash64" in query:
            _Result.result_rows = [[abs(hash(parameters["v"])) or 1]]
        return _Result()


def test_ingest_events_batch_success_issues_one_insert_per_table(monkeypatch):
    events = [
        db.build_event(load_capture("success_plain")),
        db.build_event(load_capture("success_with_command")),
    ]
    fake_client = _FakeClient()
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    tables = [table for table, _rows, _cols in fake_client.inserts]
    assert tables.count("ingest_raw") == 1
    assert tables.count("agent_events") == 1
    assert tables.count("agent_usage") == 1
    assert tables.count("agent_messages") == 1

    event_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_events")
    assert len(event_rows) == 2

    source_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "ingest_raw")
    assert len(source_rows) == 2


def test_ingest_events_batch_success_dedups_dimension_rows_by_id(monkeypatch):
    # Both captures share the same user/team, so this batch should insert
    # one ai_gateway_users/ai_gateway_groups row, not one per event.
    events = [
        db.build_event(load_capture("success_plain")),
        db.build_event(load_capture("success_with_command")),
    ]
    fake_client = _FakeClient()
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    user_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "ai_gateway_users")
    assert len(user_rows) == 1

    group_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "ai_gateway_groups")
    assert len(group_rows) == 1


def test_ingest_events_batch_success_resolves_and_dedups_client_rows(monkeypatch):
    # Both captures carry the same claude-cli user_agent - one dedup'd
    # `clients` row, and every event_row's event_client_id should match it.
    events = [
        db.build_event(load_capture("success_plain")),
        db.build_event(load_capture("success_with_command")),
    ]
    fake_client = _FakeClient()
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    client_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "clients")
    assert len(client_rows) == 1
    assert client_rows[0][1] == "claude-cli/2.1.207 (external, cli)"

    event_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_events")
    event_client_id_idx = db._EVENT_COLUMNS.index("event_client_id")
    assert all(row[event_client_id_idx] == client_rows[0][0] for row in event_rows)


def test_ingest_events_batch_unsuccess_empty_list_skips_client_entirely(monkeypatch):
    monkeypatch.setattr(db, "get_client", lambda: (_ for _ in ()).throw(AssertionError("get_client should not be called")))
    db.ingest_events_batch([])


class _PoisonRowClient(_FakeClient):
    """Rejects the whole-batch agent_usage insert (like a real UInt32
    range violation) but accepts single-row inserts for every row except
    the one belonging to poison_call_id - mirrors what ClickHouse actually
    does: the bad row sinks a bulk insert, individual rows recover."""
    def __init__(self, poison_call_id):
        super().__init__()
        self.poison_call_id = poison_call_id

    def insert(self, table, rows, column_names):
        if table == "agent_usage" and len(rows) > 1:
            raise ValueError("simulated column range violation")
        if table == "agent_usage" and len(rows) == 1:
            call_id_idx = column_names.index("litellm_call_id")
            if rows[0][call_id_idx] == self.poison_call_id:
                raise ValueError("simulated column range violation")
        super().insert(table, rows, column_names)


def test_ingest_events_batch_unsuccess_poison_row_isolated_to_ingest_dlq(monkeypatch):
    good_payload = load_capture("success_plain")
    poison_payload = load_capture("success_with_command")
    events = [db.build_event(good_payload), db.build_event(poison_payload)]
    fake_client = _PoisonRowClient(poison_call_id=poison_payload["litellm_call_id"])
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    usage_single_inserts = [rows for table, rows, _cols in fake_client.inserts if table == "agent_usage"]
    # The failed bulk attempt (both rows) never lands in .inserts (it
    # raised before appending); of the two per-row retries, only the
    # non-poison one succeeds and gets recorded.
    assert len(usage_single_inserts) == 1
    assert len(usage_single_inserts[0]) == 1

    failure_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "ingest_dlq")
    assert len(failure_rows) == 1
    failure_cols = next(cols for table, _rows, cols in fake_client.inserts if table == "ingest_dlq")
    values = dict(zip(failure_cols, failure_rows[0]))
    assert values["stage"] == "agent_usage"
    assert values["litellm_call_id"] == poison_payload["litellm_call_id"]

    # agent_events/agent_messages (unaffected tables) still got their rows.
    event_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_events")
    assert len(event_rows) == 2
