"""Unit tests for ingest_db.py's ClickHouse-touching functions -
ingest_events_batch (runs in webhook-worker, takes build_event() outputs
read back off Redis and inserts them with one client.insert() per table).
Uses a fake in-memory client throughout; no real ClickHouse connection."""

import json

from conftest import load_capture

from common import ingest_db as db
from common.ingest_parsing import build_event


class _FakeClient:
    def __init__(self):
        self.inserts = []
        self.invocation_queries = 0

    def insert(self, table, rows, column_names, column_type_names=None):
        self.inserts.append((table, rows, column_names))

    def query(self, query, parameters=None, **kwargs):
        class _Result:
            result_rows = []
        # _resolve_client_id's "SELECT cityHash64({v:String})" lookup - a
        # fake but deterministic/positive id so batch-level dedup can be
        # asserted without a real ClickHouse connection.
        if parameters and "v" in parameters and "cityHash64" in query:
            _Result.result_rows = [[abs(hash(parameters["v"])) or 1]]
        if parameters and "agent_id" in parameters:
            self.invocation_queries += 1
        return _Result()


def test_ingest_events_batch_success_issues_one_insert_per_table(monkeypatch):
    events = [
        build_event(load_capture("success_plain")),
        build_event(load_capture("success_with_command")),
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
        build_event(load_capture("success_plain")),
        build_event(load_capture("success_with_command")),
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
        build_event(load_capture("success_plain")),
        build_event(load_capture("success_with_command")),
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

    def insert(self, table, rows, column_names, column_type_names=None):
        if table == "agent_usage" and len(rows) > 1:
            raise ValueError("simulated column range violation")
        if table == "agent_usage" and len(rows) == 1:
            call_id_idx = column_names.index("litellm_call_id")
            if rows[0][call_id_idx] == self.poison_call_id:
                raise ValueError("simulated column range violation")
        super().insert(table, rows, column_names, column_type_names)


def test_ingest_events_batch_unsuccess_poison_row_isolated_to_ingest_dlq(monkeypatch):
    good_payload = load_capture("success_plain")
    poison_payload = load_capture("success_with_command")
    events = [build_event(good_payload), build_event(poison_payload)]
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


def test_ingest_events_batch_resolves_agent_fields_from_same_batch_spawn(monkeypatch):
    # success_with_agent_and_skill's invocation_rows spawns agent_id
    # "aac9d05f148e9ae4a" (test-researcher v1.0.0).
    # success_subagent_call's own x-claude-code-agent-id header is that
    # same agent_id - the spawn-and-child's-first-call-in-the-same-batch
    # case from plans/fix-agent-version-ingest-race.md.
    # This should resolve from _invocation_batch_map with zero ClickHouse
    # round trips.
    spawner_payload = load_capture("success_with_agent_and_skill")
    child_payload = load_capture("success_subagent_call")
    events = [build_event(spawner_payload), build_event(child_payload)]
    fake_client = _FakeClient()
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    assert fake_client.invocation_queries == 0

    event_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_events")
    child_call_id = child_payload["litellm_call_id"]
    child_row = next(row for row in event_rows if row[db._EVENT_CALL_ID_IDX] == child_call_id)
    assert child_row[db._EVENT_AGENT_NAME_IDX] == "test-researcher"
    assert child_row[db._EVENT_AGENT_VERSION_IDX] == "1.0.0"


class _EventuallyResolvingClient(_FakeClient):
    """Returns a blank agent_invocations lookup for its first `blank_calls`
    query()s, then a real row - simulates the spawn row committing a beat
    after the child's first call is processed."""

    def __init__(self, blank_calls, subagent_type, agent_version):
        super().__init__()
        self.blank_calls = blank_calls
        self.subagent_type = subagent_type
        self.agent_version = agent_version

    def query(self, query, parameters=None, **kwargs):
        class _Result:
            result_rows = []
        if parameters and "v" in parameters and "cityHash64" in query:
            _Result.result_rows = [[abs(hash(parameters["v"])) or 1]]
            return _Result()
        if parameters and "agent_id" in parameters:
            self.invocation_queries += 1
            if self.invocation_queries > self.blank_calls:
                _Result.result_rows = [[self.subagent_type, self.agent_version]]
        return _Result()


def test_ingest_events_batch_resolves_agent_fields_via_retry_on_cross_batch_miss(monkeypatch):
    # No same-batch spawn row this time - only the ClickHouse retry loop in
    # _BatchWriter._agent_fields can resolve it.
    monkeypatch.setattr(db.time, "sleep", lambda _seconds: None)
    child_payload = load_capture("success_subagent_call")
    events = [build_event(child_payload)]
    fake_client = _EventuallyResolvingClient(blank_calls=2, subagent_type="test-researcher", agent_version="1.0.0")
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    assert fake_client.invocation_queries == 3

    event_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_events")
    assert event_rows[0][db._EVENT_AGENT_NAME_IDX] == "test-researcher"
    assert event_rows[0][db._EVENT_AGENT_VERSION_IDX] == "1.0.0"


def test_ingest_events_batch_falls_back_to_blank_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr(db.time, "sleep", lambda _seconds: None)
    child_payload = load_capture("success_subagent_call")
    events = [build_event(child_payload)]
    fake_client = _EventuallyResolvingClient(blank_calls=99, subagent_type="test-researcher", agent_version="1.0.0")
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    assert fake_client.invocation_queries == 3

    event_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_events")
    assert event_rows[0][db._EVENT_AGENT_NAME_IDX] == ""
    assert event_rows[0][db._EVENT_AGENT_VERSION_IDX] == ""


def test_ingest_events_batch_success_backfills_skill_version_from_sibling_event(monkeypatch):
    # success_with_agent_and_skill predates the version marker convention,
    # so its own skill_version always resolves blank (see
    # test_active_skill_name_and_version_success_splits_skill_argument).
    # A second event in the same session, whose own message snapshot *does*
    # carry a version marker for the same skill, should backfill the
    # first event's skill_version rather than leaving it blank - this is
    # the "куча данных без версии" fix: a judge/gate-style call with a
    # reduced message list shouldn't lose version attribution when a
    # sibling call in the same batch/session already resolved it.
    no_version_payload = load_capture("success_with_agent_and_skill")
    no_version_payload["litellm_call_id"] = "no-version-call"

    with_version_payload = json.loads(json.dumps(no_version_payload))
    with_version_payload["litellm_call_id"] = "with-version-call"
    for message in with_version_payload["messages"]:
        content = message.get("content")
        if isinstance(content, str) and "- test-summarizer: Minimal test skill" in content:
            message["content"] = content.replace(
                "Use to verify the tracking stack end to end.\n- trace-debugging",
                "Use to verify the tracking stack end to end. v1.2.3\n- trace-debugging",
            )

    events = [build_event(no_version_payload), build_event(with_version_payload)]
    assert events[0]["usage_row"][db._USAGE_SKILL_VERSION_IDX] == ""
    assert events[1]["usage_row"][db._USAGE_SKILL_VERSION_IDX] == "1.2.3"

    fake_client = _FakeClient()
    monkeypatch.setattr(db, "get_client", lambda: fake_client)

    db.ingest_events_batch(events)

    usage_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_usage")
    call_id_idx = db._USAGE_CALL_ID_IDX
    versions = {row[call_id_idx]: row[db._USAGE_SKILL_VERSION_IDX] for row in usage_rows}
    assert versions == {"no-version-call": "1.2.3", "with-version-call": "1.2.3"}
