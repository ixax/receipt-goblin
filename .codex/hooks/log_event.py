#!/usr/bin/env python3
"""Handles Codex CLI lifecycle hooks (everything except SessionStart, see
log_session.py). Codex's hook stdin JSON is structurally close to Claude
Code's (session_id, tool_name, tool_input, tool_use_id, transcript_path,
prompt, agent_type/agent_id all match by name), so CodexEventHook shares
hooks.base.BaseEventHook (turn/sequence counters, ingest-API POST plumbing)
with .claude/hooks/log_event.py's ClaudeEventHook. Only the event/field
extraction below is Codex specific, because the two products' payload
shapes still differ in real ways:

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
  AGENT_CLI_TRACKING_DEBUG=1 and check stderr after a real Codex session if
  usage rows come through empty.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hooks"))
from base import BaseEventHook  # noqa: E402
from common import (  # noqa: E402
    debug,
    get_state_value,
    mark_permission_request,
    mark_tool_start,
    pop_permission_wait_ms,
    pop_tool_latency_ms,
    set_state_value,
)

# Codex has no dedicated failure event per tool call - PostToolUse fires
# either way, so status has to be guessed from tool_response's shape.
_ERROR_KEYS = ("error", "is_error")


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


class CodexEventHook(BaseEventHook):
    def extract_usage_since(self, transcript_path, offset_lines, offset_key):
        """Return (usage_rows, new_offset, response_text) for token_count
        events appended to the rollout JSONL since offset_lines. token_count
        reports cumulative totals for the whole session, so each row here is
        a diff against the previous snapshot - not a raw copy of the event,
        unlike Claude Code's per-message usage rows. response_text is
        always "" here - Stop/SubagentStop already have
        last_assistant_message straight from the payload, passed into
        report_usage() by the caller instead."""
        prev_cumulative = get_state_value(offset_key, "cumulative_usage", {})

        path = Path(transcript_path)
        if not path.exists():
            return [], offset_lines, ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return [], offset_lines, ""

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

        set_state_value(offset_key, "cumulative_usage", cumulative)
        return rows, len(lines), ""

    def handle_event(self):
        payload = self.payload
        event_type = self.event_type
        session_id = self.session_id

        tool_name = ""
        agent_name = ""
        status = ""
        latency_ms = None

        if event_type in ("PreToolUse", "PostToolUse", "PermissionRequest"):
            tool_name = payload.get("tool_name", "")
            agent_name = payload.get("agent_type", "") or payload.get("agent_id", "")
            key = self.tool_key(tool_name)
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
                self.report_usage(
                    subagent_transcript_path,
                    agent_name=agent_name,
                    response_text=payload.get("last_assistant_message", ""),
                    offset_key=f"codex-subagent:{agent_id}" if agent_id else subagent_transcript_path,
                )

        elif event_type in ("PreCompact", "PostCompact"):
            status = payload.get("trigger", "")

        elif event_type == "Stop":
            status = "success"
            latency_ms = pop_tool_latency_ms(session_id, "turn")
            turn_prompt_text = self.pop_turn_prompt()
            self.report_usage(
                payload.get("transcript_path"),
                prompt_text=turn_prompt_text,
                response_text=payload.get("last_assistant_message", ""),
            )

        elif event_type == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            payload["_prompt_preview"] = prompt[:500]
            mark_tool_start(session_id, "turn")
            self.remember_turn_prompt(prompt)

        return {
            "tool_name": tool_name,
            "agent_name": agent_name,
            "status": status,
            "latency_ms": latency_ms,
        }


if __name__ == "__main__":
    CodexEventHook().run()
    sys.exit(0)
