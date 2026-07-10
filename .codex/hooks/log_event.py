#!/usr/bin/env python3
"""Handles Codex CLI lifecycle hooks (everything except SessionStart, see
log_session.py). Codex's hook stdin JSON is structurally close to Claude
Code's (session_id, tool_name, tool_input, tool_use_id, transcript_path,
prompt, agent_type/agent_id all match by name), so this is *not* a
copy-paste of .claude/hooks/log_event.py - it imports common.py from that
directory directly and reuses every primitive (user id, POST, turn/
sequence counters, tool-start/latency timers, generic per-session state).
Only the event/field extraction below is new, because the two products'
payload shapes still differ in real ways:

- No PostToolUseFailure/PermissionDenied/PostToolBatch/StopFailure events
  exist in Codex - every tool call reports through PostToolUse, and
  success/failure has to be *inferred* from tool_response (best-effort,
  see _status_from_tool_response).
- Stop's payload already carries last_assistant_message directly, so
  (unlike Claude Code) there's no need to re-parse the transcript for the
  turn's reply text - only for token usage.
- Usage isn't per-message like Claude Code's transcript; Codex's rollout
  JSONL (~/.codex/sessions/.../rollout-*.jsonl) reports *cumulative*
  token_count events that must be diffed against the previous snapshot to
  recover per-turn usage. The exact nesting under
  payload.info for a token_count event_msg is not confirmed from public
  docs at the time this was written - _find_dict_with_key searches for it
  defensively instead of assuming one fixed path. Run with
  CLAUDE_TRACKING_DEBUG=1 and check stderr after a real Codex session if
  usage rows come through empty.
"""
import json
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / ".claude" / "hooks"))
import common  # noqa: E402
from common import debug, get_user_id, post_json, read_hook_input  # noqa: E402

# common.py's state functions take an explicit `namespace` param (default
# ".claude") so Claude Code and Codex CLI hooks - which both import this
# module directly, no copy-paste - keep separate state directories.
# Binding it once here means every call site below stays unchanged from
# what it'd look like calling common.py directly.
STATE_NAMESPACE = ".codex"
next_ids = partial(common.next_ids, namespace=STATE_NAMESPACE)
mark_tool_start = partial(common.mark_tool_start, namespace=STATE_NAMESPACE)
pop_tool_latency_ms = partial(common.pop_tool_latency_ms, namespace=STATE_NAMESPACE)
mark_permission_request = partial(common.mark_permission_request, namespace=STATE_NAMESPACE)
pop_permission_wait_ms = partial(common.pop_permission_wait_ms, namespace=STATE_NAMESPACE)
get_state_value = partial(common.get_state_value, namespace=STATE_NAMESPACE)
set_state_value = partial(common.set_state_value, namespace=STATE_NAMESPACE)

NEW_TURN_EVENTS = {"UserPromptSubmit"}
# Codex has no dedicated failure event per tool call - PostToolUse fires
# either way, so status has to be guessed from tool_response's shape.
_ERROR_KEYS = ("error", "is_error")


def _tool_key(payload, tool_name):
    return payload.get("tool_use_id") or f"tool:{tool_name}"


def _status_from_tool_response(tool_response):
    if not isinstance(tool_response, dict):
        return "success"
    for key in _ERROR_KEYS:
        if tool_response.get(key):
            return "error"
    if tool_response.get("success") is False:
        return "error"
    return "success"


def _find_dict_with_key(obj, key, max_depth=5):
    """Depth-limited search for a dict containing `key` anywhere under
    `obj`. Used for the token_count payload whose exact nesting isn't
    confirmed - see module docstring."""
    if max_depth < 0:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj
        for value in obj.values():
            found = _find_dict_with_key(value, key, max_depth - 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_dict_with_key(value, key, max_depth - 1)
            if found is not None:
                return found
    return None


def _extract_usage_since(transcript_path, offset_lines, prev_cumulative):
    """Return (usage_rows, new_offset, new_cumulative) for token_count
    events appended to the rollout JSONL since offset_lines. token_count
    reports cumulative totals for the whole session, so each row here is
    a diff against prev_cumulative - not a raw copy of the event, unlike
    Claude Code's per-message usage rows."""
    path = Path(transcript_path)
    if not path.exists():
        return [], offset_lines, prev_cumulative
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], offset_lines, prev_cumulative

    rows = []
    current_model = ""
    cumulative = dict(prev_cumulative or {})
    for line in lines[offset_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry_type = entry.get("type")
        if entry_type == "turn_context":
            current_model = entry.get("model") or current_model
            continue
        if entry_type != "event_msg":
            continue
        payload = entry.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        info = _find_dict_with_key(payload, "input_tokens") or _find_dict_with_key(payload, "total_tokens")
        if not info:
            debug(f"codex-tracking: token_count with no recognizable usage fields: {json.dumps(payload)[:300]}")
            continue
        current = {
            "input_tokens": info.get("input_tokens", 0) or 0,
            "cached_input_tokens": info.get("cached_input_tokens", 0) or 0,
            "output_tokens": info.get("output_tokens", 0) or 0,
            "reasoning_output_tokens": info.get("reasoning_output_tokens", 0) or 0,
        }
        delta = {k: current[k] - cumulative.get(k, 0) for k in current}
        if any(v < 0 for v in delta.values()):
            # Cumulative counters went backwards - e.g. a /compact reset
            # them. Treat this snapshot as the new baseline rather than
            # emit a bogus negative-token row.
            delta = {k: 0 for k in current}
        cumulative = current
        if any(delta.values()):
            rows.append({
                "model": current_model,
                "timestamp": entry.get("timestamp"),
                "input_tokens": delta["input_tokens"],
                # No separate reasoning-token column in this stack's
                # schema (Claude Code's extended-thinking tokens aren't
                # tracked separately either) - folded into output_tokens.
                "output_tokens": delta["output_tokens"] + delta["reasoning_output_tokens"],
                "cache_read_tokens": delta["cached_input_tokens"],
            })
    return rows, len(lines), cumulative


def _report_usage(session_id, trace_id, turn_id, transcript_path, user_id,
                   agent_name="", agent_version="", prompt_text="", response_text="",
                   offset_key=None):
    if not transcript_path:
        return
    offset_key = offset_key or session_id
    offset = get_state_value(offset_key, "transcript_offset", 0)
    prev_cumulative = get_state_value(offset_key, "cumulative_usage", {})
    rows, new_offset, new_cumulative = _extract_usage_since(transcript_path, offset, prev_cumulative)
    set_state_value(offset_key, "transcript_offset", new_offset)
    set_state_value(offset_key, "cumulative_usage", new_cumulative)
    for row in rows:
        post_json(
            "/ingest/usage",
            {
                "session_id": session_id,
                "trace_id": trace_id,
                "turn_id": turn_id,
                "timestamp": row.get("timestamp"),
                "model": row["model"],
                "agent_name": agent_name,
                "agent_version": agent_version,
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
            },
            user_id=user_id,
        )
    if prompt_text or response_text:
        post_json(
            "/ingest/message",
            {
                "session_id": session_id,
                "trace_id": trace_id,
                "turn_id": turn_id,
                "agent_name": agent_name,
                "agent_version": agent_version,
                "prompt_text": prompt_text,
                "response_text": response_text,
            },
            user_id=user_id,
        )


def main():
    payload = read_hook_input()
    event_type = payload.get("hook_event_name", "Unknown")
    session_id = payload.get("session_id", "")
    user_id = get_user_id()

    # Codex has no parent_session_id/trace_id fields of its own (no
    # subagent-tree identity beyond agent_id/agent_type on the subagent
    # events themselves) - trace_id just mirrors session_id.
    trace_id = session_id

    turn_id, sequence_id = next_ids(session_id, new_turn=event_type in NEW_TURN_EVENTS)

    tool_name = ""
    agent_name = ""
    status = ""
    latency_ms = None

    if event_type in ("PreToolUse", "PostToolUse", "PermissionRequest"):
        tool_name = payload.get("tool_name", "")
        agent_name = payload.get("agent_type", "") or payload.get("agent_id", "")
        key = _tool_key(payload, tool_name)
        if event_type == "PreToolUse":
            mark_tool_start(session_id, key)
            latency_ms = pop_permission_wait_ms(session_id, key)
        elif event_type == "PostToolUse":
            latency_ms = pop_tool_latency_ms(session_id, key)
            status = _status_from_tool_response(payload.get("tool_response"))
        elif event_type == "PermissionRequest":
            status = "requested"
            mark_permission_request(session_id, key)

    elif event_type in ("SubagentStart", "SubagentStop"):
        agent_name = payload.get("agent_type", "") or payload.get("agent_id", "")
        if event_type == "SubagentStop":
            agent_id = payload.get("agent_id", "")
            subagent_transcript_path = payload.get("agent_transcript_path")
            _report_usage(
                session_id, trace_id, turn_id, subagent_transcript_path, user_id,
                agent_name=agent_name,
                response_text=payload.get("last_assistant_message", ""),
                offset_key=f"codex-subagent:{agent_id}" if agent_id else subagent_transcript_path,
            )

    elif event_type in ("PreCompact", "PostCompact"):
        status = payload.get("trigger", "")

    elif event_type == "Stop":
        status = "success"
        latency_ms = pop_tool_latency_ms(session_id, "turn")
        turn_prompt_text = get_state_value(session_id, "turn_prompt", "") or ""
        set_state_value(session_id, "turn_prompt", "")
        _report_usage(
            session_id, trace_id, turn_id, payload.get("transcript_path"), user_id,
            prompt_text=turn_prompt_text,
            response_text=payload.get("last_assistant_message", ""),
        )

    elif event_type == "UserPromptSubmit":
        prompt = payload.get("prompt", "")
        payload["_prompt_preview"] = prompt[:500]
        mark_tool_start(session_id, "turn")
        set_state_value(session_id, "turn_prompt", prompt)

    post_json(
        "/ingest/event",
        {
            "session_id": session_id,
            "trace_id": trace_id,
            "turn_id": turn_id,
            "sequence_id": sequence_id,
            "event_type": event_type,
            "tool_name": tool_name,
            "agent_name": agent_name,
            "status": status,
            "latency_ms": latency_ms,
            "raw_payload": payload,
        },
        user_id=user_id,
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
