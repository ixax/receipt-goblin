"""Base classes for the agent-tracking lifecycle hooks.

Owns everything that is identical across Claude Code and Codex CLI: reading
the hook's stdin payload, user identity, turn/sequence bookkeeping, and
preparing + POSTing the ingest API request bodies. Each CLI's own
`.claude/hooks/log_event.py` / `.codex/hooks/log_event.py` (and
`log_session.py`) subclass these and implement only the extraction that
genuinely differs per runner: which fields live where in that runner's
payload, and how to parse that runner's transcript/rollout file for usage.
"""
from abc import ABC, abstractmethod

from common import get_state_value, get_user_id, next_ids, post_json, read_hook_input, set_state_value


class _HookBase:
    def __init__(self):
        self.payload = read_hook_input()
        self.session_id = self.payload.get("session_id", "")
        self.user_id = get_user_id()


class BaseEventHook(_HookBase, ABC):
    """Base for the "every event except SessionStart/SessionEnd" hook."""

    NEW_TURN_EVENTS = {"UserPromptSubmit"}

    def __init__(self):
        super().__init__()
        self.event_type = self.payload.get("hook_event_name", "Unknown")

    def parent_session_id(self):
        # Codex has no parent_session_id concept; ClaudeEventHook overrides.
        return ""

    def trace_id(self):
        return self.payload.get("trace_id") or self.parent_session_id() or self.session_id

    def tool_key(self, tool_name):
        # tool_use_id is the precise correlation key when the CLI provides
        # it; otherwise fall back to tool_name (loses precision for
        # concurrent calls to the same tool within a turn, never crashes).
        return self.payload.get("tool_use_id") or f"tool:{tool_name}"

    def remember_turn_prompt(self, prompt_text):
        # The prompt is submitted on UserPromptSubmit, but the turn it
        # belongs to (and the model's reply) is only known once Stop
        # reports usage.
        set_state_value(self.session_id, "turn_prompt", prompt_text)

    def pop_turn_prompt(self):
        text = get_state_value(self.session_id, "turn_prompt", "") or ""
        set_state_value(self.session_id, "turn_prompt", "")
        return text

    @abstractmethod
    def handle_event(self) -> dict:
        """Dispatch on self.event_type/self.payload, do any state
        bookkeeping and report_usage() calls, and return a dict with a
        subset of {tool_name, agent_name, agent_version, skill_name,
        skill_version, status, latency_ms} - post_event() defaults the
        rest to ""/None."""

    @abstractmethod
    def extract_usage_since(self, transcript_path, offset_lines, offset_key):
        """Parse this runner's transcript/rollout file for the usage rows
        appended since offset_lines. Returns (rows, new_offset_lines,
        response_text). `rows` are dicts already shaped as the
        /ingest/usage wire fields this runner populates - report_usage()
        merges them straight into the common envelope. A runner needing
        extra persisted state beyond the line offset (e.g. Codex's
        cumulative token counters) reads/writes it itself via
        get_state_value/set_state_value(offset_key, ...)."""

    def report_usage(self, transcript_path, *, agent_name="", agent_version="",
                      skill_name="", skill_version="", mcp_tool_name="",
                      prompt_text="", response_text="", offset_key=None):
        if not transcript_path:
            return
        offset_key = offset_key or self.session_id
        offset = get_state_value(offset_key, "transcript_offset", 0)
        rows, new_offset, extracted_text = self.extract_usage_since(transcript_path, offset, offset_key)
        set_state_value(offset_key, "transcript_offset", new_offset)
        response_text = response_text or extracted_text

        envelope = {
            "session_id": self.session_id,
            "trace_id": self.trace_id(),
            "turn_id": self.turn_id,
            "agent_name": agent_name,
            "agent_version": agent_version,
            "skill_name": skill_name,
            "skill_version": skill_version,
            "mcp_tool_name": mcp_tool_name,
        }
        for row in rows:
            post_json("/ingest/usage", {**envelope, **row}, user_id=self.user_id)

        if prompt_text or response_text:
            post_json(
                "/ingest/message",
                {**envelope, "prompt_text": prompt_text, "response_text": response_text},
                user_id=self.user_id,
            )

    def post_event(self, fields):
        post_json(
            "/ingest/event",
            {
                "session_id": self.session_id,
                "trace_id": self.trace_id(),
                "parent_session_id": self.parent_session_id(),
                "turn_id": self.turn_id,
                "sequence_id": self.sequence_id,
                "event_type": self.event_type,
                "tool_name": fields.get("tool_name", ""),
                "agent_name": fields.get("agent_name", ""),
                "agent_version": fields.get("agent_version", ""),
                "skill_name": fields.get("skill_name", ""),
                "skill_version": fields.get("skill_version", ""),
                "status": fields.get("status", ""),
                "latency_ms": fields.get("latency_ms"),
                "raw_payload": self.payload,
            },
            user_id=self.user_id,
        )

    def run(self):
        # Stashed on self (rather than threaded through every method
        # signature) so handle_event()'s own report_usage() calls (e.g.
        # SubagentStop) can reference self.turn_id directly.
        self.turn_id, self.sequence_id = next_ids(self.session_id, new_turn=self.event_type in self.NEW_TURN_EVENTS)
        self.post_event(self.handle_event())


class BaseSessionHook(_HookBase, ABC):
    """Base for the SessionStart/SessionEnd hook."""

    def event_type(self):
        return "SessionStart"

    def on_session_start(self, event_type):
        pass

    @abstractmethod
    def extract_status(self):
        ...

    def run(self):
        turn_id, sequence_id = next_ids(self.session_id, new_turn=False)
        event_type = self.event_type()
        self.on_session_start(event_type)
        post_json(
            "/ingest/event",
            {
                "session_id": self.session_id,
                "trace_id": self.session_id,
                "turn_id": turn_id,
                "sequence_id": sequence_id,
                "event_type": event_type,
                "status": self.extract_status(),
                "raw_payload": self.payload,
            },
            user_id=self.user_id,
        )
