#!/usr/bin/env python3
"""Handles every Claude Code lifecycle hook except SessionStart/SessionEnd
(see log_session.py). ClaudeEventHook subclasses hooks.base.BaseEventHook,
which owns the turn/sequence counters and the ingest-API POST plumbing -
only the field extraction below is Claude Code specific.

Field names read off the hook JSON (tool_input.subagent_type,
message.usage, transcript line shape, etc.) are best-effort: Claude Code's
exact hook payload shape can change between versions. Run with
AGENT_CLI_TRACKING_DEBUG=1 to print the raw payload to stderr and adjust the
extraction helpers below if your installed version differs.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from base import BaseEventHook  # noqa: E402
from common import (  # noqa: E402
    get_state_value,
    mark_permission_request,
    mark_tool_start,
    pop_permission_wait_ms,
    pop_tool_latency_ms,
    resolve_agent_version,
    resolve_skill_version,
    set_state_value,
)


class ClaudeEventHook(BaseEventHook):
    def parent_session_id(self):
        return self.payload.get("parent_session_id", "") or ""

    def _extract_tool_and_skill(self):
        tool_name = self.payload.get("tool_name", "")
        tool_input = self.payload.get("tool_input") or {}
        skill_name = ""
        # A tool call happening *inside* a subagent's own execution (its own
        # Read/Grep/etc, not the parent's call that spawned it) gets agent_id/
        # agent_type stamped directly on the payload - use that first so the
        # subagent's own tool calls are attributed to it instead of showing up
        # as anonymous, unattributed calls.
        agent_name = self.payload.get("agent_type", "") or self.payload.get("agent_name", "")
        if not agent_name and tool_name in ("Task", "Agent"):
            # The parent's own call that spawns a subagent - tool name differs
            # by Claude Code build ("Task" upstream, "Agent" in this
            # environment) - attribute it to the subagent it's about to spawn.
            agent_name = tool_input.get("subagent_type", "") or tool_input.get("description", "")
        if tool_name == "Skill":
            skill_name = tool_input.get("skill") or tool_input.get("name") or tool_input.get("skill_name") or ""
        return tool_name, agent_name, skill_name

    def _extract_subagent_name(self):
        return (
            self.payload.get("subagent_type")
            or self.payload.get("agent_name")
            or self.payload.get("agent_type")
            or ""
        )

    def extract_usage_since(self, transcript_path, offset_lines, offset_key):
        """Return (usage_rows, new_offset, response_text) for assistant
        messages appended to the transcript since offset_lines. Tracking an
        offset (rather than re-reading the whole file each time) avoids
        double-counting usage across multiple Stop events in the same
        session.

        response_text concatenates every text content block from those same
        assistant messages (tool_use blocks are skipped) - it's the model's
        actual reply for whatever segment [offset_lines:] covers, paired
        with the usage numbers for that segment via agent_messages."""
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

    def _remember_turn_skill(self, skill_name, skill_version):
        # Skills are invoked as a plain tool call within a turn - there is no
        # separate lifecycle event for them the way there is for subagents
        # (SubagentStart/Stop). Stash the skill here so Stop/StopFailure can
        # attach it to the usage rows it reports for this turn.
        set_state_value(self.session_id, "turn_skill", {"name": skill_name, "version": skill_version})

    def _pop_turn_skill(self):
        skill = get_state_value(self.session_id, "turn_skill", {}) or {}
        set_state_value(self.session_id, "turn_skill", {})
        return skill.get("name", ""), skill.get("version", "")

    def _remember_subagent_prompt(self, prompt_text):
        # The Task tool's own tool_input.prompt is the subagent's prompt -
        # only available at the parent's PreToolUse, while the reply is only
        # known once SubagentStop reports usage for the subagent's
        # transcript. Keyed by session_id (the parent's), same "last Task
        # call wins if several happen before the matching SubagentStop"
        # simplification already used for turn_skill/turn_mcp_tool below.
        set_state_value(self.session_id, "subagent_prompt", prompt_text)

    def _pop_subagent_prompt(self):
        prompt_text = get_state_value(self.session_id, "subagent_prompt", "") or ""
        set_state_value(self.session_id, "subagent_prompt", "")
        return prompt_text

    def _remember_turn_mcp_tool(self, mcp_tool_name):
        # Same rationale as _remember_turn_skill: an MCP tool call has no
        # usage of its own (it's just tool execution), the tokens it
        # triggers are spent on the surrounding turn - so stash which MCP
        # tool was last called this turn for Stop/StopFailure to attribute
        # usage to. If more than one MCP tool is called in the same turn,
        # only the last one is attributed (same deliberate simplification
        # as turn-skill).
        set_state_value(self.session_id, "turn_mcp_tool", mcp_tool_name)

    def _pop_turn_mcp_tool(self):
        mcp_tool_name = get_state_value(self.session_id, "turn_mcp_tool", "") or ""
        set_state_value(self.session_id, "turn_mcp_tool", "")
        return mcp_tool_name

    def handle_event(self):
        payload = self.payload
        event_type = self.event_type
        session_id = self.session_id

        tool_name = ""
        agent_name = ""
        agent_version = ""
        skill_name = ""
        skill_version = ""
        status = ""
        latency_ms = None

        if event_type in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied"):
            tool_name, agent_name, skill_name = self._extract_tool_and_skill()
            if agent_name:
                agent_version = resolve_agent_version(agent_name)
            if skill_name:
                skill_version = resolve_skill_version(skill_name)
                self._remember_turn_skill(skill_name, skill_version)
            if tool_name.startswith("mcp__"):
                self._remember_turn_mcp_tool(tool_name)
            if event_type == "PreToolUse" and tool_name == "Task":
                task_prompt = (payload.get("tool_input") or {}).get("prompt", "")
                if task_prompt:
                    self._remember_subagent_prompt(task_prompt)

            key = self.tool_key(tool_name)
            if event_type == "PreToolUse":
                mark_tool_start(session_id, key)
                # If a permission prompt preceded this (i.e. the tool wasn't
                # already allowed), latency_ms here is the wait for the
                # user's decision, not tool execution time - that's still
                # measured separately via PostToolUse's own latency_ms.
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
            agent_name = self._extract_subagent_name()
            agent_version = resolve_agent_version(agent_name)
            status = payload.get("status", "")
            if event_type == "SubagentStop":
                subagent_prompt_text = self._pop_subagent_prompt()
                # payload["transcript_path"] is the *parent* session's
                # transcript, not the subagent's own - Claude Code puts the
                # subagent's actual transcript at agent_transcript_path.
                # Falling back to transcript_path keeps this working (parent
                # transcript, better than nothing) on older versions that
                # don't send the dedicated field.
                subagent_transcript_path = payload.get("agent_transcript_path") or payload.get("transcript_path")
                agent_id = payload.get("agent_id", "")
                self.report_usage(
                    subagent_transcript_path,
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
            # Reuses the same generic start/elapsed timer PreToolUse/
            # PostToolUse uses for tool execution time, keyed by a fixed
            # "turn" string instead of a tool_use_id - so latency_ms on a
            # Stop/StopFailure row means turn duration (UserPromptSubmit ->
            # Stop), one more meaning for the same overloaded field
            # alongside tool exec / permission wait.
            latency_ms = pop_tool_latency_ms(session_id, "turn")
            turn_skill_name, turn_skill_version = self._pop_turn_skill()
            turn_mcp_tool_name = self._pop_turn_mcp_tool()
            turn_prompt_text = self.pop_turn_prompt()
            self.report_usage(
                payload.get("transcript_path"),
                skill_name=turn_skill_name, skill_version=turn_skill_version,
                mcp_tool_name=turn_mcp_tool_name, prompt_text=turn_prompt_text,
            )

        elif event_type == "UserPromptSubmit":
            prompt = payload.get("prompt", "")
            payload["_prompt_preview"] = prompt[:500]
            mark_tool_start(session_id, "turn")
            self._remember_turn_skill("", "")
            self._remember_turn_mcp_tool("")
            self.remember_turn_prompt(prompt)

        return {
            "tool_name": tool_name,
            "agent_name": agent_name,
            "agent_version": agent_version,
            "skill_name": skill_name,
            "skill_version": skill_version,
            "status": status,
            "latency_ms": latency_ms,
        }


if __name__ == "__main__":
    ClaudeEventHook().run()
    sys.exit(0)
