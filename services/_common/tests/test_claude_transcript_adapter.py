from datetime import datetime, timezone

import pytest
from common import fastjson as json
from common import ingest_parsing as ip
from common.claude_transcript_adapter import build_claude_transcript_event

_NOW = datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)


def _envelope() -> dict:
    return {
        "schema_version": 1,
        "source": "claude_desktop",
        "event_id": "req_011CdmnK9GfzE8n6Kf8J3P6L",
        "session_id": "01c48175-883a-4aef-96d3-14dadc9de94c",
        "timestamp": "2026-08-06T14:06:52.000Z",
        "model": "claude-opus-5",
        "client_version": "2.1.221",
        "entrypoint": "claude-desktop",
        "stop_reason": "end_turn",
        "tool_name": "",
        "usage": {
            "input_tokens": 2,
            "output_tokens": 24,
            "cache_creation_tokens": 39_921,
            "cache_read_tokens": 33_693,
            "cache_creation_1h_tokens": 39_921,
            "cache_creation_5m_tokens": 0,
        },
        "identity": {
            "user_id": "user-1",
            "user_name": "alice",
            "group_id": "team-1",
        },
    }


def _pricing() -> dict:
    return {
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 25e-6,
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_creation_input_token_cost_above_1hr": 10e-6,
        "cache_read_input_token_cost": 0.5e-6,
    }


def test_build_claude_transcript_event_success_builds_source_neutral_rows():
    envelope = _envelope()

    event = build_claude_transcript_event(envelope, pricing=_pricing(), now=_NOW)

    event_values = dict(zip(ip._EVENT_COLUMNS, event["event_row"]))
    usage_values = dict(zip(ip._USAGE_COLUMNS, event["usage_row"]))
    raw_values = dict(zip(ip._SOURCE_COLUMNS, event["source_row"]))

    assert event_values["event_type"] == "claude_transcript_call"
    assert event_values["litellm_call_id"] == envelope["event_id"]
    assert event_values["session_id"] == envelope["session_id"]
    assert usage_values["model"] == "claude-opus-5"
    assert usage_values["input_tokens"] == 2
    assert usage_values["cache_creation_tokens"] == 39_921
    assert usage_values["cache_read_tokens"] == 33_693
    assert usage_values["cost"] == pytest.approx(0.416_666_5)
    assert usage_values["input_cost"] == pytest.approx(0.416_066_5)
    assert usage_values["output_cost"] == pytest.approx(0.000_6)
    assert event_values["client_product"] == "claude"
    assert event_values["client_surface"] == "desktop"
    assert event_values["ingest_path"] == "claude_transcript"
    assert usage_values["client_product"] == "claude"
    assert usage_values["client_surface"] == "desktop"
    assert usage_values["ingest_path"] == "claude_transcript"
    assert event["message_row"] is None
    assert event["user_agent"] == "claude-desktop/2.1.221"
    assert json.loads(raw_values["raw_payload_full"])["source"] == "claude_desktop"


def test_build_claude_transcript_event_success_names_the_group_when_resolved():
    envelope = _envelope()
    envelope["identity"]["group_name"] = "win-hub"

    event = build_claude_transcript_event(envelope, pricing=_pricing(), now=_NOW)

    group_values = dict(zip(ip._GROUP_COLUMNS, event["group_row"]))
    assert group_values["group_id"] == "team-1"
    assert group_values["group_name"] == "win-hub"


def test_build_claude_transcript_event_unsuccess_skips_group_row_without_a_name():
    # ai_gateway_groups is a ReplacingMergeTree on group_id, so an
    # empty-name row would overwrite whatever named the Team before.
    event = build_claude_transcript_event(_envelope(), pricing=_pricing(), now=_NOW)

    assert event["group_row"] is None


def test_build_claude_transcript_event_success_marks_missing_pricing_without_failing_usage():
    event = build_claude_transcript_event(_envelope(), pricing=None, now=_NOW)

    usage_values = dict(zip(ip._USAGE_COLUMNS, event["usage_row"]))
    event_values = dict(zip(ip._EVENT_COLUMNS, event["event_row"]))
    calculated = json.loads(event_values["calculated_payload"])

    assert usage_values["cost"] == 0
    assert calculated["cost_basis"] == "unavailable"
    assert calculated["content_omitted"] is True


def test_build_claude_transcript_event_success_attributes_remote_control_client():
    envelope = _envelope()
    envelope["source"] = "claude_remote_control"
    envelope["entrypoint"] = "cli"

    event = build_claude_transcript_event(envelope, pricing=_pricing(), now=_NOW)

    assert event["user_agent"] == "claude-remote-control/2.1.221"
    event_values = dict(zip(ip._EVENT_COLUMNS, event["event_row"]))
    assert event_values["client_surface"] == "remote_control"


def test_build_claude_transcript_event_rows_match_their_column_lists_exactly():
    # This adapter builds its rows positionally, independently of
    # ingest_parsing._usage_row - so a column added to one builder and not the
    # other shifts every later value. The dict(zip(...)) reads used by the
    # tests above hide that: zip stops at the shorter side, so a row missing
    # its last column still yields plausible-looking values for every field
    # before it, and only the insert fails at runtime.
    event = build_claude_transcript_event(_envelope(), pricing=_pricing(), now=_NOW)

    assert len(event["event_row"]) == len(ip._EVENT_COLUMNS)
    assert len(event["usage_row"]) == len(ip._USAGE_COLUMNS)
    assert len(event["source_row"]) == len(ip._SOURCE_COLUMNS)


def test_build_claude_transcript_event_success_marks_traffic_as_subscription_billed():
    event = build_claude_transcript_event(_envelope(), pricing=_pricing(), now=_NOW)

    usage_values = dict(zip(ip._USAGE_COLUMNS, event["usage_row"]))
    assert usage_values["billing_mode"] == "subscription"
