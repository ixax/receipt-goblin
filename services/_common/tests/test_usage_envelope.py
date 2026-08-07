import pytest
from common.usage_envelope import UsageEnvelopeError, normalize_usage_envelope


def _payload() -> dict:
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
    }


def test_normalize_usage_envelope_success_adds_server_identity():
    payload = _payload()

    normalized = normalize_usage_envelope(
        payload,
        identity={
            "user_id": "user-1",
            "user_name": "ficac",
            "group_id": "team-1",
            "group_name": "win-hub",
        },
    )

    assert normalized["event_id"] == payload["event_id"]
    assert normalized["usage"]["cache_creation_tokens"] == 39_921
    assert normalized["identity"] == {
        "user_id": "user-1",
        "user_name": "ficac",
        "group_id": "team-1",
        "group_name": "win-hub",
    }


def test_normalize_usage_envelope_success_defaults_group_name_when_unresolved():
    normalized = normalize_usage_envelope(
        _payload(),
        identity={"user_id": "user-1", "group_id": "team-1"},
    )

    assert normalized["identity"]["group_name"] == ""


def test_normalize_usage_envelope_unsuccess_rejects_content_fields():
    payload = _payload()
    payload["messages"] = [{"role": "user", "content": "secret"}]

    with pytest.raises(UsageEnvelopeError, match="unsupported field"):
        normalize_usage_envelope(payload)


def test_normalize_usage_envelope_unsuccess_rejects_negative_tokens():
    payload = _payload()
    payload["usage"]["output_tokens"] = -1

    with pytest.raises(UsageEnvelopeError, match="output_tokens"):
        normalize_usage_envelope(payload)


def test_normalize_usage_envelope_unsuccess_rejects_unknown_version():
    payload = _payload()
    payload["schema_version"] = 2

    with pytest.raises(UsageEnvelopeError, match="schema_version"):
        normalize_usage_envelope(payload)


def test_normalize_success_treats_explicit_null_optional_field_as_absent():
    payload = _payload()
    payload["stop_reason"] = None

    normalized = normalize_usage_envelope(payload)

    assert normalized["stop_reason"] == ""
