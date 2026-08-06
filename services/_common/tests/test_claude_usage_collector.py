import importlib.util
import json
import urllib.error
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "claude_usage_collector.py"
_SPEC = importlib.util.spec_from_file_location("claude_usage_collector", _SCRIPT)
collector = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(collector)


def _assistant_row(entrypoint: str = "claude-desktop") -> dict:
    return {
        "type": "assistant",
        "requestId": "req_123",
        "uuid": "assistant-uuid",
        "timestamp": "2026-08-06T14:06:52.000Z",
        "entrypoint": entrypoint,
        "sessionId": "session-1",
        "version": "2.1.221",
        "message": {
            "model": "claude-opus-5",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "private answer"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "secret.txt"}},
            ],
            "usage": {
                "input_tokens": 2,
                "output_tokens": 24,
                "cache_creation_input_tokens": 39_921,
                "cache_read_input_tokens": 33_693,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 39_921,
                    "ephemeral_5m_input_tokens": 0,
                },
            },
        },
    }


def _write_transcript(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_extract_usage_envelopes_success_tracks_desktop_without_content(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [_assistant_row()])

    envelopes = list(collector.extract_usage_envelopes(transcript))

    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope["source"] == "claude_desktop"
    assert envelope["tool_name"] == "Read"
    assert envelope["usage"]["cache_creation_tokens"] == 39_921
    serialized = json.dumps(envelope)
    assert "private answer" not in serialized
    assert "secret.txt" not in serialized


def test_extract_usage_envelopes_success_ignores_proxied_cli(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [_assistant_row(entrypoint="cli")])

    assert list(collector.extract_usage_envelopes(transcript)) == []


def test_extract_usage_envelopes_success_tracks_direct_cli_as_remote_control(tmp_path):
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [_assistant_row(entrypoint="cli")])

    envelopes = list(collector.extract_usage_envelopes(transcript, tracking_mode="direct"))

    assert envelopes[0]["source"] == "claude_remote_control"


def test_outbox_success_deduplicates_by_request_id(tmp_path):
    outbox = collector.Outbox(tmp_path / "outbox.sqlite3")
    envelope = {
        "schema_version": 1,
        "source": "claude_desktop",
        "event_id": "req_123",
    }

    outbox.put(envelope)
    outbox.put(envelope)

    assert outbox.pending_count() == 1


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _queued_envelope(event_id: str) -> dict:
    envelope = _assistant_row()
    envelope["requestId"] = event_id
    return collector._envelope_from_row(envelope, tracking_mode=None)


def test_flush_outbox_success_batches_and_drains_more_than_one_page(monkeypatch, tmp_path):
    outbox = collector.Outbox(tmp_path / "outbox.sqlite3")
    outbox.put_batch(_queued_envelope(f"req-{index}") for index in range(120))
    batches = []

    def urlopen(request, timeout):
        batches.append(json.loads(request.data))
        return _Response()

    monkeypatch.setattr(collector.urllib.request, "urlopen", urlopen)

    sent = collector.flush_outbox(
        outbox,
        api_url="http://localhost:8010",
        virtual_key="test-key",
        time_budget_seconds=10,
        batch_size=50,
        max_events_per_second=0,
    )

    assert sent == 120
    assert [len(batch) for batch in batches] == [50, 50, 20]
    assert outbox.pending_count() == 0


def test_flush_outbox_unsuccess_keeps_batch_when_server_is_unavailable(monkeypatch, tmp_path):
    outbox = collector.Outbox(tmp_path / "outbox.sqlite3")
    outbox.put_batch(_queued_envelope(f"req-{index}") for index in range(3))

    def urlopen(request, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(collector.urllib.request, "urlopen", urlopen)

    sent = collector.flush_outbox(
        outbox,
        api_url="http://localhost:8010",
        virtual_key="test-key",
        time_budget_seconds=10,
        batch_size=50,
        max_events_per_second=0,
    )

    assert sent == 0
    assert outbox.pending_count() == 3
