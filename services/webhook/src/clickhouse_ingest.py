"""Parses LiteLLM's StandardLoggingPayload webhook events and inserts them
into ClickHouse (agent_events, agent_usage, agent_messages). agent_name/
skill_name are recovered from the payload's own messages (Agent/Skill
tool_use blocks), not from a CLI-side hook - see AGENTS.md.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import clickhouse_connect

from . import fastjson
from .config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
)

_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f]+)")
_COMMAND_NAME_RE = re.compile(r"<command-name>/?(.*?)</command-name>")
_COMMAND_VERSION_RE = re.compile(r"<version>(.*?)</version>")
# Codex CLI's own persistent-context continuation wrapper - its equivalent
# of this repo's Claude Code /goal Stop hook, re-injected as the "prompt" on
# every turn the underlying context (goal, plan, ...) stays active. Its
# "source" attribute names which one (e.g. "goal") - treated as a synthetic
# command by that name (see _active_command_name_and_version/
# _prompt_kind_and_display) so it rolls up into the same command_name
# accounting as a real Claude Code slash command, with the <objective> text
# standing in for <command-args>. Not hardcoded to "goal" specifically -
# whatever Codex names a future context (e.g. "plan") is picked up as-is.
_CODEX_INTERNAL_CONTEXT_RE = re.compile(r'<codex_internal_context\s+source="([^"]+)">')
_OBJECTIVE_TAG_RE = re.compile(r"<objective>\s*(.*?)\s*</objective>", re.DOTALL)
# Codex's collaboration-mode-switch notice - see _codex_collaboration_mode_change.
_COLLABORATION_MODE_HEADER_RE = re.compile(r"<collaboration_mode>#\s*(.*?)\s*\n")

# calculated_type prompt-prefix classifiers (category B), checked only
# when the response made no tool call at all (category A, see _classify_event).
_JUDGE_CALL_PREFIX = "Based on the conversation transcript above"
_SYSTEM_NOTIFICATION_PREFIX = "[SYSTEM NOTIFICATION"
_SUGGESTION_MODE_PREFIX = "[SUGGESTION MODE"
_TRANSCRIPT_HANDOFF_PREFIX = "<transcript>"
_TITLE_GEN_PREFIX = "<session>"
_INTERRUPTED_PREFIX = "[Request interrupted by user]"
_WEBPAGE_CONTENT_PREFIX = "Web page content"
_STOP_HOOK_FEEDBACK_PREFIX = "Stop hook feedback:"
# Claude Code's own auto-injected prompt asking the model to summarize
# progress for an idle-then-returning user - never typed by a human.
_AWAY_RECAP_PREFIX = "The user stepped away and is coming back."

# Trace panel (panel-76) text-cleaning regexes, ported from its SQL CTEs
# so display text is precomputed once at ingest instead of per dashboard load.
# "+" (not one bare block): Claude Code can inject more than one
# system-reminder as separate leading content blocks of the same user turn
# (e.g. a skills-listing reminder followed by a memory/claudeMd reminder) -
# a single non-repeating match here only strips the first, leaving the
# second sitting in front of the real prompt text untouched.
_SYSTEM_REMINDER_STRIP_RE = re.compile(r"^(?:\s*<system-reminder>.*?</system-reminder>)+\s*", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_LOCAL_STDOUT_STRIP_RE = re.compile(r"^.*</local-command-stdout>", re.DOTALL)
_INTERRUPTED_STRIP_RE = re.compile(r"^\[Request interrupted by user\]\s*")
_SUMMARY_TAG_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_STATUS_TAG_RE = re.compile(r"<status>(.*?)</status>")
_SEVERITY_TAG_RE = re.compile(r"<severity>\s*(\d+)")
_BLOCK_TAG_RE = re.compile(r"<block>\s*(yes|no)", re.IGNORECASE)
# Shape: "Stop hook feedback:\n[<prompt>]: <reasoning>". Greedy match so it
# lands on the *last* "]: " even if the prompt itself contains "]: ".
_STOP_HOOK_REASON_RE = re.compile(r"^Stop hook feedback:\n\[.*\]: (.*)$", re.DOTALL)
_WHITESPACE_COLLAPSE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_COLLAPSE_RE = re.compile(r"\n{2,}")

_DISPLAY_TEXT_TRUNCATE = 1500
_WEBPAGE_DISPLAY_TRUNCATE = 100

# tool_render's argument preference chain, ported verbatim (order matters).
_TOOL_ARG_KEY_PREFERENCE = ("file_path", "command", "sql", "url", "query", "description", "summary", "task_id")

# provider classification for agent_usage.provider, computed once at
# ingest instead of duplicated across ~30 Grafana panels.
_PROVIDER_OPENAI_RE = re.compile(r"^(gpt-|chatgpt-|o[0-9]|text-embedding-|dall-e-|whisper|tts-)")

logger = logging.getLogger("webhook.clickhouse_ingest")

_client = None


def get_client():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=CLICKHOUSE_HOST,
            port=CLICKHOUSE_PORT,
            username=CLICKHOUSE_USER,
            password=CLICKHOUSE_PASSWORD,
            database=CLICKHOUSE_DATABASE,
        )
    return _client


def clickhouse_alive() -> None:
    """Raises if ClickHouse isn't reachable. Shared by server.py's /health
    and worker.py's startup dependency check, so the liveness probe itself
    (currently just SELECT 1) only has one place to change."""
    get_client().command("SELECT 1")


def _to_dt(epoch_seconds: Optional[float]) -> datetime:
    if not epoch_seconds:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _flatten_content(content: Any) -> str:
    """Extracts human-readable text from Anthropic content (string or
    text/tool_use/tool_result blocks) or Responses-API content (input_text/
    output_text blocks, used by the "chatgpt" provider); tool payloads are
    captured elsewhere."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in ("text", "input_text", "output_text") and block.get("text"):
            parts.append(block["text"])
        elif block_type == "tool_use":
            parts.append(f"[tool_use:{block.get('name', '')}]")
        elif block_type == "tool_result":
            parts.append("[tool_result]")
    return "\n".join(parts)


def _last_user_text(messages: Any) -> str:
    """Most recent human-typed turn - skips tool_result continuations, which
    are also role="user" but automatic, not human input. Also skips non-turn
    items from Responses-API-shaped messages (the "chatgpt" provider mixes
    "reasoning"/"custom_tool_call"/etc. items into the same flat list -
    only "message" items are actual chat turns)."""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("type", "message") != "message":
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
        return _flatten_content(content) if not isinstance(content, str) else content
    return ""


def _codex_collaboration_mode_change(messages: Any) -> str:
    """Codex CLI only - Codex occasionally injects a fresh role="developer"
    message announcing a collaboration-mode switch (e.g. leaving Plan Mode
    for "Collaboration Mode: Default") immediately before the next real
    user turn. Returns that mode's header line (e.g. "Collaboration Mode:
    Default") only on the call where the switch just happened - the notice
    stays in `messages` history afterward, but on later calls it no longer
    sits immediately before the latest user turn, so this won't re-fire for
    the same switch. "" when no such notice preceded this turn (the normal
    case, and always the case for Claude Code payloads, which have no
    role="developer" messages at all).

    Also covers the session's *starting* mode: the very first call has no
    switch notice (the startup preamble's own <collaboration_mode> block
    sits several messages before the first real user turn, separated by
    AGENTS.md/environment_context - see _CODEX_INTERNAL_CONTEXT_RE's own
    comment for that ordering), so without this the session would silently
    start "in" a mode with no indication of which one. Detected by there
    being no assistant turn yet anywhere in `messages` - true only for a
    session's first call - in which case the first <collaboration_mode>
    block found anywhere (the preamble's) is reported instead."""
    if not isinstance(messages, list):
        return ""
    last_user_index = None
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if not isinstance(message, dict) or message.get("type", "message") != "message":
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
        last_user_index = i
        break
    if not last_user_index:
        return ""
    previous = messages[last_user_index - 1]
    if not isinstance(previous, dict) or previous.get("role") != "developer" or previous.get("type", "message") != "message":
        is_first_call = not any(
            isinstance(m, dict) and m.get("type", "message") == "message" and m.get("role") == "assistant"
            for m in messages
        )
        if not is_first_call:
            return ""
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "developer":
                continue
            match = _COLLABORATION_MODE_HEADER_RE.search(_flatten_content(message.get("content")))
            if match:
                return match.group(1)
        return ""
    match = _COLLABORATION_MODE_HEADER_RE.search(_flatten_content(previous.get("content")))
    return match.group(1) if match else ""


def _active_command_name_and_version(messages: Any) -> tuple[str, str]:
    """Walks back to the human-originated turn that started this chain of
    calls, looking for Claude Code's "<command-name>/foo</command-name>" tag
    (slash-command invocation) and an optional "<version>" marker in
    the same expanded body. Returns ("", "") for a freeform prompt.

    Also recognizes Codex CLI's "<codex_internal_context source=\"...\">"
    continuation wrapper (see _CODEX_INTERNAL_CONTEXT_RE) as a synthetic
    command named after its "source" attribute (e.g. "goal"), version
    always "" - Codex's own equivalent of this repo's Claude Code /goal
    Stop hook, re-injected as the prompt on every turn that context stays
    active. Everything else here (a real Claude Code command, or no command
    at all) still applies to Codex payloads exactly as documented - only
    this one wrapper is Codex-specific."""
    if not isinstance(messages, list):
        return "", ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue  # automatic continuation - keep walking back
        text = _flatten_content(content) if not isinstance(content, str) else content
        match = _COMMAND_NAME_RE.search(text)
        if not match:
            codex_context_match = _CODEX_INTERNAL_CONTEXT_RE.search(text)
            if codex_context_match:
                return codex_context_match.group(1), ""
            return "", ""
        version_match = _COMMAND_VERSION_RE.search(text)
        return match.group(1), (version_match.group(1) if version_match else "")
    return "", ""


def _failed_tool_call(messages: Any) -> tuple[str, str, str]:
    """Claude Code only - looks for Anthropic's explicit tool_result
    is_error flag, which Codex's Responses-API shape has no equivalent for
    (its custom_tool_call_output/function_call_output items carry no
    structured success/failure signal, just an output string), so this
    always returns ("", "", "") for a Codex payload - a failed Codex tool
    call is currently invisible to failed_tool_name/args/error.

    If the last message is a tool_result marked is_error, find its paired
    tool_use by tool_use_id and return (tool_name, arguments_json, error_text)
    - the failed invocation this call is reacting to, distinct from
    tool_name (whatever this call's own response invokes next, if anything)."""
    if not isinstance(messages, list) or not messages:
        return "", "", ""
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return "", "", ""
    content = last.get("content")
    if not isinstance(content, list):
        return "", "", ""
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error")):
            continue
        tool_use_id = block.get("tool_use_id")
        error_text = _flatten_content(block.get("content"))
        for message in reversed(messages[:-1]):
            if not isinstance(message, dict):
                continue
            inner_content = message.get("content")
            if not isinstance(inner_content, list):
                continue
            for inner_block in inner_content:
                if (
                    isinstance(inner_block, dict)
                    and inner_block.get("type") == "tool_use"
                    and inner_block.get("id") == tool_use_id
                ):
                    return (
                        inner_block.get("name", ""),
                        json.dumps(inner_block.get("input") or {}, default=str),
                        error_text,
                    )
        return "", "", error_text
    return "", "", ""


def _codex_session_id(headers: dict) -> str:
    """Codex CLI's equivalent of x-claude-code-session-id: a JSON-encoded
    header carrying session_id (stable across every turn of one Codex
    conversation, confirmed against real captures), falling back to
    thread_id for calls where session_id isn't set."""
    raw = headers.get("x-codex-turn-metadata")
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return data.get("session_id") or data.get("thread_id") or ""


def _session_and_trace_id(payload: dict) -> tuple[str, str]:
    """Shared dispatcher: tries Claude Code's session header first, then
    Codex CLI's (_codex_session_id), before falling back to trace/call id."""
    trace_id = payload.get("trace_id") or ""
    headers = ((payload.get("metadata") or {}).get("requester_custom_headers")) or {}
    session_id = (
        headers.get("x-claude-code-session-id")  # Claude Code
        or _codex_session_id(headers)  # Codex CLI
        or trace_id
        or payload.get("litellm_call_id", "")
    )
    trace_id = trace_id or session_id
    return session_id, trace_id


@dataclass
class EventContext:
    """Per-call identity/attribution fields shared by _event_row/_usage_row/
    _message_row - avoids passing the same 9 positional values through all
    three call sites (ingest_standard_logging_payload, build_event,
    reparse._reparse_one)."""
    session_id: str
    trace_id: str
    agent_name: str = ""
    agent_version: str = ""
    skill_name: str = ""
    skill_version: str = ""
    command_name: str = ""
    command_version: str = ""
    agent_invocation_id: str = ""


def _derive_context(payload: dict, messages: Any, client=None) -> EventContext:
    """Computes an EventContext for one payload. agent_name/agent_version
    need a DB round-trip (_agent_name_and_version_for_invocation), so they're
    left blank when client is None - the DB-free build_event() path, where
    ingest_events_batch() patches them in afterward once the batch's own
    invocation_rows are inserted."""
    session_id, trace_id = _session_and_trace_id(payload)
    agent_invocation_id = _agent_invocation_id(payload)
    if client is not None:
        agent_name, agent_version = _agent_name_and_version_for_invocation(client, agent_invocation_id)
    else:
        agent_name, agent_version = "", ""
    skill_name, skill_version = _skill_name_and_version(payload)
    command_name, command_version = _active_command_name_and_version(messages)
    return EventContext(
        session_id=session_id,
        trace_id=trace_id,
        agent_name=agent_name,
        agent_version=agent_version,
        skill_name=skill_name,
        skill_version=skill_version,
        command_name=command_name,
        command_version=command_version,
        agent_invocation_id=agent_invocation_id,
    )


def _split_name_version(value: str) -> tuple[str, str]:
    """Claude Code only - subagents/skills are a Claude Code concept, Codex
    CLI has neither. Splits on the last "_v" - the old naming convention
    (e.g. "test-researcher_v1.0.0"), superseded by _version_marker_for_name.
    Kept as a fallback for subagent_type values with no version marker and
    for historical rows. Agents only; skills have no such suffix convention."""
    idx = value.rfind("_v")
    if idx == -1 or idx + 2 >= len(value):
        return value, ""
    return value[:idx], value[idx + 2:]


def _flatten_messages_text(messages: Any) -> str:
    """Every message's text, joined - used to search for the "Available
    agent types"/"available skills" listings Claude Code injects, where
    <version> markers surface. Not restricted to one message since the
    listing can sit several turns back."""
    if not isinstance(messages, list):
        return ""
    parts = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        parts.append(_flatten_content(content) if not isinstance(content, str) else content)
    return "\n".join(parts)


def _version_marker_for_name(text: str, name: str, tag: str) -> str:
    """Claude Code only - agent/skill listings with these markers are
    injected by Claude Code, never by Codex CLI, so this always returns ""
    against a Codex payload. Finds "- <name>: ...<tag>version</tag>" in an
    agent/skill listing (see AGENTS.md for the marker convention) - the tag
    can sit anywhere on that line, not just immediately after "- name: ",
    since the convention puts it at the end of the description now. Takes
    the last match so a mid-session refreshed listing wins over a stale
    one. "" if no marker."""
    if not name:
        return ""
    pattern = re.compile(rf"^- {re.escape(name)}: .*?<{tag}>([^<]*)</{tag}>", re.MULTILINE)
    matches = pattern.findall(text)
    return matches[-1] if matches else ""


def _user_id(payload: dict) -> str:
    """Stable caller identity: metadata.user_api_key_user_id. Deliberately
    not user_api_key_alias, which is a renamable display name that would
    silently change what historical rows' user_id "meant". Alias is only a
    last-resort fallback so ingestion never drops a row for lack of an id."""
    metadata = payload.get("metadata") or {}
    return (
        metadata.get("user_api_key_user_id")
        or metadata.get("user_api_key_alias")
        or "unknown-user"
    )


def _user_name(payload: dict) -> str:
    """Display-only label for the user identified by _user_id, stored once
    in ai_gateway_users.user_name rather than on every fact-table row."""
    metadata = payload.get("metadata") or {}
    return (
        metadata.get("user_api_key_alias")
        or metadata.get("user_api_key_user_id")
        or "unknown-user"
    )


def _group_id(payload: dict) -> str:
    """Stable UUID of the LiteLLM Team a key belongs to (empty until Teams
    are configured). Deliberately not user_api_key_team_alias, a renamable
    display name that would silently break filters keyed on it - see
    _group_alias for the display label."""
    metadata = payload.get("metadata") or {}
    return metadata.get("user_api_key_team_id") or ""


def _group_alias(payload: dict) -> str:
    """Display-only label for the group identified by _group_id; never
    used as a filter/join key."""
    metadata = payload.get("metadata") or {}
    return metadata.get("user_api_key_team_alias") or ""


def _user_key_hash(payload: dict) -> str:
    """Stable per-key id (metadata.user_api_key_hash). Distinct from
    _user_id since one internal user can hold multiple keys."""
    metadata = payload.get("metadata") or {}
    return metadata.get("user_api_key_hash") or ""


def _user_agent(payload: dict) -> str:
    """Calling client, e.g. "claude-cli/2.1.207 (external, cli)" - latest
    value wins in ai_gateway_users (ReplacingMergeTree)."""
    metadata = payload.get("metadata") or {}
    return metadata.get("user_agent") or ""


def _agent_invocations_from_messages(messages: Any) -> list[tuple[str, str, str, str]]:
    """Claude Code only - subagent spawning (the "Agent" tool) is a Claude
    Code concept, Codex CLI has no equivalent, so this always returns []
    for a Codex payload. Scan messages for Agent tool_use blocks paired with
    the following tool_result, pulling the spawned subagent's agent_id from
    its text (e.g. "agentId: a04bd3c594bf74fb9"). agent_version comes from
    the "<version>" marker in the "Available agent types" listing,
    falling back to splitting a legacy "_v<version>" suffix off
    subagent_type (see _split_name_version). Returns (agent_id,
    subagent_type, agent_version, description) tuples, usually empty."""
    if not isinstance(messages, list):
        return []
    listing_text = _flatten_messages_text(messages)
    results = []
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Agent"):
                continue
            tool_use_id = block.get("id")
            input_ = block.get("input") or {}
            agent_id = _agent_id_from_tool_result(messages, i, tool_use_id)
            if agent_id:
                subagent_type = input_.get("subagent_type", "")
                agent_version = _version_marker_for_name(listing_text, subagent_type, "version")
                if not agent_version:
                    bare_name, suffix_version = _split_name_version(subagent_type)
                    if suffix_version:
                        subagent_type, agent_version = bare_name, suffix_version
                results.append((agent_id, subagent_type, agent_version, input_.get("description", "")))
    return results


def _agent_id_from_tool_result(messages: list, tool_use_index: int, tool_use_id: Optional[str]) -> str:
    # Claude Code only - see _agent_invocations_from_messages, its only caller.
    if tool_use_index + 1 >= len(messages):
        return ""
    next_message = messages[tool_use_index + 1]
    if not isinstance(next_message, dict):
        return ""
    content = next_message.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        if tool_use_id is not None and block.get("tool_use_id") != tool_use_id:
            continue
        match = _AGENT_ID_RE.search(_flatten_content(block.get("content")))
        if match:
            return match.group(1)
    return ""


def _response_tool_calls(payload: dict) -> list[tuple[str, dict]]:
    """Shared: Claude Code (Anthropic/chat-completions shape) and Codex CLI
    (Responses API shape). Whether this call's own completion invoked a
    tool. LiteLLM normalizes the response to an OpenAI-style tool_calls
    list, so this reads payload["response"], not "messages" (which stay
    Anthropic-shaped). Falls back to Responses-API shape (the "chatgpt"
    provider, i.e. Codex) where there's no "choices" - tool calls are
    "function_call" items in response["output"], or "custom_tool_call"
    items for Codex's freeform tools (e.g. "exec", which runs JS source,
    not JSON arguments - see the custom_tool_call branch below)."""
    response = payload.get("response") or {}
    choices = response.get("choices") or []
    calls = []
    if choices:
        message = choices[0].get("message") or {}
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = function.get("name", "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                arguments = {}
            calls.append((name, arguments))
        return calls

    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            name = item.get("name", "")
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except (TypeError, ValueError):
                arguments = {}
            calls.append((name, arguments))
        elif item_type == "custom_tool_call":
            # Codex's freeform tools (e.g. "exec") take raw source text in
            # "input", not a JSON arguments blob - surface it under "command",
            # which _tool_display_arg's key-preference chain already prefers.
            calls.append((item.get("name", ""), {"command": item.get("input", "")}))
    return calls


def _skill_name_and_version(payload: dict) -> tuple[str, str]:
    """Claude Code only - the "Skill" tool is a Claude Code concept, Codex
    CLI has no equivalent, so this always returns ("", "") for a Codex
    payload. skill_name is the bare directory name (no version suffix - see
    AGENTS.md). skill_version comes from the "<version>" marker in
    the "available skills" listing, already present in this payload's
    messages."""
    for name, arguments in _response_tool_calls(payload):
        if name == "Skill" and arguments.get("skill"):
            skill_name = arguments["skill"]
            skill_version = _version_marker_for_name(
                _flatten_messages_text(payload.get("messages")), skill_name, "version"
            )
            return skill_name, skill_version
    return "", ""


def _provider_for_model(model: str) -> str:
    if model.startswith("claude-"):
        return "claude"
    if _PROVIDER_OPENAI_RE.match(model):
        return "openai"
    return "other"


def _collapse_whitespace(text: str) -> str:
    text = _WHITESPACE_COLLAPSE_RE.sub(" ", text)
    return _BLANK_LINES_COLLAPSE_RE.sub("\n", text)


def _truncate_at_word_boundary(text: str, limit: int) -> str:
    """Caps `text` to at most `limit` characters, but never mid-word.
    Trims back to the last whitespace before the cut, dropping the trailing
    partial word/list-item entirely.
    Appends a visible '...' indicator only when truncation actually
    occurred - a shorter-than-`limit` string is returned unchanged, no
    indicator.
    Falls back to a hard cut at `limit` only when no whitespace exists
    anywhere before it (one giant unbroken token) - a hard cut is still
    better than an unbounded blob there.
    Ported into panel-76's own SQL (see agents_overview.json) for the
    analogous SQL-side truncation points (reply text, etc.) - keep both in
    sync if this changes."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + "..."


def _clean_prompt_text(prompt_text: str) -> str:
    return _SYSTEM_REMINDER_STRIP_RE.sub("", prompt_text, count=1).strip()


def _response_text(payload: dict) -> str:
    """The model's reply text - shared by _message_row and the
    transcript_handoff branch, which reads the <severity>/<block> verdict
    out of it. Falls back to Responses-API shape (the "chatgpt" provider)
    where there's no "choices" - the reply is the last assistant "message"
    item in response["output"], if any (can legitimately be empty even on a
    successful call - not every logged Responses-API call captures its own
    reply text there)."""
    response = payload.get("response") or {}
    choices = response.get("choices") or []
    if choices:
        return _flatten_content(choices[0].get("message", {}).get("content"))

    for item in reversed(response.get("output") or []):
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant":
            return _flatten_content(item.get("content"))
    return ""


def _judge_verdict(response_text: str) -> tuple[Optional[bool], str]:
    """Claude Code only - the goal-check judge call is issued by this repo's
    Claude Code /goal Stop hook; Codex CLI has no equivalent lifecycle hook,
    so a Codex payload never reaches this (see _JUDGE_CALL_PREFIX in
    _classify_event/_prompt_kind_and_display). Parses the goal-check judge's
    response, once at ingest, so the Trace panel doesn't re-parse raw JSON
    per row. Expected shape: a bare {"ok": <bool>, "reason": "<string>"}
    object."""
    try:
        data = json.loads(response_text)
    except (TypeError, ValueError):
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    ok = data.get("ok")
    reason = data.get("reason")
    return (ok if isinstance(ok, bool) else None), (reason if isinstance(reason, str) else "")


def _prompt_kind_and_display(prompt_text: str, command_name: str, response_text: str = "") -> tuple[str, str, str]:
    """Classifies the *incoming* prompt (port of panel-76's prompt_calc/
    prompt_display/prompt_final CTEs), independent of calculated_type -
    which prioritizes "did the response invoke a tool" and previously
    misclassified system-notification/judge-call prompts whose response
    happened to also call a tool as a real user prompt.

    Every non-"real"/"command" prompt_kind here (system_notification,
    judge_call, title_gen, transcript_handoff, suggestion_mode,
    stop_hook_feedback) is a Claude Code CLI/hook lifecycle convention with
    no Codex CLI equivalent - a Codex prompt never starts with any of these
    prefixes, so it always falls through to the "real" branch at the bottom
    (or "command", harmlessly, for a prompt that happens to contain a
    literal "<command-args>" tag). Not a Codex-specific bug: there's simply
    nothing here for Codex to misclassify into.

    Returns (prompt_kind, display_text, display_arg); display_arg is the
    variable "gray argument" part of the line (severity score, summary...),
    empty when a branch has none."""
    if not prompt_text:
        return "", "", ""

    cleaned = _clean_prompt_text(prompt_text)

    # _COMMAND_ARGS_RE.search is unanchored - a resent/harness-echoed blob
    # (a <transcript>-wrapped severity check, or a compaction handoff
    # summary) can contain an old <command-args> tag buried inside it, which
    # would otherwise misclassify the whole echoed continuation as a fresh
    # "command" prompt. Skip the command check for those.
    is_harness_echo = (
        _TRANSCRIPT_HANDOFF_PREFIX in prompt_text
        or "This session is being continued from a previous conversation" in prompt_text
    )
    command_args_match = None if is_harness_echo else _COMMAND_ARGS_RE.search(prompt_text)
    command_args = command_args_match.group(1) if command_args_match else ""
    if not command_args and command_name and not is_harness_echo and "</local-command-stdout>" in prompt_text:
        command_args = _LOCAL_STDOUT_STRIP_RE.sub("", prompt_text, count=1).strip()
    if not command_args and command_name and not is_harness_echo:
        # Codex CLI's <codex_internal_context> wrapper (see
        # _CODEX_INTERNAL_CONTEXT_RE) - <objective> stands in for
        # <command-args> here, whatever the context's "source" name is.
        objective_match = _OBJECTIVE_TAG_RE.search(prompt_text)
        if objective_match:
            command_args = objective_match.group(1)
    if command_args:
        return "command", f"/{command_name} {_truncate_at_word_boundary(_collapse_whitespace(command_args), _DISPLAY_TEXT_TRUNCATE)}", ""

    if prompt_text.startswith(_INTERRUPTED_PREFIX):
        stripped = _INTERRUPTED_STRIP_RE.sub("", cleaned, count=1)
        return "interrupted", f"[interrupted] {_truncate_at_word_boundary(_collapse_whitespace(stripped), _DISPLAY_TEXT_TRUNCATE)}", ""
    if prompt_text.startswith(_SUGGESTION_MODE_PREFIX):
        return "suggestion_mode", _truncate_at_word_boundary(_collapse_whitespace(cleaned), _DISPLAY_TEXT_TRUNCATE), ""
    if prompt_text.startswith(_JUDGE_CALL_PREFIX):
        return "judge_call", "[goal-check judge call]", ""
    if prompt_text.startswith(_TITLE_GEN_PREFIX):
        return "title_gen", "[title-gen call]", ""
    if prompt_text.startswith(_TRANSCRIPT_HANDOFF_PREFIX):
        severity_match = _SEVERITY_TAG_RE.search(response_text)
        block_match = _BLOCK_TAG_RE.search(response_text)
        arg_parts = []
        if severity_match:
            arg_parts.append(f"{severity_match.group(1)}/100")
        if block_match:
            arg_parts.append(f"block: {block_match.group(1).lower()}")
        return "transcript_handoff", "[background] severity check", ", ".join(arg_parts)
    if prompt_text.startswith(_SYSTEM_NOTIFICATION_PREFIX):
        idx = cleaned.find("\n\n")
        tail = (cleaned[idx + 2:] if idx >= 0 else cleaned[1:]).strip()
        summary_match = _SUMMARY_TAG_RE.search(tail)
        if summary_match:
            status_match = _STATUS_TAG_RE.search(tail)
            status_text = status_match.group(1) if status_match else ""
            summary_text = _truncate_at_word_boundary(_collapse_whitespace(summary_match.group(1)), _DISPLAY_TEXT_TRUNCATE)
            return "system_notification", f"[background] {status_text}", summary_text
        if tail:
            return "system_notification", f"[background] {_truncate_at_word_boundary(_collapse_whitespace(tail), _DISPLAY_TEXT_TRUNCATE)}", ""
        return "system_notification", "[background check]", ""
    if prompt_text.startswith(_WEBPAGE_CONTENT_PREFIX):
        excerpt = _truncate_at_word_boundary(_collapse_whitespace(cleaned), _WEBPAGE_DISPLAY_TRUNCATE)
        return "webpage_content", f"{excerpt}...", ""
    if prompt_text.startswith(_STOP_HOOK_FEEDBACK_PREFIX):
        return "stop_hook_feedback", "[background] stop hook feedback", ""
    if prompt_text.startswith(_AWAY_RECAP_PREFIX):
        return "away_recap", "[background] away recap", ""

    return "real", _truncate_at_word_boundary(_collapse_whitespace(cleaned), _DISPLAY_TEXT_TRUNCATE), ""


def _tool_display_arg(calculated_type: str, calculated_payload: dict) -> str:
    """Ports tool_render's 8-branch preference chain to Python, run once at
    ingest instead of per dashboard row over JSON text."""
    if calculated_type == "skill_call":
        args = calculated_payload.get("args", "")
        return args if isinstance(args, str) else json.dumps(args, default=str)
    if calculated_type != "tool_call":
        return ""
    tools = calculated_payload.get("tools") or []
    if not tools:
        return ""
    args = tools[0].get("args") or {}
    if not isinstance(args, dict):
        return str(args)
    for key in _TOOL_ARG_KEY_PREFERENCE:
        value = args.get(key)
        if value:
            return f"task_id: {value}" if key == "task_id" else str(value)
    return json.dumps(args, default=str)


def _error_type(payload: dict) -> str:
    """Decodes the provider's error type (e.g. "rate_limit_error") from
    error_information.error_message once at ingest, instead of a JOIN +
    JSONExtractString per failed row in the Trace panel."""
    error_message = (payload.get("error_information") or {}).get("error_message")
    if isinstance(error_message, str):
        try:
            error_message = json.loads(error_message)
        except (TypeError, ValueError):
            return ""
    if not isinstance(error_message, dict):
        return ""
    return (error_message.get("error") or {}).get("type", "")


def _classify_event(payload: dict) -> tuple[str, dict]:
    """The calculated_type/calculated_payload dispatcher. Priority: did
    this call's response invoke a tool (category A), else what did the
    triggering prompt look like (category B, port of panel-76's startsWith
    chain). 'unknown' is a searchable bucket, not an error.

    Shared vs CLI-specific outcomes: "tool_call"/"llm_answer"/"unknown" (and
    "ask_user_question", which Codex CLI also exposes) apply to both CLIs.
    "agent_spawn" and "skill_call" only ever fire for Claude Code (see
    _agent_invocations_from_messages/_skill_name_and_version) - a Codex
    payload's first tool call, if any, always lands in the generic
    "tool_call" branch instead. Likewise every category-B kind besides
    "real"/"command" is Claude Code-only (see _prompt_kind_and_display)."""
    tool_calls = _response_tool_calls(payload)
    if tool_calls:
        first_name, first_args = tool_calls[0]
        if first_name == "Agent":
            listing_text = _flatten_messages_text(payload.get("messages"))
            subagent_type = first_args.get("subagent_type", "")
            return "agent_spawn", {
                "subagent_type": subagent_type,
                "agent_version": _version_marker_for_name(listing_text, subagent_type, "version"),
                "description": first_args.get("description", ""),
            }
        if first_name == "Skill":
            return "skill_call", {"skill": first_args.get("skill", ""), "args": first_args.get("args", "")}
        if first_name == "AskUserQuestion":
            return "ask_user_question", {"questions": first_args.get("questions", [])}
        return "tool_call", {"tools": [{"tool": name, "args": args} for name, args in tool_calls]}

    prompt_text = _last_user_text(payload.get("messages"))
    if prompt_text.startswith(_JUDGE_CALL_PREFIX):
        # TODO: structured confidence/reasoning extraction; excerpt only for now.
        return "judge_call", {"prompt_excerpt": prompt_text[:500]}
    if prompt_text.startswith(_SYSTEM_NOTIFICATION_PREFIX):
        return "system_notification", {}
    if prompt_text.startswith(_SUGGESTION_MODE_PREFIX):
        return "suggestion_mode", {}
    if prompt_text.startswith(_TRANSCRIPT_HANDOFF_PREFIX):
        return "transcript_handoff", {}
    if prompt_text.startswith(_TITLE_GEN_PREFIX):
        return "title_gen", {}
    if prompt_text.startswith(_INTERRUPTED_PREFIX):
        return "interrupted", {}
    if prompt_text.startswith(_WEBPAGE_CONTENT_PREFIX):
        return "webpage_content", {}
    if prompt_text:
        # Text already lives in agent_messages.response_text; don't duplicate it.
        return "llm_answer", {}
    return "unknown", {}


def _source_row(payload: dict, session_id: str, now: datetime) -> list:
    """The ingest_raw row: full untouched original payload, written once
    per call so reparse.py can recompute calculated_type/provider without
    needing .capture/*.json. Uses fastjson (orjson-backed), not the stdlib
    json this module otherwise uses everywhere else - this is the one place
    re-serializing the *entire* ~360KB-1.5MB payload, not a small
    substructure, so it's the one worth the faster encoder (see AGENTS.md
    "Why a queue in front of ClickHouse")."""
    return [
        payload.get("litellm_call_id", ""),
        session_id,
        now,
        fastjson.dumps(payload, default=str).decode(),
    ]


def _first_tool_call_name(payload: dict) -> str:
    """The tool the model invoked this turn (e.g. "Agent", "Skill", "Read"),
    or "" for a plain text reply."""
    calls = _response_tool_calls(payload)
    return calls[0][0] if calls else ""


def _agent_invocation_id(payload: dict) -> str:
    headers = ((payload.get("metadata") or {}).get("requester_custom_headers")) or {}
    return headers.get("x-claude-code-agent-id", "")


def _agent_name_and_version_for_invocation(client, agent_invocation_id: str) -> tuple[str, str]:
    """Best-effort lookup - blank if the parent's Agent tool_use/tool_result
    hasn't been ingested yet (e.g. subagent's first call raced ahead of it)."""
    if not agent_invocation_id:
        return "", ""
    try:
        result = client.query(
            "SELECT subagent_type, agent_version FROM agent_invocations WHERE agent_id = {agent_id:String} "
            "ORDER BY spawned_at DESC LIMIT 1",
            parameters={"agent_id": agent_invocation_id},
        )
        rows = result.result_rows
        return (rows[0][0], rows[0][1]) if rows else ("", "")
    except Exception:
        logger.exception("failed to resolve agent_invocation_id=%s", agent_invocation_id)
        return "", ""


_INVOCATION_COLUMNS = ["agent_id", "session_id", "subagent_type", "agent_version", "description", "spawned_at"]
_EVENT_COLUMNS = [
    "timestamp", "user_id", "group_id", "user_key_hash", "session_id", "trace_id",
    "turn_id", "event_type", "tool_name", "agent_name",
    "agent_version", "skill_name", "skill_version", "command_name",
    "command_version", "agent_invocation_id", "status", "latency_ms",
    "failed_tool_name", "failed_tool_args", "failed_tool_error",
    "litellm_call_id", "event_client_id", "calculated_type", "calculated_payload", "ingested_at",
]
_GROUP_COLUMNS = ["group_id", "group_name", "updated_at"]
_USER_COLUMNS = ["user_id", "group_id", "user_name", "updated_at"]
_CLIENT_COLUMNS = ["id", "value", "updated_at"]
_USAGE_COLUMNS = [
    "timestamp", "user_id", "group_id", "user_key_hash", "session_id", "trace_id", "turn_id", "model",
    "agent_name", "agent_version", "skill_name", "skill_version",
    "command_name", "command_version", "agent_invocation_id", "mcp_tool_name",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "stop_reason",
    "cache_creation_1h_tokens", "cache_creation_5m_tokens",
    "cost", "input_cost", "output_cost", "cache_hit", "ttft_ms",
    "litellm_call_id", "provider", "ingested_at",
]
_MESSAGE_COLUMNS = [
    "timestamp", "user_id", "group_id", "user_key_hash", "session_id", "trace_id", "turn_id",
    "agent_name", "agent_version", "skill_name", "skill_version",
    "command_name", "command_version", "agent_invocation_id", "prompt_text", "response_text",
    "litellm_call_id", "ingested_at",
]
_SOURCE_COLUMNS = ["litellm_call_id", "session_id", "ingested_at", "raw_payload_full"]
_GIT_BRANCH_COLUMNS = ["session_id", "git_branch", "git_repo", "issue_id", "captured_at"]
_PLAN_PROPOSAL_COLUMNS = ["session_id", "plan_text", "captured_at"]
_FAILURE_COLUMNS = ["occurred_at", "stage", "error", "litellm_call_id", "session_id", "raw_row"]
_LITELLM_ALERT_COLUMNS = [
    "received_at", "event", "event_group", "key_alias", "team_id", "user_id",
    "spend", "max_budget", "event_message", "raw_payload",
]

_INVOCATION_SPAWNED_AT_IDX = _INVOCATION_COLUMNS.index("spawned_at")
_EVENT_TIMESTAMP_IDX = _EVENT_COLUMNS.index("timestamp")
_EVENT_AGENT_NAME_IDX = _EVENT_COLUMNS.index("agent_name")
_EVENT_AGENT_VERSION_IDX = _EVENT_COLUMNS.index("agent_version")
_EVENT_CLIENT_ID_IDX = _EVENT_COLUMNS.index("event_client_id")
_USAGE_TIMESTAMP_IDX = _USAGE_COLUMNS.index("timestamp")
_USAGE_AGENT_NAME_IDX = _USAGE_COLUMNS.index("agent_name")
_USAGE_AGENT_VERSION_IDX = _USAGE_COLUMNS.index("agent_version")
_MESSAGE_TIMESTAMP_IDX = _MESSAGE_COLUMNS.index("timestamp")
_MESSAGE_AGENT_NAME_IDX = _MESSAGE_COLUMNS.index("agent_name")
_MESSAGE_AGENT_VERSION_IDX = _MESSAGE_COLUMNS.index("agent_version")
_SOURCE_INGESTED_AT_IDX = _SOURCE_COLUMNS.index("ingested_at")
_GROUP_UPDATED_AT_IDX = _GROUP_COLUMNS.index("updated_at")
_USER_UPDATED_AT_IDX = _USER_COLUMNS.index("updated_at")
_EVENT_INGESTED_AT_IDX = _EVENT_COLUMNS.index("ingested_at")
_USAGE_INGESTED_AT_IDX = _USAGE_COLUMNS.index("ingested_at")
_MESSAGE_INGESTED_AT_IDX = _MESSAGE_COLUMNS.index("ingested_at")
_EVENT_CALL_ID_IDX = _EVENT_COLUMNS.index("litellm_call_id")
_EVENT_SESSION_ID_IDX = _EVENT_COLUMNS.index("session_id")
_USAGE_CALL_ID_IDX = _USAGE_COLUMNS.index("litellm_call_id")
_USAGE_SESSION_ID_IDX = _USAGE_COLUMNS.index("session_id")
_MESSAGE_CALL_ID_IDX = _MESSAGE_COLUMNS.index("litellm_call_id")
_MESSAGE_SESSION_ID_IDX = _MESSAGE_COLUMNS.index("session_id")


def _agent_invocation_rows(session_id: str, messages: Any, now: Optional[datetime] = None) -> list[list]:
    now = now or datetime.now(timezone.utc)
    invocations = _agent_invocations_from_messages(messages)
    return [
        [agent_id, session_id, subagent_type, agent_version, description, now]
        for agent_id, subagent_type, agent_version, description in invocations
    ]


def _insert_agent_invocations(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("agent_invocations", rows, column_names=_INVOCATION_COLUMNS)


def _insert_event(client, row: list) -> None:
    client.insert("agent_events", [row], column_names=_EVENT_COLUMNS)


def _insert_usage(client, row: list) -> None:
    client.insert("agent_usage", [row], column_names=_USAGE_COLUMNS)


def _insert_message(client, row: list) -> None:
    client.insert("agent_messages", [row], column_names=_MESSAGE_COLUMNS)


def _insert_source(client, row: list) -> None:
    client.insert("ingest_raw", [row], column_names=_SOURCE_COLUMNS)


def _insert_git_branch(client, row: list) -> None:
    client.insert("session_git_branch", [row], column_names=_GIT_BRANCH_COLUMNS)


def _insert_plan_proposal(client, row: list) -> None:
    client.insert("plan_proposals", [row], column_names=_PLAN_PROPOSAL_COLUMNS)


def _insert_litellm_alert(client, row: list) -> None:
    client.insert("litellm_alerts", [row], column_names=_LITELLM_ALERT_COLUMNS)


def _group_row(payload: dict, now: Optional[datetime] = None) -> Optional[list]:
    """ai_gateway_groups row for this payload's group, or None when the
    call has no group_id (LiteLLM Teams not configured - see _group_id)."""
    group_id = _group_id(payload)
    if not group_id:
        return None
    return [group_id, _group_alias(payload), now or datetime.now(timezone.utc)]


def _user_row(payload: dict, now: Optional[datetime] = None) -> Optional[list]:
    """ai_gateway_users row for this payload's caller. user_id is never
    empty (_user_id always falls back to "unknown-user"), so this only
    returns None if somehow called on a payload with no metadata at all -
    kept Optional for symmetry with _group_row."""
    user_id = _user_id(payload)
    if not user_id:
        return None
    return [
        user_id, _group_id(payload), _user_name(payload),
        now or datetime.now(timezone.utc),
    ]


def _insert_ai_gateway_groups(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("ai_gateway_groups", rows, column_names=_GROUP_COLUMNS)


def _insert_ai_gateway_users(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("ai_gateway_users", rows, column_names=_USER_COLUMNS)


def _resolve_client_id(client, user_agent: str) -> int:
    """Best-effort id lookup for a calling-client user-agent string (e.g.
    "claude-cli/2.1.207 (external, cli)"). The hash is always computed by
    ClickHouse itself (cityHash64), never reimplemented in Python, so this
    can never diverge from the one-off backfill SQL run separately against
    ingest_raw. 0 (unknown) on any failure or empty input - never blocks
    ingestion."""
    if not user_agent:
        return 0
    try:
        result = client.query("SELECT cityHash64({v:String})", parameters={"v": user_agent})
        return result.result_rows[0][0]
    except Exception:
        logger.exception("failed to resolve client id for user_agent=%s", user_agent)
        return 0


def _insert_clients(client, rows: list[list]) -> None:
    if not rows:
        return
    client.insert("clients", rows, column_names=_CLIENT_COLUMNS)


def _event_row(payload: dict, ctx: EventContext, now: Optional[datetime] = None) -> list:
    start_time = payload.get("startTime")
    end_time = payload.get("endTime")
    # NOT payload["response_time"]: for streamed calls that's time-to-first-
    # token (~1-3ms), not total duration.
    latency_ms = (
        int((end_time - start_time) * 1000)
        if isinstance(start_time, (int, float)) and isinstance(end_time, (int, float))
        else None
    )
    # LiteLLM occasionally reports endTime < startTime on streamed calls;
    # a negative value can't pack into UInt32 and used to crash the insert.
    if latency_ms is not None and latency_ms < 0:
        latency_ms = None
    # Blank (not call_type) for a plain text reply - callers filter on
    # tool_name != '' to mean "was a tool call".
    tool_name = _first_tool_call_name(payload)
    failed_tool_name, failed_tool_args, failed_tool_error = _failed_tool_call(payload.get("messages"))
    calculated_type, calculated_payload = _classify_event(payload)

    response_text = _response_text(payload)
    prompt_text_raw = _last_user_text(payload.get("messages"))
    prompt_kind, display_text, display_arg = _prompt_kind_and_display(
        prompt_text_raw, ctx.command_name, response_text
    )
    if prompt_kind:
        calculated_payload["prompt_kind"] = prompt_kind
        calculated_payload["display_text"] = display_text
        if display_arg:
            calculated_payload["display_arg"] = display_arg
        if prompt_kind == "transcript_handoff":
            block_match = _BLOCK_TAG_RE.search(response_text)
            if block_match:
                calculated_payload["severity_check_block"] = block_match.group(1).lower()
        if prompt_kind == "judge_call":
            judge_ok, judge_reason = _judge_verdict(response_text)
            if judge_ok is not None:
                calculated_payload["judge_ok"] = judge_ok
            if judge_reason:
                calculated_payload["judge_reason"] = judge_reason
        if prompt_kind == "stop_hook_feedback":
            reason_match = _STOP_HOOK_REASON_RE.match(prompt_text_raw)
            if reason_match:
                calculated_payload["stop_hook_reason"] = reason_match.group(1).strip()
    tool_display_arg = _tool_display_arg(calculated_type, calculated_payload)
    if tool_display_arg:
        calculated_payload["tool_display_arg"] = tool_display_arg
    if calculated_type == "agent_spawn":
        subagent_type = calculated_payload.get("subagent_type") or "?"
        calculated_payload["agent_display_name"] = subagent_type.split("_")[0]
    if payload.get("status") == "failure":
        error_type = _error_type(payload)
        if error_type:
            calculated_payload["error_type"] = error_type
    collaboration_mode_change = _codex_collaboration_mode_change(payload.get("messages"))
    if collaboration_mode_change:
        calculated_payload["collaboration_mode_change"] = collaboration_mode_change

    return [
        _to_dt(payload.get("endTime") or payload.get("startTime")),
        _user_id(payload),
        _group_id(payload),
        _user_key_hash(payload),
        ctx.session_id,
        ctx.trace_id,
        0,  # turn_id: unknown from this source
        "litellm_call",
        tool_name,
        ctx.agent_name,
        ctx.agent_version,
        ctx.skill_name,
        ctx.skill_version,
        ctx.command_name,
        ctx.command_version,
        ctx.agent_invocation_id,
        payload.get("status", ""),
        latency_ms,
        failed_tool_name,
        failed_tool_args,
        failed_tool_error,
        payload.get("litellm_call_id", ""),
        0,  # event_client_id: patched in by ingest_events_batch() once the
            # calling client's user_agent has been resolved to an id - see
            # _resolve_client_id (build_event() has no DB client to do it here).
        calculated_type,
        json.dumps(calculated_payload, default=str),
        now or datetime.now(timezone.utc),
    ]


def _usage_row(payload: dict, ctx: EventContext, now: Optional[datetime] = None) -> Optional[list]:
    response = payload.get("response") or {}
    usage = response.get("usage") or (payload.get("metadata") or {}).get("usage_object") or {}
    prompt_tokens = payload.get("prompt_tokens") or usage.get("prompt_tokens") or 0
    completion_tokens = payload.get("completion_tokens") or usage.get("completion_tokens") or 0
    if not prompt_tokens and not completion_tokens:
        return None  # nothing billable to record (e.g. a rejected-before-call failure)

    prompt_details = usage.get("prompt_tokens_details") or {}
    ephemeral = prompt_details.get("cache_creation_token_details") or {}
    choices = response.get("choices") or []
    stop_reason = (choices[0].get("finish_reason") if choices else "") or ""

    # Responses-API shape (the "chatgpt" provider) has no top-level
    # cache_creation_input_tokens/cache_read_input_tokens - its cache hit
    # lives in usage.prompt_tokens_details instead, and payload["cache_hit"]
    # is left None rather than False, so a missing flag with cached tokens
    # present still counts as a hit. response_cost/cost_breakdown stay 0 for
    # this provider regardless (no LiteLLM pricing entry registered yet).
    cache_read_tokens = usage.get("cache_read_input_tokens") or prompt_details.get("cached_tokens") or 0
    cache_creation_tokens = (
        usage.get("cache_creation_input_tokens")
        or prompt_details.get("cache_write_tokens")
        or prompt_details.get("cache_creation_tokens")
        or 0
    )
    cache_hit_flag = payload.get("cache_hit")
    cache_hit = 1 if (cache_hit_flag or (cache_hit_flag is None and cache_read_tokens)) else 0

    completion_start = payload.get("completionStartTime")
    start_time = payload.get("startTime")
    ttft_ms = (
        int((completion_start - start_time) * 1000)
        if isinstance(completion_start, (int, float)) and isinstance(start_time, (int, float))
        else 0
    )
    # Same LiteLLM streaming quirk as latency_ms above (completionStartTime
    # occasionally precedes startTime) - a negative value can't pack into
    # UInt32 and used to crash the whole batch's insert.
    if ttft_ms < 0:
        ttft_ms = 0

    called_tool = _first_tool_call_name(payload)
    mcp_tool_name = called_tool if called_tool.startswith("mcp__") else ""
    cost_breakdown = payload.get("cost_breakdown") or {}
    model = payload.get("model_group") or payload.get("model", "")

    return [
        _to_dt(payload.get("endTime") or payload.get("startTime")),
        _user_id(payload),
        _group_id(payload),
        _user_key_hash(payload),
        ctx.session_id,
        ctx.trace_id,
        0,  # turn_id: unknown from this source
        model,
        ctx.agent_name,
        ctx.agent_version,
        ctx.skill_name,
        ctx.skill_version,
        ctx.command_name,
        ctx.command_version,
        ctx.agent_invocation_id,
        mcp_tool_name,
        prompt_tokens,
        completion_tokens,
        cache_creation_tokens,
        cache_read_tokens,
        stop_reason,
        ephemeral.get("ephemeral_1h_input_tokens") or 0,
        ephemeral.get("ephemeral_5m_input_tokens") or 0,
        payload.get("response_cost") or 0,
        cost_breakdown.get("input_cost") or 0,
        cost_breakdown.get("output_cost") or 0,
        cache_hit,
        ttft_ms,
        payload.get("litellm_call_id", ""),
        _provider_for_model(model),
        now or datetime.now(timezone.utc),
    ]


def _message_row(payload: dict, ctx: EventContext, now: Optional[datetime] = None) -> Optional[list]:
    response_text = _response_text(payload)
    prompt_text = _last_user_text(payload.get("messages"))
    if not prompt_text and not response_text:
        return None

    return [
        _to_dt(payload.get("endTime") or payload.get("startTime")),
        _user_id(payload),
        _group_id(payload),
        _user_key_hash(payload),
        ctx.session_id,
        ctx.trace_id,
        0,  # turn_id: unknown from this source
        ctx.agent_name,
        ctx.agent_version,
        ctx.skill_name,
        ctx.skill_version,
        ctx.command_name,
        ctx.command_version,
        ctx.agent_invocation_id,
        prompt_text,
        response_text,
        payload.get("litellm_call_id", ""),
        now or datetime.now(timezone.utc),
    ]


def ingest_standard_logging_payload(payload: dict) -> None:
    """Insert one LiteLLM StandardLoggingPayload into ClickHouse. Never
    raises - LiteLLM would retry a payload forever if this broke the ack."""
    session_id = trace_id = ""
    try:
        client = get_client()
        now = datetime.now(timezone.utc)

        messages = payload.get("messages")
        session_id, trace_id = _session_and_trace_id(payload)
        # Insert this call's own spawned-subagent rows before doing
        # _derive_context's DB lookup below (for *this* call's own
        # agent_invocation_id) - minimizes the race window before the
        # spawned subagent's own first call arrives and looks its row up.
        _insert_agent_invocations(client, _agent_invocation_rows(session_id, messages, now=now))

        ctx = _derive_context(payload, messages, client=client)

        _insert_source(client, _source_row(payload, session_id, now))

        _insert_event(client, _event_row(payload, ctx, now))

        if payload.get("status") == "success":
            usage_row = _usage_row(payload, ctx, now)
            if usage_row is not None:
                _insert_usage(client, usage_row)

            message_row = _message_row(payload, ctx, now)
            if message_row is not None:
                _insert_message(client, message_row)
    except Exception:
        logger.exception(
            "failed to ingest LiteLLM payload into ClickHouse "
            "(litellm_call_id=%s trace_id=%s session_id=%s status=%s call_type=%s)",
            payload.get("litellm_call_id", ""), trace_id, session_id,
            payload.get("status", ""), payload.get("call_type", ""),
        )


_ISSUE_ID_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9}-\d+)(?![A-Za-z0-9])")


def _issue_id_from_branch(git_branch: str) -> str:
    """Ticket ID embedded in a branch name, e.g. "VIEW-12345", matched
    case-insensitively and uppercased. Trailing boundary is a negative
    lookahead, not \\b: \\b treats digit/underscore as the same word class
    and would miss "VIEW-100500_my-branch"."""
    match = _ISSUE_ID_RE.search(git_branch or "")
    return match.group(1).upper() if match else ""


def ingest_git_branch(session_id: str, git_branch: str, git_repo: str = "") -> None:
    """Insert a session's git branch/repo (hooks/report_git_branch.py).
    Never raises - a tracking failure must not surface to the CLI session."""
    try:
        client = get_client()
        issue_id = _issue_id_from_branch(git_branch)
        _insert_git_branch(client, [session_id, git_branch, git_repo, issue_id, datetime.now(timezone.utc)])
    except Exception:
        logger.exception("failed to ingest git branch (session_id=%s)", session_id)


def ingest_plan_proposal(session_id: str, plan_text: str) -> None:
    """Insert an ExitPlanMode call's plan text (hooks/report_plan_proposal.py).
    Never raises - a tracking failure must not surface to the CLI session."""
    try:
        client = get_client()
        _insert_plan_proposal(client, [session_id, plan_text, datetime.now(timezone.utc)])
    except Exception:
        logger.exception("failed to ingest plan proposal (session_id=%s)", session_id)


def ingest_litellm_alert(payload: dict) -> None:
    """Insert one LiteLLM native-alerting webhook event (budget/outage/
    exception/hang signals - see general_settings.alerting in
    services/litellm/config.yaml). Direct-to-ClickHouse, not queued through
    Redis: these events are rare compared to per-call metrics traffic, same
    low-volume pattern as ingest_git_branch/ingest_plan_proposal. Never
    raises - a tracking failure must not surface to LiteLLM's own retry
    logic for its alerting webhook.

    Only the budget-event shape is fully documented by LiteLLM's own docs -
    other alert types (llm_exceptions/outage_alerts/db_exceptions/...)
    likely carry different fields, so raw_payload keeps the full body
    regardless of which fields above happen to be present."""
    try:
        client = get_client()
        _insert_litellm_alert(client, [
            datetime.now(timezone.utc),
            payload.get("event") or "",
            payload.get("event_group") or "",
            payload.get("key_alias") or "",
            payload.get("team_id") or "",
            payload.get("user_id") or "",
            payload.get("spend"),
            payload.get("max_budget"),
            payload.get("event_message") or "",
            json.dumps(payload, default=str),
        ])
    except Exception:
        logger.exception("failed to ingest LiteLLM alert (event=%s)", payload.get("event", ""))


def ingest_webhook_body(body: Any) -> None:
    """Usually a list of StandardLoggingPayload dicts (log_format:
    json_array), but tolerate a single dict too."""
    payloads = body if isinstance(body, list) else [body]
    for payload in payloads:
        if isinstance(payload, dict):
            ingest_standard_logging_payload(payload)


def _serialize_row(row: Optional[list], timestamp_idx: int) -> Optional[list]:
    if row is None:
        return None
    row = list(row)
    row[timestamp_idx] = row[timestamp_idx].isoformat()
    return row


def _serialize_row_multi(row: Optional[list], *timestamp_indices: int) -> Optional[list]:
    """Serialize multiple datetime fields in a row by index."""
    if row is None:
        return None
    row = list(row)
    for idx in timestamp_indices:
        if idx < len(row) and isinstance(row[idx], datetime):
            row[idx] = row[idx].isoformat()
    return row


def _deserialize_row(row: Optional[list], timestamp_idx: int) -> Optional[list]:
    if row is None:
        return None
    row = list(row)
    row[timestamp_idx] = datetime.fromisoformat(row[timestamp_idx])
    return row


def _deserialize_row_multi(row: Optional[list], *timestamp_indices: int) -> Optional[list]:
    """Deserialize multiple datetime fields in a row by index."""
    if row is None:
        return None
    row = list(row)
    for idx in timestamp_indices:
        if idx < len(row) and isinstance(row[idx], str):
            row[idx] = datetime.fromisoformat(row[idx])
    return row


def build_event(payload: dict) -> dict:
    """The DB-free half of ingesting one StandardLoggingPayload: pure
    functions only, no ClickHouse round-trip. Called in webhook-worker
    (worker.py's _decode_into), not on webhook's own request path - webhook
    only XADDs the raw payload onto Redis unmodified, so its hot path never
    pays for this CPU-bound parsing (regex classification, message
    scanning, JSON work).

    Includes source_row (full untouched payload) alongside the compact
    per-table rows; Redis MAXLEN/mem_limit are sized around this larger
    per-event footprint (see config.yml).

    agent_name/agent_version are left blank - resolving them needs a SELECT
    against agent_invocations, which only makes sense once this batch's own
    invocation_rows are inserted; ingest_events_batch() patches them in.

    Never raises internally - lets worker.py's _decode_into decide whether
    one bad payload drops just that item or the whole batch.
    """
    messages = payload.get("messages")
    now = datetime.now(timezone.utc)
    ctx = _derive_context(payload, messages)  # no client - agent_name/version stay blank
    invocation_rows = _agent_invocation_rows(ctx.session_id, messages, now=now)

    source_row = _source_row(payload, ctx.session_id, now)

    event_row = _event_row(payload, ctx, now)

    usage_row = None
    message_row = None
    if payload.get("status") == "success":
        usage_row = _usage_row(payload, ctx, now)
        message_row = _message_row(payload, ctx, now)

    return {
        "agent_invocation_id": ctx.agent_invocation_id,
        # Raw calling-client identity - resolved to event_client_id by
        # ingest_events_batch() (needs a DB round trip via _resolve_client_id,
        # which this DB-free function can't do).
        "user_agent": _user_agent(payload),
        "invocation_rows": [
            _serialize_row(row, _INVOCATION_SPAWNED_AT_IDX) for row in invocation_rows
        ],
        "source_row": _serialize_row(source_row, _SOURCE_INGESTED_AT_IDX),
        "event_row": _serialize_row_multi(event_row, _EVENT_TIMESTAMP_IDX, _EVENT_INGESTED_AT_IDX),
        "usage_row": _serialize_row_multi(usage_row, _USAGE_TIMESTAMP_IDX, _USAGE_INGESTED_AT_IDX),
        "message_row": _serialize_row_multi(message_row, _MESSAGE_TIMESTAMP_IDX, _MESSAGE_INGESTED_AT_IDX),
        "group_row": _serialize_row(_group_row(payload, now), _GROUP_UPDATED_AT_IDX),
        "user_row": _serialize_row(_user_row(payload, now), _USER_UPDATED_AT_IDX),
    }


def ingest_events_batch(events: list[dict]) -> None:
    """Runs in webhook-worker. Writes a batch of build_event() outputs with
    exactly one client.insert() per table (vs up-to-4-per-payload in the
    old synchronous path) - the batching that takes load off ClickHouse
    under concurrent traffic.

    Never raises - the caller still needs to XACK/retry the batch's message
    ids regardless of a malformed event.
    """
    if not events:
        return
    try:
        writer = _BatchWriter(get_client())
        writer.write_dimensions(events)
        event_rows, usage_rows, message_rows = writer.patch_fact_rows(events)
        for table, rows, columns, call_id_idx, session_id_idx in (
            ("agent_events", event_rows, _EVENT_COLUMNS, _EVENT_CALL_ID_IDX, _EVENT_SESSION_ID_IDX),
            ("agent_usage", usage_rows, _USAGE_COLUMNS, _USAGE_CALL_ID_IDX, _USAGE_SESSION_ID_IDX),
            ("agent_messages", message_rows, _MESSAGE_COLUMNS, _MESSAGE_CALL_ID_IDX, _MESSAGE_SESSION_ID_IDX),
        ):
            writer.insert_with_dlq_fallback(table, rows, columns, call_id_idx, session_id_idx)
    except Exception:
        logger.exception("failed to ingest event batch (n=%d)", len(events))


class _BatchWriter:
    """Owns the per-batch state ingest_events_batch() needs across its three
    concerns - dimension rows, per-event agent/client resolution caches, and
    per-table insert-with-DLQ-fallback - so ingest_events_batch() itself
    stays a plain call sequence."""

    def __init__(self, client):
        self.client = client
        self._agent_fields_cache: dict[str, tuple[str, str]] = {}
        self._client_id_cache: dict[str, int] = {}

    def write_dimensions(self, events: list[dict]) -> None:
        """Inserts agent_invocations, ingest_raw, and the (deduped)
        ai_gateway_groups/ai_gateway_users rows for this batch."""
        client = self.client

        invocation_rows = [
            _deserialize_row(row, _INVOCATION_SPAWNED_AT_IDX)
            for event in events
            for row in (event.get("invocation_rows") or [])
        ]
        _insert_agent_invocations(client, invocation_rows)

        source_rows = [
            row for event in events
            if (row := _deserialize_row(event.get("source_row"), _SOURCE_INGESTED_AT_IDX)) is not None
        ]
        if source_rows:
            client.insert("ingest_raw", source_rows, column_names=_SOURCE_COLUMNS)

        # Dedup by id within the batch (last wins); ReplacingMergeTree would
        # collapse duplicates on merge anyway, this just skips redundant inserts.
        group_rows_by_id: dict[str, list] = {}
        for event in events:
            row = _deserialize_row(event.get("group_row"), _GROUP_UPDATED_AT_IDX)
            if row is not None:
                group_rows_by_id[row[0]] = row
        _insert_ai_gateway_groups(client, list(group_rows_by_id.values()))

        user_rows_by_id: dict[str, list] = {}
        for event in events:
            row = _deserialize_row(event.get("user_row"), _USER_UPDATED_AT_IDX)
            if row is not None:
                user_rows_by_id[row[0]] = row
        _insert_ai_gateway_users(client, list(user_rows_by_id.values()))

    def _agent_fields(self, agent_invocation_id: str) -> tuple[str, str]:
        if not agent_invocation_id:
            return "", ""
        if agent_invocation_id not in self._agent_fields_cache:
            self._agent_fields_cache[agent_invocation_id] = _agent_name_and_version_for_invocation(
                self.client, agent_invocation_id
            )
        return self._agent_fields_cache[agent_invocation_id]

    def _client_id(self, user_agent: str) -> int:
        if not user_agent:
            return 0
        if user_agent not in self._client_id_cache:
            self._client_id_cache[user_agent] = _resolve_client_id(self.client, user_agent)
        return self._client_id_cache[user_agent]

    def patch_fact_rows(self, events: list[dict]) -> tuple[list[list], list[list], list[list]]:
        """Resolves each event's agent_name/version and event_client_id
        (both needed a DB round trip build_event() couldn't do), patches
        them into its event/usage/message rows, and inserts the resolved
        `clients` rows. Returns (event_rows, usage_rows, message_rows)."""
        client_rows_by_id: dict[int, list] = {}
        now = datetime.now(timezone.utc)
        event_rows, usage_rows, message_rows = [], [], []

        for event in events:
            agent_name, agent_version = self._agent_fields(event.get("agent_invocation_id") or "")

            user_agent = event.get("user_agent") or ""
            client_id = self._client_id(user_agent)
            if client_id:
                client_rows_by_id[client_id] = [client_id, user_agent, now]

            event_row = _deserialize_row_multi(event.get("event_row"), _EVENT_TIMESTAMP_IDX, _EVENT_INGESTED_AT_IDX)
            if event_row is not None:
                event_row[_EVENT_AGENT_NAME_IDX] = agent_name
                event_row[_EVENT_AGENT_VERSION_IDX] = agent_version
                event_row[_EVENT_CLIENT_ID_IDX] = client_id
                event_rows.append(event_row)

            usage_row = _deserialize_row_multi(event.get("usage_row"), _USAGE_TIMESTAMP_IDX, _USAGE_INGESTED_AT_IDX)
            if usage_row is not None:
                usage_row[_USAGE_AGENT_NAME_IDX] = agent_name
                usage_row[_USAGE_AGENT_VERSION_IDX] = agent_version
                usage_rows.append(usage_row)

            message_row = _deserialize_row_multi(event.get("message_row"), _MESSAGE_TIMESTAMP_IDX, _MESSAGE_INGESTED_AT_IDX)
            if message_row is not None:
                message_row[_MESSAGE_AGENT_NAME_IDX] = agent_name
                message_row[_MESSAGE_AGENT_VERSION_IDX] = agent_version
                message_rows.append(message_row)

        _insert_clients(self.client, list(client_rows_by_id.values()))
        return event_rows, usage_rows, message_rows

    def insert_with_dlq_fallback(
        self, table: str, rows: list[list], columns: list[str], call_id_idx: int, session_id_idx: int,
    ) -> None:
        """One table's insert. On a rejected batch (e.g. a value out of a
        column's numeric range), falls back to inserting row-by-row so only
        the actual poison row(s) are lost - those go to ingest_dlq (see
        schema.sql) for triage instead of silently vanishing with the rest
        of a good batch. Each table is independent: one table rejecting its
        batch must not drop another table's otherwise-good rows for this
        same set of events."""
        if not rows:
            return
        client = self.client
        try:
            client.insert(table, rows, column_names=columns)
            return
        except Exception:
            logger.exception("failed to insert into %s (n=%d) - retrying row-by-row", table, len(rows))

        failure_rows = []
        for row in rows:
            try:
                client.insert(table, [row], column_names=columns)
            except Exception as exc:
                failure_rows.append([
                    datetime.now(timezone.utc), table, str(exc),
                    row[call_id_idx], row[session_id_idx],
                    json.dumps(row, default=str),
                ])
        if failure_rows:
            logger.error("dropped %d row(s) into ingest_dlq (stage=%s)", len(failure_rows), table)
            try:
                client.insert("ingest_dlq", failure_rows, column_names=_FAILURE_COLUMNS)
            except Exception:
                logger.exception("failed to record ingest_dlq (stage=%s, n=%d)", table, len(failure_rows))
