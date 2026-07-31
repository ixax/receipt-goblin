"""Unit tests for src/loadtest.py's corpus-loading logic
(load_session_corpus). Uses synthetic tmp_path fixtures, not the real
FIXTURES_DIR runtime directory - these tests must stay deterministic and
independent of whatever fixtures happen to be generated on a given machine
at test time."""

import json

from src import loadtest


def _write_event(session_dir, filename, status="success"):
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / filename).write_text(json.dumps({"id": filename, "status": status}))


def test_load_session_corpus_success_indexes_all_files_in_a_session(tmp_path):
    session_dir = tmp_path / "session-a"
    _write_event(session_dir, "20260101T000000000000-a.json")
    _write_event(session_dir, "20260101T000005000000-b.json")
    _write_event(session_dir, "20260101T000020000000-c.json")

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    assert len(corpus) == 1
    trace = corpus[0]
    assert len(trace.paths) == 3
    assert [p.endswith(("a.json", "b.json", "c.json")) for p in trace.paths] == [True, True, True]


def test_load_session_corpus_success_computes_real_gaps_between_events(tmp_path):
    session_dir = tmp_path / "session-a"
    _write_event(session_dir, "20260101T000000000000-a.json")
    _write_event(session_dir, "20260101T000005000000-b.json")
    _write_event(session_dir, "20260101T000020000000-c.json")

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    trace = corpus[0]
    assert trace.gaps == [0.0, 5.0, 15.0]


def test_load_session_corpus_unsuccess_empty_session_dir_is_dropped(tmp_path):
    session_dir = tmp_path / "session-empty"
    session_dir.mkdir(parents=True, exist_ok=True)

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    assert corpus == []


def test_load_session_corpus_success_clamps_gap_to_max_gap_seconds(tmp_path):
    session_dir = tmp_path / "session-a"
    _write_event(session_dir, "20260101T000000000000-a.json")
    _write_event(session_dir, "20260101T003000000000-b.json")  # 30 min later

    corpus = loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60)

    trace = corpus[0]
    assert trace.gaps == [0.0, 60.0]


def test_load_session_corpus_unsuccess_empty_fixtures_dir_returns_empty_list(tmp_path):
    assert loadtest.load_session_corpus(str(tmp_path), max_gap_seconds=60) == []
