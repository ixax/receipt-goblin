"""Unit tests for src/loadtest.py's corpus-loading/filtering logic
(_is_success_capture, load_session_corpus). Uses synthetic tmp_path
fixtures, not the real .capture/ runtime directory - these tests must stay
deterministic and independent of whatever traffic happens to be captured on
a given machine at test time."""

import json

from src import loadtest


# ---------------------------------------------------------------------------
# _is_success_capture
# ---------------------------------------------------------------------------

def test_is_success_capture_success_reads_status_from_json(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"id": "abc", "call_type": "acompletion", "status": "success"}))
    assert loadtest._is_success_capture(str(path)) is True


def test_is_success_capture_unsuccess_status_failure_returns_false(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"id": "abc", "call_type": "acompletion", "status": "failure"}))
    assert loadtest._is_success_capture(str(path)) is False


def test_is_success_capture_unsuccess_missing_status_field_returns_false(tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"id": "abc", "call_type": "acompletion"}))
    assert loadtest._is_success_capture(str(path)) is False


def test_is_success_capture_unsuccess_nonexistent_file_returns_false(tmp_path):
    assert loadtest._is_success_capture(str(tmp_path / "missing.json")) is False


def test_is_success_capture_success_ignores_malformed_content_after_prefix(tmp_path):
    # status sits inside the first _STATUS_PREFIX_BYTES; everything after it
    # is deliberately invalid JSON/UTF-8 - proves the check never falls back
    # to a full json.load of the file (see the function's docstring on why
    # that matters for a multi-GB corpus).
    path = tmp_path / "event.json"
    prefix = b'{"id": "abc", "call_type": "acompletion", "status": "success", "junk": '
    path.write_bytes(prefix + b"\xff" * 2000)
    assert loadtest._is_success_capture(str(path)) is True


def test_is_success_capture_unsuccess_status_failure_ignores_malformed_content_after_prefix(tmp_path):
    path = tmp_path / "event.json"
    prefix = b'{"id": "abc", "call_type": "acompletion", "status": "failure", "junk": '
    path.write_bytes(prefix + b"\xff" * 2000)
    assert loadtest._is_success_capture(str(path)) is False


def test_is_success_capture_unsuccess_status_beyond_prefix_window_returns_false(tmp_path):
    # A pathological file where status sits past _STATUS_PREFIX_BYTES - real
    # captures never look like this (server.py always writes status within
    # the first ~150 bytes), but the function must fail closed (treat as
    # not-success/skip), not raise or scan the whole file.
    path = tmp_path / "event.json"
    padding = '"pad": "' + ("x" * loadtest._STATUS_PREFIX_BYTES) + '", '
    path.write_text('{' + padding + '"status": "success"}')
    assert loadtest._is_success_capture(str(path)) is False


# ---------------------------------------------------------------------------
# load_session_corpus
# ---------------------------------------------------------------------------

def _write_event(session_dir, filename, status):
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / filename).write_text(json.dumps({"id": filename, "status": status}))


def test_load_session_corpus_success_keeps_only_success_events(tmp_path):
    session_dir = tmp_path / "session-a"
    _write_event(session_dir, "20260101T000000000000-a.json", "success")
    _write_event(session_dir, "20260101T000005000000-b.json", "failure")
    _write_event(session_dir, "20260101T000020000000-c.json", "success")

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    assert len(corpus) == 1
    trace = corpus[0]
    assert [p.endswith(("a.json", "c.json")) for p in trace.paths] == [True, True]
    assert len(trace.paths) == 2


def test_load_session_corpus_success_computes_gap_across_dropped_failure(tmp_path):
    # Gap between the two *kept* success events must span the full 20s
    # (including the dropped failure in between), not the original 5s
    # between the first success and the failure that got filtered out.
    session_dir = tmp_path / "session-a"
    _write_event(session_dir, "20260101T000000000000-a.json", "success")
    _write_event(session_dir, "20260101T000005000000-b.json", "failure")
    _write_event(session_dir, "20260101T000020000000-c.json", "success")

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    trace = corpus[0]
    assert trace.gaps == [0.0, 20.0]


def test_load_session_corpus_unsuccess_session_with_no_success_events_is_dropped(tmp_path):
    session_dir = tmp_path / "session-all-failed"
    _write_event(session_dir, "20260101T000000000000-a.json", "failure")
    _write_event(session_dir, "20260101T000010000000-b.json", "failure")

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    assert corpus == []


def test_load_session_corpus_success_clamps_gap_to_max_gap_seconds(tmp_path):
    session_dir = tmp_path / "session-a"
    _write_event(session_dir, "20260101T000000000000-a.json", "success")
    _write_event(session_dir, "20260101T003000000000-b.json", "success")  # 30 min later

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    trace = corpus[0]
    assert trace.gaps == [0.0, 60.0]


def test_load_session_corpus_unsuccess_empty_capture_dir_returns_empty_list(tmp_path):
    assert loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60) == []
