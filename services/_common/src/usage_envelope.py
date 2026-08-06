"""Versioned, source-neutral usage event contract for non-proxy clients."""

from datetime import datetime, timezone
from typing import Any


class UsageEnvelopeError(ValueError):
    pass


_SOURCES = {"claude_desktop", "claude_remote_control"}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "source",
    "event_id",
    "session_id",
    "timestamp",
    "model",
    "client_version",
    "entrypoint",
    "stop_reason",
    "tool_name",
    "agent_id",
    "usage",
    "identity",
}
_USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "cache_creation_1h_tokens",
    "cache_creation_5m_tokens",
}
_IDENTITY_FIELDS = {"user_id", "user_name", "group_id", "group_name"}
_UINT32_MAX = 2**32 - 1


def _string(payload: dict, field: str, *, required: bool = True) -> str:
    value = payload.get(field, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise UsageEnvelopeError(f"{field} must be a non-empty string")
    return value.strip()


def _tokens(usage: dict, field: str) -> int:
    value = usage.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _UINT32_MAX:
        raise UsageEnvelopeError(f"{field} must be an integer between 0 and {_UINT32_MAX}")
    return value


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise UsageEnvelopeError("timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageEnvelopeError("timestamp must be an ISO-8601 string") from exc
    if parsed.tzinfo is None:
        raise UsageEnvelopeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_identity(value: Any) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise UsageEnvelopeError("identity must be an object")
    unknown = set(value) - _IDENTITY_FIELDS
    if unknown:
        raise UsageEnvelopeError(f"unsupported identity field: {sorted(unknown)[0]}")
    return {
        "user_id": _string(value, "user_id", required=False) or "unknown-user",
        "user_name": _string(value, "user_name", required=False),
        "group_id": _string(value, "group_id", required=False),
        "group_name": _string(value, "group_name", required=False),
    }


def normalize_usage_envelope(payload: Any, identity: dict | None = None) -> dict:
    """Validates and returns only allowlisted metadata and usage fields."""
    if not isinstance(payload, dict):
        raise UsageEnvelopeError("usage envelope must be an object")
    unknown = set(payload) - _TOP_LEVEL_FIELDS
    if unknown:
        raise UsageEnvelopeError(f"unsupported field: {sorted(unknown)[0]}")
    if payload.get("schema_version") != 1:
        raise UsageEnvelopeError("schema_version must be 1")

    source = _string(payload, "source")
    if source not in _SOURCES:
        raise UsageEnvelopeError(f"unsupported source: {source}")

    raw_usage = payload.get("usage")
    if not isinstance(raw_usage, dict):
        raise UsageEnvelopeError("usage must be an object")
    unknown_usage = set(raw_usage) - _USAGE_FIELDS
    if unknown_usage:
        raise UsageEnvelopeError(f"unsupported usage field: {sorted(unknown_usage)[0]}")
    usage = {field: _tokens(raw_usage, field) for field in _USAGE_FIELDS}
    split_creation = usage["cache_creation_1h_tokens"] + usage["cache_creation_5m_tokens"]
    if split_creation > usage["cache_creation_tokens"]:
        raise UsageEnvelopeError("cache creation tier tokens exceed cache_creation_tokens")

    normalized = {
        "schema_version": 1,
        "source": source,
        "event_id": _string(payload, "event_id"),
        "session_id": _string(payload, "session_id"),
        "timestamp": _timestamp(payload.get("timestamp")),
        "model": _string(payload, "model"),
        "client_version": _string(payload, "client_version"),
        "entrypoint": _string(payload, "entrypoint"),
        "stop_reason": _string(payload, "stop_reason", required=False),
        "tool_name": _string(payload, "tool_name", required=False),
        "agent_id": _string(payload, "agent_id", required=False),
        "usage": usage,
        "identity": _normalize_identity(identity if identity is not None else payload.get("identity")),
    }
    return normalized
