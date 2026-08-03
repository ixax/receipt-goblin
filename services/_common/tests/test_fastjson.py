"""Unit tests for src/fastjson.py - the orjson-backed drop-in-ish
replacement for stdlib json used on the hot/full-payload paths (see
AGENTS.md "Why a queue in front of ClickHouse"). Covers the specific
places it deliberately diverges from stdlib json's contract (dumps/dump
return/write bytes, not str) as well as where it must behave identically
(default= callback, JSONDecodeError being a ValueError subclass so
existing `except (TypeError, ValueError):` call sites keep working)."""

import io

import pytest
from common import fastjson

# ---------------------------------------------------------------------------
# loads / load
# ---------------------------------------------------------------------------

def test_loads_success_parses_bytes():
    assert fastjson.loads(b'{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_loads_success_parses_str():
    assert fastjson.loads('{"a": 1}') == {"a": 1}


def test_loads_unsuccess_invalid_json_raises_value_error():
    # Callers (worker.py, webhook/worker's queue.py) catch (TypeError, ValueError) -
    # orjson.JSONDecodeError must satisfy that, not just its own type.
    with pytest.raises(ValueError):
        fastjson.loads(b"{not valid json")


def test_load_success_reads_from_binary_file_object():
    fp = io.BytesIO(b'{"litellm_call_id": "abc123"}')
    assert fastjson.load(fp) == {"litellm_call_id": "abc123"}


# ---------------------------------------------------------------------------
# dumps / dump
# ---------------------------------------------------------------------------

def test_dumps_success_returns_bytes_not_str():
    result = fastjson.dumps({"a": 1})
    assert isinstance(result, bytes)


def test_dumps_success_roundtrips_through_loads():
    payload = {"litellm_call_id": "abc", "messages": [{"role": "user", "content": "hi"}], "n": 3.5}
    assert fastjson.loads(fastjson.dumps(payload)) == payload


def test_dumps_unsuccess_unsupported_type_without_default_raises():
    # A bare set isn't JSON-serializable and there's no default= to bail out to.
    with pytest.raises(TypeError):
        fastjson.dumps({"a", "b"})


def test_dumps_success_default_str_handles_unsupported_type():
    class Weird:
        def __str__(self):
            return "weird-value"

    result = fastjson.dumps({"x": Weird()}, default=str)
    assert fastjson.loads(result) == {"x": "weird-value"}


def test_dumps_success_indent_produces_multiline_output():
    compact = fastjson.dumps({"a": 1})
    indented = fastjson.dumps({"a": 1}, indent=2)
    assert b"\n" not in compact
    assert b"\n" in indented
    assert fastjson.loads(indented) == fastjson.loads(compact)


def test_dump_success_writes_bytes_to_binary_file_object():
    fp = io.BytesIO()
    fastjson.dump({"a": 1}, fp)
    assert fastjson.loads(fp.getvalue()) == {"a": 1}


def test_dump_success_forwards_default_and_indent():
    fp = io.BytesIO()
    fastjson.dump({"x": object()}, fp, indent=2, default=lambda o: "fallback")
    written = fp.getvalue()
    assert b"\n" in written
    assert fastjson.loads(written) == {"x": "fallback"}
