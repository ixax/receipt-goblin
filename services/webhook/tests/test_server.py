import asyncio

import pytest
from fastapi import HTTPException
from src import server


class _Request:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _usage_event() -> dict:
    return {
        "schema_version": 1,
        "source": "claude_desktop",
        "event_id": "req-1",
        "session_id": "session-1",
        "timestamp": "2026-08-06T14:06:52Z",
        "model": "claude-opus-5",
        "client_version": "2.1.221",
        "entrypoint": "claude-desktop",
        "stop_reason": "end_turn",
        "tool_name": "",
        "agent_id": "",
        "usage": {
            "input_tokens": 2,
            "output_tokens": 24,
            "cache_creation_tokens": 39_921,
            "cache_read_tokens": 33_693,
            "cache_creation_1h_tokens": 39_921,
            "cache_creation_5m_tokens": 0,
        },
    }


def test_receive_usage_event_success_enriches_identity_and_queues(monkeypatch):
    queued = []

    async def enqueue(payloads):
        queued.extend(payloads)

    monkeypatch.setattr(server, "enqueue_usage_events", enqueue)
    monkeypatch.setattr(server, "_team_alias", lambda team_id: "win-hub" if team_id else "")

    response = asyncio.run(
        server.receive_usage_event(
            _Request(_usage_event()),
            {"user_id": "user-1", "key_alias": "ficac", "team_id": "team-1"},
        )
    )

    assert response == {"status": "queued"}
    assert queued[0]["identity"] == {
        "user_id": "user-1",
        "user_name": "ficac",
        "group_id": "team-1",
        "group_name": "win-hub",
    }


def test_receive_usage_event_success_validates_and_queues_batch(monkeypatch):
    queued = []

    async def enqueue(payloads):
        queued.extend(payloads)

    monkeypatch.setattr(server, "enqueue_usage_events", enqueue, raising=False)
    monkeypatch.setattr(server, "_team_alias", lambda team_id: "win-hub" if team_id else "")

    response = asyncio.run(
        server.receive_usage_event(
            _Request([_usage_event(), {**_usage_event(), "event_id": "req-2"}]),
            {"user_id": "user-1", "key_alias": "ficac", "team_id": "team-1"},
        )
    )

    assert response == {"status": "queued"}
    assert [payload["event_id"] for payload in queued] == ["req-1", "req-2"]


def test_receive_usage_event_unsuccess_returns_503_when_queue_rejects_batch(monkeypatch):
    async def enqueue(payloads):
        raise ConnectionError("redis down")

    monkeypatch.setattr(server, "enqueue_usage_events", enqueue, raising=False)
    monkeypatch.setattr(server, "_team_alias", lambda team_id: "win-hub" if team_id else "")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            server.receive_usage_event(
                _Request([_usage_event()]),
                {"user_id": "user-1", "key_alias": "ficac", "team_id": "team-1"},
            )
        )

    assert exc_info.value.status_code == 503
