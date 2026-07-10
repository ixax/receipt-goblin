#!/usr/bin/env python3
"""Handles every Claude Code lifecycle hook except SessionStart/SessionEnd
(see log_session.py). One script for all events keeps field-extraction
logic (which varies per event type) in one place while sharing the
turn/sequence counters and HTTP plumbing from common.py.

Field names read off the hook JSON (tool_input.subagent_type,
message.usage, transcript line shape, etc.) are best-effort: Claude Code's
exact hook payload shape can change between versions. Run with
CLAUDE_TRACKING_DEBUG=1 to print the raw payload to stderr and adjust the
extraction helpers below if your installed version differs.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    get_state_value,
    get_user_id,
    mark_permission_request,
    mark_tool_start,
    next_ids,
    pop_permission_wait_ms,
    pop_tool_latency_ms,
    post_json,
    read_hook_input,
    resolve_agent_version,
    resolve_skill_version,
    set_state_value,
)

# Events that start a new turn (reset the per-turn sequence counter).
NEW_TURN_EVENTS = {"UserPromptSubmit"}


def _tool_key(payload, tool_name):
    # tool_use_id is the precise correlation key when Claude Code provides
    # it; otherwise fall back to tool_name (loses precision for concurrent
    # calls to the same tool within a turn, but never crashes).
    return payload.get("tool_use_id") or f"tool:{tool_name}"


def _extract_tool_and_skill(payload):
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    skill_name = ""
    # A tool call happening *inside* a subagent's own execution (its own
    # Read/Grep/etc, not the parent's call that spawned it) gets agent_id/
    # agent_type stamped directly on the payload - use that first so the
    # subagent's own tool calls are attributed to it instead of showing up
    # as anonymous, unattributed calls (previously the case: this field was
    # never read here, only in _extract_subagent_name for the
    # SubagentStart/SubagentStop lifecycle events themselves).
    agent_name = payload.get("agent_type", "") or payload.get("agent_name", "")
    if not agent_name and tool_name in ("Task", "Agent"):
        # The parent's own call that spawns a subagent - tool name differs
        # by Claude Code build ("Task" upstream, "Agent" in this
        # environment) - attribute it to the subagent it's about to spawn.
        agent_name = tool_input.get("subagent_type", "") or tool_input.get("description", "")
    if tool_name == "Skill":
        skill_name = tool_input.get("skill") or tool_input.get("name") or tool_input.get("skill_name") or ""
    return tool_name, agent_name, skill_name


def _extract_subagent_name(payload):
    return (
        payload.get("subagent_type")
        or payload.get("agent_name")
        or payload.get("agent_type")
        or ""
    )


def _extract_usage_since(transcript_path, offset_lines):
    """Return (usage_rows, new_offset, response_text) for assistant messages
    appended to the transcript since offset_lines. Tracking an offset
    (rather than re-reading the whole file each time) avoids double-counting
    usage across multiple Stop events in the same session.

    response_text concatenates every text content block from those same
    assistant messages (tool_use blocks are skipped) - it's the model's
    actual reply for whatever segment [offset_lines:] covers, paired with
    the usage numbers for that segment via agent_messages."""
    path = Path(transcript_path)
    if not path.exists():
        return [], offset_lines, ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], offset_lines, ""

    rows = []
    texts = []
    for line in lines[offset_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
        usage = message.get("usage")
        if not usage:
            continue
        cache_creation = usage.get("cache_creation") or {}
        server_tool_use = usage.get("server_tool_use") or {}
        rows.append({
            "model": message.get("model", ""),
            # Prefer the transcript's own recorded time (when the model
            # actually responded) over the ingest API's receive-time
            # fallback - otherwise hook/network lag or host/container
            # clock drift can push a usage row just outside a narrow
            # Grafana time range even though it's clearly visible at a
            # wider zoom.
            "timestamp": entry.get("timestamp"),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            # stop_reason lives on the message itself, not inside usage.
            "stop_reason": message.get("stop_reason") or "",
            "service_tier": usage.get("service_tier", ""),
            "speed": usage.get("speed", ""),
            "cache_creation_1h_tokens": cache_creation.get("ephemeral_1h_input_tokens", 0),
            "cache_creation_5m_tokens": cache_creation.get("ephemeral_5m_input_tokens", 0),
            "web_search_requests": server_tool_use.get("web_search_requests", 0),
            "web_fetch_requests": server_tool_use.get("web_fetch_requests", 0),
        })
    return rows, len(lines), "\n\n".join(texts)


def _remember_turn_skill(session_id, skill_name, skill_version):
    # Skills are invoked as a plain tool call within a turn - there is no
    # separate lifecycle event for them the way there is for subagents
    # (SubagentStart/Stop). Stash the skill here so Stop/StopFailure can
    # attach it to the usage rows it reports for this turn.
    set_state_value(session_id, "turn_skill", {"name": skill_name, "version": skill_version})


def _pop_turn_skill(session_id):
    skill = get_state_value(session_id, "turn_skill", {}) or {}
    set_state_value(session_id, "turn_skill", {})
    return skill.get("name", ""), skill.get("version", "")


def _remember_turn_prompt(session_id, prompt_text):
    # Same rationale as _remember_turn_skill: the prompt is submitted on
    # UserPromptSubmit, but the turn it belongs to (and the model's reply
    # to it) is only known once Stop/StopFailure reports usage.
    set_state_value(session_id, "turn_prompt", prompt_text)


def _pop_turn_prompt(session_id):
    prompt_text = get_state_value(session_id, "turn_prompt", "") or ""
    set_state_value(session_id, "turn_prompt", "")
    return prompt_text


def _remember_subagent_prompt(session_id, prompt_text):
    # The Task tool's own tool_input.prompt is the subagent's prompt - only
    # available at the parent's PreToolUse, while the reply is only known
    # once SubagentStop reports usage for the subagent's transcript. Keyed
    # by session_id (the parent's), same "last Task call wins if several
    # happen before the matching SubagentStop" simplification already used
    # for turn_skill/turn_mcp_tool above.
    set_state_value(session_id, "subagent_prompt", prompt_text)


def _pop_subagent_prompt(session_id):
    prompt_text = get_state_value(session_id, "subagent_prompt", "") or ""
    set_state_value(session_id, "subagent_prompt", "")
    return prompt_text


def _remember_turn_mcp_tool(session_id, mcp_tool_name):
    # Same rationale as _remember_turn_skill: an MCP tool call has no usage
    # of its own (it's just tool execution), the tokens it triggers are
    # spent on the surrounding turn - so stash which MCP tool was last
    # called this turn for Stop/StopFailure to attribute usage to. If more
    # than one MCP tool is called in the same turn, only the last one is
    # attributed (same deliberate simplification as turn-skill).
    set_state_value(session_id, "turn_mcp_tool", mcp_tool_name)


def _pop_turn_mcp_tool(session_id):
    mcp_tool_name = get_state_value(session_id, "turn_mcp_tool", "") or ""
    set_state_value(session_id, "turn_mcp_tool", "")
    return mcp_tool_name


def _report_usage(session_id, trace_id, turn_id, transcript_path, user_id, agent_name="", agent_version="", skill_name="", skill_version="", mcp_tool_name="", prompt_text="", offset_key=None):
    if not transcript_path:
        return
    # offset_key defaults to session_id (the main session's own transcript,
    # read incrementally across every Stop in that session). A subagent's
    # transcript is a different file entirely - the caller passes a unique
    # offset_key for it (its own path/agent_id) so this doesn't share
    # (and corrupt) the main session's offset counter.
    offset_key = offset_key or session_id
    offset = get_state_value(offset_key, "transcript_offset", 0)
    rows, new_offset, response_text = _extract_usage_since(transcript_path, offset)
    set_state_value(offset_key, "transcript_offset", new_offset)
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
                "skill_name": skill_name,
                "skill_version": skill_version,
                "mcp_tool_name": mcp_tool_name,
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_creation_tokens": row["cache_creation_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "stop_reason": row["stop_reason"],
                "service_tier": row["service_tier"],
                "speed": row["speed"],
                "cache_creation_1h_tokens": row["cache_creation_1h_tokens"],
                "cache_creation_5m_tokens": row["cache_creation_5m_tokens"],
                "web_search_requests": row["web_search_requests"],
                "web_fetch_requests": row["web_fetch_requests"],
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
                "skill_name": skill_name,
                "skill_version": skill_version,
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

    parent_session_id = payload.get("parent_session_id", "") or ""
    trace_id = payload.get("trace_id") or parent_session_id or session_id

    turn_id, sequence_id = next_ids(session_id, new_turn=event_type in NEW_TURN_EVENTS)

    tool_name = ""
    agent_name = ""
    agent_version = ""
    skill_name = ""
    skill_version = ""
    status = ""
    latency_ms = None

    if event_type in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"):
        tool_name, agent_name, skill_name = _extract_tool_and_skill(payload)
        if agent_name:
            agent_version = resolve_agent_version(agent_name)
        if skill_name:
            skill_version = resolve_skill_version(skill_name)
            _remember_turn_skill(session_id, skill_name, skill_version)
        if tool_name.startswith("mcp__"):
            _remember_turn_mcp_tool(session_id, tool_name)
        if event_type == "PreToolUse" and tool_name == "Task":
            task_prompt = (payload.get("tool_input") or {}).get("prompt", "")
            if task_prompt:
                _remember_subagent_prompt(session_id, task_prompt)

        key = _tool_key(payload, tool_name)
        if event_type == "PreToolUse":
            mark_tool_start(session_id, key)
            # If a permission prompt preceded this (i.e. the tool wasn't
            # already allowed), latency_ms here is the wait for the user's
            # decision, not tool execution time - that's still measured
            # separately via PostToolUse's own latency_ms.
            latency_ms = pop_permission_wait_ms(session_id, key)
        elif event_type in ("PostToolUse", "PostToolUseFailure"):
            latency_ms = pop_tool_latency_ms(session_id, key)
            status = "error" if event_type == "PostToolUseFailure" else "success"
        elif event_type == "PermissionDenied":
            status = "denied"
            latency_ms = pop_permission_wait_ms(session_id, key)
        elif event_type == "PermissionRequest":
            status = "requested"
            mark_permission_request(session_id, key)

    elif event_type == "PostToolBatch":
        tool_name = "batch"
        status = payload.get("status", "")

    elif event_type in ("SubagentStart", "SubagentStop"):
        agent_name = _extract_subagent_name(payload)
        agent_version = resolve_agent_version(agent_name)
        status = payload.get("status", "")
        if event_type == "SubagentStop":
            subagent_prompt_text = _pop_subagent_prompt(session_id)
            # payload["transcript_path"] is the *parent* session's transcript,
            # not the subagent's own - Claude Code puts the subagent's actual
            # transcript at agent_transcript_path. Falling back to
            # transcript_path keeps this working (parent transcript, better
            # than nothing) on older versions that don't send the dedicated
            # field.
            subagent_transcript_path = payload.get("agent_transcript_path") or payload.get("transcript_path")
            agent_id = payload.get("agent_id", "")
            _report_usage(
                session_id, trace_id, turn_id,
                subagent_transcript_path, user_id,
                agent_name=agent_name, agent_version=agent_version,
                prompt_text=subagent_prompt_text,
                # Unique per subagent invocation - this transcript is a
                # different file than the parent's, so it must not share
                # the parent session's transcript_offset counter (that
                # would either skip the whole thing or double count).
                offset_key=f"subagent:{agent_id}" if agent_id else subagent_transcript_path,
            )

    elif event_type in ("PreCompact", "PostCompact"):
        status = payload.get("trigger", "")

    elif event_type in ("Stop", "StopFailure"):
        status = "error" if event_type == "StopFailure" else "success"
        # Reuses the same generic start/elapsed timer PreToolUse/PostToolUse
        # uses for tool execution time, keyed by a fixed "turn" string
        # instead of a tool_use_id - so latency_ms on a Stop/StopFailure row
        # means turn duration (UserPromptSubmit -> Stop), one more meaning
        # for the same overloaded field alongside tool exec / permission wait.
        latency_ms = pop_tool_latency_ms(session_id, "turn")
        turn_skill_name, turn_skill_version = _pop_turn_skill(session_id)
        turn_mcp_tool_name = _pop_turn_mcp_tool(session_id)
        turn_prompt_text = _pop_turn_prompt(session_id)
        _report_usage(
            session_id, trace_id, turn_id, payload.get("transcript_path"), user_id,
            skill_name=turn_skill_name, skill_version=turn_skill_version,
            mcp_tool_name=turn_mcp_tool_name, prompt_text=turn_prompt_text,
        )

    elif event_type == "UserPromptSubmit":
        prompt = payload.get("prompt", "")
        payload["_prompt_preview"] = prompt[:500]
        mark_tool_start(session_id, "turn")
        _remember_turn_skill(session_id, "", "")
        _remember_turn_mcp_tool(session_id, "")
        _remember_turn_prompt(session_id, prompt)

    post_json(
        "/ingest/event",
        {
            "session_id": session_id,
            "trace_id": trace_id,
            "parent_session_id": parent_session_id,
            "turn_id": turn_id,
            "sequence_id": sequence_id,
            "event_type": event_type,
            "tool_name": tool_name,
            "agent_name": agent_name,
            "agent_version": agent_version,
            "skill_name": skill_name,
            "skill_version": skill_version,
            "status": status,
            "latency_ms": latency_ms,
            "raw_payload": payload,
        },
        user_id=user_id,
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
