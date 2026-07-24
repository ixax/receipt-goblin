"""Unit tests for the pure (no-ClickHouse-access) functions in
clickhouse_ingest.py, exercised against real LiteLLM payloads captured by
the webhook (see webhook/tests/captures/*.json - copied from
webhook/captures/). DB-touching functions (get_client,
_agent_name_and_version_for_invocation, _insert_*, ingest_*) are out of
scope here - they require a live ClickHouse connection."""

import json
from datetime import datetime, timezone

from conftest import load_capture

from src import clickhouse_ingest as ci


# ---------------------------------------------------------------------------
# _to_dt
# ---------------------------------------------------------------------------

def test_to_dt_success_converts_epoch_seconds():
    dt = ci._to_dt(1750000000.5)
    assert dt == datetime.fromtimestamp(1750000000.5, tz=timezone.utc)


def test_to_dt_unsuccess_falls_back_to_now_for_falsy_input():
    before = datetime.now(timezone.utc)
    dt = ci._to_dt(None)
    after = datetime.now(timezone.utc)
    assert before <= dt <= after


# ---------------------------------------------------------------------------
# _flatten_content
# ---------------------------------------------------------------------------

def test_flatten_content_success_joins_text_and_placeholders():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash"},
        {"type": "tool_result", "content": "ignored"},
    ]
    assert ci._flatten_content(content) == "hello\n[tool_use:Bash]\n[tool_result]"


def test_flatten_content_unsuccess_non_list_non_str_returns_empty():
    assert ci._flatten_content(None) == ""
    assert ci._flatten_content(42) == ""


# ---------------------------------------------------------------------------
# _last_user_text
# ---------------------------------------------------------------------------

def test_last_user_text_success_returns_plain_prompt():
    payload = load_capture("success_plain")
    text = ci._last_user_text(payload["messages"])
    assert "test-summarizer skill" in text


def test_last_user_text_unsuccess_skips_pure_tool_result_continuation():
    payload = load_capture("success_with_failed_tool_reaction", index=1)
    # the trailing messages are all tool_use/tool_result continuations, no
    # fresh human text after the original prompt - walking back must not
    # return a bare tool_result placeholder.
    text = ci._last_user_text(payload["messages"])
    assert "[tool_result]" != text
    assert "test-summarizer skill" in text


# ---------------------------------------------------------------------------
# _active_command_name_and_version
# ---------------------------------------------------------------------------

def test_active_command_name_and_version_success_recovers_slash_command():
    payload = load_capture("success_with_command", index=1)
    # this capture predates the <command_version> marker convention, so the
    # command's body carries no marker - version comes back blank, same as
    # any command never edited since creation.
    assert ci._active_command_name_and_version(payload["messages"]) == ("mcp", "")


def test_active_command_name_and_version_success_recovers_version_marker():
    messages = [
        {"role": "user", "content": "<command-name>whatsup</command-name>\n<command_version>1.2.3</command_version>\n# whatsup\n..."},
    ]
    assert ci._active_command_name_and_version(messages) == ("whatsup", "1.2.3")


def test_active_command_name_and_version_unsuccess_freeform_prompt_returns_empty():
    payload = load_capture("success_with_command", index=0)
    assert ci._active_command_name_and_version(payload["messages"]) == ("", "")


# ---------------------------------------------------------------------------
# _failed_tool_call
# ---------------------------------------------------------------------------

def test_failed_tool_call_success_finds_paired_failing_tool_use():
    payload = load_capture("success_with_failed_tool_reaction", index=0)
    tool_name, args_json, error_text = ci._failed_tool_call(payload["messages"])
    assert tool_name == "Bash"
    assert "shuf" in args_json  # args come from the failing call (which used `shuf`), not a later one
    assert "command not found" in error_text


def test_failed_tool_call_unsuccess_no_trailing_error_returns_blank():
    payload = load_capture("success_plain")
    assert ci._failed_tool_call(payload["messages"]) == ("", "", "")


# ---------------------------------------------------------------------------
# _session_and_trace_id
# ---------------------------------------------------------------------------

def test_session_and_trace_id_success_prefers_claude_code_header():
    payload = load_capture("success_with_agent_and_skill")
    session_id, trace_id = ci._session_and_trace_id(payload)
    assert session_id == "ea219a89-9dd0-4f32-8c66-6f4d01e9788c"
    assert trace_id == payload["trace_id"]


def test_session_and_trace_id_unsuccess_falls_back_without_headers():
    payload = {"trace_id": "", "litellm_call_id": "call-123", "metadata": {}}
    session_id, trace_id = ci._session_and_trace_id(payload)
    assert session_id == "call-123"
    assert trace_id == "call-123"


# ---------------------------------------------------------------------------
# _split_name_version
# ---------------------------------------------------------------------------

def test_split_name_version_success_splits_on_last_v():
    assert ci._split_name_version("test-researcher_v1.0.0") == ("test-researcher", "1.0.0")


def test_split_name_version_unsuccess_no_version_suffix():
    assert ci._split_name_version("claude") == ("claude", "")


# ---------------------------------------------------------------------------
# _version_marker_for_name / _flatten_messages_text
# ---------------------------------------------------------------------------

def test_version_marker_for_name_success_finds_marker_in_listing_line():
    text = (
        "Available agent types for the Agent tool:\n"
        "- clickhouse-analyst: <agent_version>1.1.0</agent_version> Delegate target for...\n"
        "- general-purpose: General-purpose agent for researching...\n"
    )
    assert ci._version_marker_for_name(text, "clickhouse-analyst", "agent_version") == "1.1.0"


def test_version_marker_for_name_unsuccess_name_has_no_marker_returns_empty():
    text = "- general-purpose: General-purpose agent for researching...\n"
    assert ci._version_marker_for_name(text, "general-purpose", "agent_version") == ""
    assert ci._version_marker_for_name(text, "", "agent_version") == ""


# ---------------------------------------------------------------------------
# _user_id
# ---------------------------------------------------------------------------

def test_user_id_success_reads_real_user_id():
    payload = {"metadata": {"user_api_key_user_id": "u-123", "user_api_key_alias": "someone"}}
    assert ci._user_id(payload) == "u-123"


def test_user_id_falls_back_to_alias_when_no_real_id():
    payload = {"metadata": {"user_api_key_alias": "someone"}}
    assert ci._user_id(payload) == "someone"


def test_user_id_unsuccess_falls_back_to_unknown():
    assert ci._user_id({}) == "unknown-user"


def test_user_name_prefers_alias_over_real_id():
    payload = {"metadata": {"user_api_key_user_id": "u-123", "user_api_key_alias": "someone"}}
    assert ci._user_name(payload) == "someone"


def test_user_name_falls_back_to_real_id_when_no_alias():
    payload = {"metadata": {"user_api_key_user_id": "u-123"}}
    assert ci._user_name(payload) == "u-123"


def test_user_name_unsuccess_falls_back_to_unknown():
    assert ci._user_name({}) == "unknown-user"


# ---------------------------------------------------------------------------
# _group_id / _group_alias
# ---------------------------------------------------------------------------

def test_group_id_success_reads_stable_team_id():
    payload = {"metadata": {"user_api_key_team_id": "cc4f422e-f253-40b2-9dcb-749f9d5e7976", "user_api_key_team_alias": "team-a"}}
    assert ci._group_id(payload) == "cc4f422e-f253-40b2-9dcb-749f9d5e7976"


def test_group_id_unsuccess_no_team_falls_back_to_empty():
    payload = {"metadata": {"user_api_key_alias": "someone"}}
    assert ci._group_id(payload) == ""


def test_group_alias_success_reads_team_alias():
    payload = {"metadata": {"user_api_key_team_alias": "team-a"}}
    assert ci._group_alias(payload) == "team-a"


def test_group_alias_unsuccess_no_team_falls_back_to_empty():
    payload = {"metadata": {"user_api_key_alias": "someone"}}
    assert ci._group_alias(payload) == ""


# ---------------------------------------------------------------------------
# _agent_invocations_from_messages / _agent_id_from_tool_result
# ---------------------------------------------------------------------------

def test_agent_invocations_from_messages_success_finds_spawned_subagent():
    payload = load_capture("success_with_agent_and_skill")
    invocations = ci._agent_invocations_from_messages(payload["messages"])
    # this capture predates the <agent_version> marker convention, so the
    # listing carries no marker for this name - falls back to splitting the
    # "_v<version>" suffix baked into subagent_type itself (the old
    # convention, via _split_name_version).
    assert invocations == [("aac9d05f148e9ae4a", "test-researcher", "1.0.0", "Summarize Makefile contents")]


def test_agent_invocations_from_messages_success_recovers_version_marker():
    messages = [
        {
            "role": "system",
            "content": (
                "Available agent types for the Agent tool:\n"
                "- clickhouse-analyst: <agent_version>1.1.0</agent_version> Delegate target for...\n"
            ),
        },
        {"role": "assistant", "content": [{"type": "tool_use", "name": "Agent", "id": "toolu_1", "input": {"subagent_type": "clickhouse-analyst", "description": "look up cost"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "agentId: deadbeef"}]},
    ]
    invocations = ci._agent_invocations_from_messages(messages)
    assert invocations == [("deadbeef", "clickhouse-analyst", "1.1.0", "look up cost")]


def test_agent_invocations_from_messages_unsuccess_no_agent_calls_returns_empty():
    payload = load_capture("success_plain")
    assert ci._agent_invocations_from_messages(payload["messages"]) == []


def test_agent_id_from_tool_result_unsuccess_mismatched_tool_use_id():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "name": "Agent", "id": "toolu_1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_other", "content": "agentId: deadbeef"}]},
    ]
    assert ci._agent_id_from_tool_result(messages, 0, "toolu_1") == ""


# ---------------------------------------------------------------------------
# _response_tool_calls / _first_tool_call_name / _skill_name_and_version
# ---------------------------------------------------------------------------

def test_response_tool_calls_success_parses_function_arguments():
    payload = load_capture("success_with_agent_and_skill")
    calls = ci._response_tool_calls(payload)
    assert calls == [("Skill", {"skill": "test-summarizer", "args": "Summarize /Users/ixax/PycharmProjects/claude-wrapper/README.md"})]


def test_response_tool_calls_unsuccess_plain_text_reply_returns_empty():
    payload = load_capture("success_with_command", index=0)
    assert ci._response_tool_calls(payload) == []


def test_first_tool_call_name_success_returns_first_call():
    payload = load_capture("success_with_agent_and_skill")
    assert ci._first_tool_call_name(payload) == "Skill"


def test_first_tool_call_name_unsuccess_plain_text_reply_returns_empty():
    payload = load_capture("success_with_command", index=0)
    assert ci._first_tool_call_name(payload) == ""


def test_skill_name_and_version_success_splits_skill_argument():
    payload = load_capture("success_with_agent_and_skill")
    # this capture predates the <skill_version> marker convention, so the
    # listing carries no marker for this name - version comes back blank.
    assert ci._skill_name_and_version(payload) == ("test-summarizer", "")


def test_skill_name_and_version_success_recovers_version_marker():
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "available skills for the Skill tool:\n"
                    "- test-linter: <skill_version>2.0.0</skill_version> Minimal test skill...\n"
                ),
            },
        ],
        "response": {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "Skill", "arguments": json.dumps({"skill": "test-linter", "args": "check foo.py"})}}
        ]}}]},
    }
    assert ci._skill_name_and_version(payload) == ("test-linter", "2.0.0")


def test_skill_name_and_version_unsuccess_no_skill_call():
    payload = load_capture("success_plain")
    assert ci._skill_name_and_version(payload) == ("", "")


# ---------------------------------------------------------------------------
# _agent_invocation_id
# ---------------------------------------------------------------------------

def test_agent_invocation_id_success_reads_header():
    payload = load_capture("success_subagent_call")
    assert ci._agent_invocation_id(payload) == "aac9d05f148e9ae4a"


def test_agent_invocation_id_unsuccess_missing_header_returns_empty():
    payload = load_capture("success_plain")
    assert ci._agent_invocation_id(payload) == ""


# ---------------------------------------------------------------------------
# _agent_invocation_rows
# ---------------------------------------------------------------------------

def test_agent_invocation_rows_success_builds_one_row_per_spawn():
    payload = load_capture("success_with_agent_and_skill")
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    rows = ci._agent_invocation_rows("session-1", payload["messages"], now=now)
    assert rows == [["aac9d05f148e9ae4a", "session-1", "test-researcher", "1.0.0", "Summarize Makefile contents", now]]


def test_agent_invocation_rows_unsuccess_no_spawns_returns_empty_list():
    payload = load_capture("success_plain")
    assert ci._agent_invocation_rows("session-1", payload["messages"]) == []


# ---------------------------------------------------------------------------
# _event_row
# ---------------------------------------------------------------------------

def test_event_row_success_reports_status_and_latency():
    payload = load_capture("success_plain")
    row = ci._event_row(payload, "session-1", "trace-1", "", "", "", "", "", "", "")
    columns = ci._EVENT_COLUMNS
    values = dict(zip(columns, row))
    assert values["status"] == "success"
    assert values["session_id"] == "session-1"
    assert values["latency_ms"] is not None and values["latency_ms"] >= 0
    assert values["calculated_type"] == "title_gen"  # prompt starts with "<session>"
    assert values["group_id"] == "206ec527-2402-4c8b-b5b5-8bd65b8bca0f"  # capture's user_api_key_team_id


def test_event_row_unsuccess_failure_payload_has_no_tool_name_or_latency():
    payload = load_capture("failure")
    row = ci._event_row(payload, "session-1", "trace-1", "", "", "", "", "", "", "")
    values = dict(zip(ci._EVENT_COLUMNS, row))
    assert values["status"] == "failure"
    assert values["tool_name"] == ""


# ---------------------------------------------------------------------------
# _usage_row
# ---------------------------------------------------------------------------

def test_usage_row_success_extracts_token_counts():
    payload = load_capture("success_plain")
    row = ci._usage_row(payload, "session-1", "trace-1", "", "", "", "", "", "", "")
    assert row is not None
    values = dict(zip(ci._USAGE_COLUMNS, row))
    assert values["input_tokens"] == 723
    assert values["output_tokens"] == 16
    assert values["group_id"] == "206ec527-2402-4c8b-b5b5-8bd65b8bca0f"


def test_usage_row_unsuccess_no_billable_tokens_returns_none():
    payload = load_capture("failure")
    assert ci._usage_row(payload, "session-1", "trace-1", "", "", "", "", "", "", "") is None


# ---------------------------------------------------------------------------
# _message_row
# ---------------------------------------------------------------------------

def test_message_row_success_captures_prompt_and_response_text():
    payload = load_capture("success_plain")
    row = ci._message_row(payload, "session-1", "trace-1", "", "", "", "", "", "", "")
    assert row is not None
    values = dict(zip(ci._MESSAGE_COLUMNS, row))
    assert "test-summarizer skill" in values["prompt_text"]
    assert values["response_text"]
    assert values["group_id"] == "206ec527-2402-4c8b-b5b5-8bd65b8bca0f"


def test_message_row_unsuccess_no_prompt_or_response_text_returns_none():
    payload = {"messages": [], "response": {"choices": []}}
    assert ci._message_row(payload, "session-1", "trace-1", "", "", "", "", "", "", "") is None


# ---------------------------------------------------------------------------
# build_event - the queue-facing, DB-free half of ingestion (see
# queue_client.enqueue). source_row deliberately DOES carry the full
# payload (messages included) - that's what event_sources is for; only the
# per-table rows are stripped down.
# ---------------------------------------------------------------------------

def test_build_event_success_returns_json_safe_dict_with_source_row():
    payload = load_capture("success_plain")
    event = ci.build_event(payload)

    encoded = json.dumps(event)  # must not raise - safe to XADD onto Redis
    assert event["source_row"] is not None
    assert event["event_row"] is not None
    assert event["usage_row"] is not None
    assert event["message_row"] is not None
    # timestamps are serialized to ISO strings, not raw datetime objects,
    # so the dict is safe to json.dumps() straight onto the Redis stream.
    assert isinstance(event["event_row"][ci._EVENT_TIMESTAMP_IDX], str)
    assert isinstance(event["source_row"][ci._SOURCE_INGESTED_AT_IDX], str)
    # the full original payload really is in there, untouched
    source_payload = json.loads(event["source_row"][ci._SOURCE_COLUMNS.index("raw_payload_full")])
    assert "messages" in source_payload


def test_build_event_unsuccess_failure_payload_has_no_usage_or_message_row():
    payload = load_capture("failure")
    event = ci.build_event(payload)

    assert event["event_row"] is not None
    assert event["usage_row"] is None
    assert event["message_row"] is None


# ---------------------------------------------------------------------------
# ingest_events_batch - runs in webhook-worker, takes build_event() outputs
# read back off Redis and inserts them with one client.insert() per table.
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))

    def query(self, *args, **kwargs):
        class _Result:
            result_rows = []
        return _Result()


def test_ingest_events_batch_success_issues_one_insert_per_table(monkeypatch):
    events = [
        ci.build_event(load_capture("success_plain")),
        ci.build_event(load_capture("success_with_command")),
    ]
    fake_client = _FakeClient()
    monkeypatch.setattr(ci, "get_client", lambda: fake_client)

    ci.ingest_events_batch(events)

    tables = [table for table, _rows, _cols in fake_client.inserts]
    assert tables.count("event_sources") == 1
    assert tables.count("agent_events") == 1
    assert tables.count("agent_usage") == 1
    assert tables.count("agent_messages") == 1

    event_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "agent_events")
    assert len(event_rows) == 2

    source_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "event_sources")
    assert len(source_rows) == 2


def test_ingest_events_batch_success_dedups_dimension_rows_by_id(monkeypatch):
    # Both captures share the same user/team (see captures/*.json), so a
    # batch of two events should still only insert one ai_gateway_users and
    # one ai_gateway_groups row - not one per event.
    events = [
        ci.build_event(load_capture("success_plain")),
        ci.build_event(load_capture("success_with_command")),
    ]
    fake_client = _FakeClient()
    monkeypatch.setattr(ci, "get_client", lambda: fake_client)

    ci.ingest_events_batch(events)

    user_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "ai_gateway_users")
    assert len(user_rows) == 1

    group_rows = next(rows for table, rows, _cols in fake_client.inserts if table == "ai_gateway_groups")
    assert len(group_rows) == 1


def test_ingest_events_batch_unsuccess_empty_list_skips_client_entirely(monkeypatch):
    monkeypatch.setattr(ci, "get_client", lambda: (_ for _ in ()).throw(AssertionError("get_client should not be called")))
    ci.ingest_events_batch([])
