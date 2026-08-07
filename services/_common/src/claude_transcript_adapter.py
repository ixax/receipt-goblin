"""Adapter from UsageEnvelopeV1 to the source-neutral ingest row bundle."""

from datetime import datetime, timezone
from typing import Optional

from common import fastjson as json
from common.client_attribution import from_claude_envelope
from common.ingest_parsing import (
    _EVENT_INGESTED_AT_IDX,
    _EVENT_TIMESTAMP_IDX,
    _GROUP_UPDATED_AT_IDX,
    _SOURCE_INGESTED_AT_IDX,
    _USAGE_INGESTED_AT_IDX,
    _USAGE_TIMESTAMP_IDX,
    _USER_UPDATED_AT_IDX,
    _billing_mode_for_model,
    _serialize_row,
    _serialize_row_multi,
)
from common.usage_envelope import normalize_usage_envelope


def _api_equivalent_cost(usage: dict, pricing: Optional[dict]) -> tuple[float, float, float, str]:
    if not pricing:
        return 0.0, 0.0, 0.0, "unavailable"

    input_rate = pricing.get("input_cost_per_token")
    output_rate = pricing.get("output_cost_per_token")
    cache_write_rate = pricing.get("cache_creation_input_token_cost")
    cache_write_1h_rate = pricing.get("cache_creation_input_token_cost_above_1hr")
    cache_read_rate = pricing.get("cache_read_input_token_cost")
    if not all(isinstance(rate, (int, float)) for rate in (input_rate, output_rate)):
        return 0.0, 0.0, 0.0, "unavailable"

    cache_write_rate = cache_write_rate if isinstance(cache_write_rate, (int, float)) else input_rate
    cache_write_1h_rate = (
        cache_write_1h_rate if isinstance(cache_write_1h_rate, (int, float)) else cache_write_rate
    )
    cache_read_rate = cache_read_rate if isinstance(cache_read_rate, (int, float)) else input_rate

    creation_1h = usage["cache_creation_1h_tokens"]
    creation_5m = usage["cache_creation_5m_tokens"]
    creation_unclassified = max(usage["cache_creation_tokens"] - creation_1h - creation_5m, 0)
    input_cost = (
        usage["input_tokens"] * input_rate
        + creation_1h * cache_write_1h_rate
        + (creation_5m + creation_unclassified) * cache_write_rate
        + usage["cache_read_tokens"] * cache_read_rate
    )
    output_cost = usage["output_tokens"] * output_rate
    return input_cost + output_cost, input_cost, output_cost, "litellm_cost_map"


def _client_user_agent(envelope: dict) -> str:
    client = "claude-desktop" if envelope["source"] == "claude_desktop" else "claude-remote-control"
    return f"{client}/{envelope['client_version']}"


def build_claude_transcript_event(
    payload: dict,
    *,
    pricing: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> dict:
    envelope = normalize_usage_envelope(payload)
    now = now or datetime.now(timezone.utc)
    timestamp = datetime.fromisoformat(envelope["timestamp"].replace("Z", "+00:00"))
    usage = envelope["usage"]
    identity = envelope["identity"]
    call_id = envelope["event_id"]
    session_id = envelope["session_id"]
    tool_name = envelope["tool_name"]
    total_cost, input_cost, output_cost, cost_basis = _api_equivalent_cost(usage, pricing)
    calculated_type = "tool_call" if tool_name else "llm_answer"
    if tool_name == "Agent":
        calculated_type = "agent_spawn"
    elif tool_name == "Skill":
        calculated_type = "skill_call"
    elif tool_name == "AskUserQuestion":
        calculated_type = "ask_user_question"
    calculated_payload = {
        "source": envelope["source"],
        "entrypoint": envelope["entrypoint"],
        "content_omitted": True,
        "cost_basis": cost_basis,
    }
    attribution = from_claude_envelope(envelope)

    event_row = [
        timestamp,
        identity["user_id"],
        identity["group_id"],
        "",
        session_id,
        session_id,
        0,
        "claude_transcript_call",
        tool_name,
        "",
        "",
        "",
        "",
        "",
        envelope["agent_id"],
        "success",
        None,
        "",
        "",
        "",
        call_id,
        0,
        attribution.product,
        attribution.surface,
        attribution.ingest_path,
        calculated_type,
        json.dumps(calculated_payload).decode(),
        now,
    ]
    usage_row = [
        timestamp,
        identity["user_id"],
        identity["group_id"],
        "",
        session_id,
        session_id,
        0,
        envelope["model"],
        "",
        "",
        "",
        "",
        "",
        envelope["agent_id"],
        tool_name if tool_name.startswith("mcp__") else "",
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cache_creation_tokens"],
        usage["cache_read_tokens"],
        envelope["stop_reason"],
        usage["cache_creation_1h_tokens"],
        usage["cache_creation_5m_tokens"],
        total_cost,
        input_cost,
        output_cost,
        1 if usage["cache_read_tokens"] else 0,
        0,
        call_id,
        0,
        attribution.product,
        attribution.surface,
        attribution.ingest_path,
        "claude",
        _billing_mode_for_model(envelope["model"]),
        now,
    ]
    source_row = [call_id, session_id, now, json.dumps(envelope).decode()]
    # Only written when the Team's name actually resolved: ai_gateway_groups is
    # a ReplacingMergeTree keyed on group_id, so a row with an empty name would
    # overwrite a good one, and the dashboard's group variable coalesces on
    # NULL (no row) - an empty-string row labels the Team blank instead of
    # falling back to its id.
    group_name = identity.get("group_name") or ""
    group_row = [identity["group_id"], group_name, now] if identity["group_id"] and group_name else None
    user_row = [identity["user_id"], identity["group_id"], identity["user_name"], now]

    return {
        "agent_invocation_id": envelope["agent_id"],
        "user_agent": _client_user_agent(envelope),
        "invocation_rows": [],
        "source_row": _serialize_row(source_row, _SOURCE_INGESTED_AT_IDX),
        "event_row": _serialize_row_multi(event_row, _EVENT_TIMESTAMP_IDX, _EVENT_INGESTED_AT_IDX),
        "usage_row": _serialize_row_multi(usage_row, _USAGE_TIMESTAMP_IDX, _USAGE_INGESTED_AT_IDX),
        "message_row": None,
        "group_row": _serialize_row(group_row, _GROUP_UPDATED_AT_IDX),
        "user_row": _serialize_row(user_row, _USER_UPDATED_AT_IDX),
    }
