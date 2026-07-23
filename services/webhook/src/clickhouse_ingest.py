"""Parses LiteLLM's StandardLoggingPayload webhook events and inserts them
into ClickHouse (agent_events, agent_usage, agent_messages) - the only
ingestion path now that the transcript-reading .claude/hooks + .codex/hooks
pipeline has been retired. agent_name/skill_name are recovered from the
payload's own messages (Agent/Skill tool_use blocks), not from a CLI-side
hook - see AGENTS.md.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import clickhouse_connect

from .config import (
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
)

_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9a-f]+)")
_COMMAND_NAME_RE = re.compile(r"<command-name>/?(.*?)</command-name>")
_COMMAND_VERSION_RE = re.compile(r"<command_version>(.*?)</command_version>")

# calculated_type prompt-prefix classifiers (category B in the schema-sql-
# capture plan) - checked in this order only when the response made no tool
# call at all (category A, handled separately in _classify_event via
# _response_tool_calls).
_JUDGE_CALL_PREFIX = "Based on the conversation transcript above"
_SYSTEM_NOTIFICATION_PREFIX = "[SYSTEM NOTIFICATION"
_SUGGESTION_MODE_PREFIX = "[SUGGESTION MODE"
_TRANSCRIPT_HANDOFF_PREFIX = "<transcript>"
_TITLE_GEN_PREFIX = "<session>"
_INTERRUPTED_PREFIX = "[Request interrupted by user]"
_WEBPAGE_CONTENT_PREFIX = "Web page content"

# provider classification for agent_usage.provider - the same 3-way regex
# that used to be duplicated across ~30 Grafana panels, now computed once
# at ingest time instead.
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


def _to_dt(epoch_seconds: Optional[float]) -> datetime:
    if not epoch_seconds:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


def _flatten_content(content: Any) -> str:
    """Anthropic message content is either a plain string or a list of
    content blocks (text/tool_use/tool_result/...). Only the human-readable
    text is worth storing in agent_messages - tool payloads are already
    captured in full on disk (webhook/captures/*.json)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and block.get("text"):
            parts.append(block["text"])
        elif block_type == "tool_use":
            parts.append(f"[tool_use:{block.get('name', '')}]")
        elif block_type == "tool_result":
            parts.append("[tool_result]")
    return "\n".join(parts)


def _last_user_text(messages: Any) -> str:
    """The most recent human-originated turn, not just the most recent
    "user"-role message - a tool_result continuation is also role="user" but
    is an automatic reply, not something a human typed. Skipping those (same
    logic as _active_command_name) avoids storing a bare "[tool_result]"
    placeholder as prompt_text for every call after the first in a chain.
    Doesn't strip injected system-reminder/command-message boilerplate that
    may still share the same message as genuine text - that's inherent to
    how the CLI constructs its prompts, not something this can cleanly
    separate out."""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            if content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
        return _flatten_content(content) if not isinstance(content, str) else content
    return ""


def _active_command_name_and_version(messages: Any) -> tuple[str, str]:
    """Walks backward from the most recent message, skipping over messages
    that are pure tool_result continuations (automatic, not new human
    input), to find the human-originated turn that started the current
    chain of calls. Claude Code injects a "<command-name>/foo</command-name>"
    tag into that turn's text when it was a slash-command invocation - if
    found, this whole chain (tool calls, follow-up turns) is attributed to
    command "foo". A command's own body may carry a
    "<command_version>1.2.3</command_version>" marker (a command file never
    needs a version-suffixed filename - see AGENTS.md); since that body is
    expanded into this same message, look for it right here rather than via
    a separate lookup. Returns ("", "") for a freeform prompt (not a
    command), or once the user has moved on to unrelated freeform text in a
    later turn. A never-edited command has no marker - version comes back
    "" (same graceful blank as an unversioned agent/skill)."""
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
            return "", ""
        version_match = _COMMAND_VERSION_RE.search(text)
        return match.group(1), (version_match.group(1) if version_match else "")
    return "", ""


def _failed_tool_call(messages: Any) -> tuple[str, str, str]:
    """If the last message is a tool_result marked is_error, find its paired
    tool_use (by tool_use_id) earlier in the same messages array and return
    (tool_name, arguments_json, error_text) - the specific tool invocation
    whose failure THIS call is now reacting to. Distinct from this row's own
    tool_name, which is whatever tool (if any) THIS call's own response goes
    on to invoke next - a call can process a failed tool result and then
    invoke a completely different tool, or none at all."""
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


def _session_and_trace_id(payload: dict) -> tuple[str, str]:
    trace_id = payload.get("trace_id") or ""
    headers = ((payload.get("metadata") or {}).get("requester_custom_headers")) or {}
    session_id = headers.get("x-claude-code-session-id") or trace_id or payload.get("litellm_call_id", "")
    trace_id = trace_id or session_id
    return session_id, trace_id


def _split_name_version(value: str) -> tuple[str, str]:
    """Splits on the last "_v" - the *old* convention, where an agent/skill's
    invocation identifier itself carried "<name>_v<version>" (e.g.
    "test-researcher_v1.0.0"). Superseded for new writes by
    _version_marker_for_name (version now lives in a marker inside
    description:/body instead, so the identifier itself never needs to
    change) - kept as a fallback in _agent_invocations_from_messages for
    subagent_type values still carrying the old suffix (no marker found),
    and for reading historical agent_invocations rows written under the old
    convention. Skills have no equivalent suffix convention (confirmed via
    AGENTS.md), so this fallback only applies to agents."""
    idx = value.rfind("_v")
    if idx == -1 or idx + 2 >= len(value):
        return value, ""
    return value[:idx], value[idx + 2:]


def _flatten_messages_text(messages: Any) -> str:
    """Every message's text, joined - used to search for the "Available
    agent types"/"available skills" system-reminder listings Claude Code
    injects into the conversation, which is where an <agent_version>/
    <skill_version> marker (embedded in that entry's description:) actually
    surfaces. Not restricted to a single message since the listing can sit
    several turns back from the tool call it informed."""
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
    """Finds "- <name>: <tag>version</tag>..." - the shape of one entry in
    the "Available agent types"/"available skills" listing once its
    description: is authored with the marker as the very first thing in the
    text (see AGENTS.md), which keeps the marker on the same line as the
    entry's own name so it can be looked up by name rather than position.
    Takes the last match so a listing that gets refreshed mid-session
    ("New agent types are now available...") always wins over a stale one.
    Returns "" when the name has no marker (self-named/ad-hoc agent, or an
    entry never edited since creation)."""
    if not name:
        return ""
    pattern = re.compile(rf"^- {re.escape(name)}: <{tag}>([^<]*)</{tag}>", re.MULTILINE)
    matches = pattern.findall(text)
    return matches[-1] if matches else ""


def _user_id(payload: dict) -> str:
    metadata = payload.get("metadata") or {}
    return (
        metadata.get("user_api_key_team_alias")
        or metadata.get("user_api_key_alias")
        or "unknown-user"
    )


def _agent_invocations_from_messages(messages: Any) -> list[tuple[str, str, str, str]]:
    """Scan messages for Agent tool_use blocks paired with the tool_result
    that immediately follows, and pull the spawned subagent's agent_id out
    of that result's text (e.g. "agentId: a04bd3c594bf74fb9"). subagent_type
    is the bare name as-is under the current convention (see AGENTS.md);
    agent_version comes from a "<agent_version>...</agent_version>" marker
    inside that agent's entry in the "Available agent types" listing, which
    Claude Code re-injects into messages on every call - falling back, when
    no marker is found, to splitting a "_v<version>" suffix baked directly
    into subagent_type itself (the *old* convention - see
    _split_name_version), so agent_invocations always holds a clean
    (subagent_type, agent_version) pair regardless of which convention the
    wire value used. Returns (agent_id, subagent_type, agent_version,
    description) tuples - usually empty, since most calls never spawn a
    subagent."""
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
                agent_version = _version_marker_for_name(listing_text, subagent_type, "agent_version")
                if not agent_version:
                    bare_name, suffix_version = _split_name_version(subagent_type)
                    if suffix_version:
                        subagent_type, agent_version = bare_name, suffix_version
                results.append((agent_id, subagent_type, agent_version, input_.get("description", "")))
    return results


def _agent_id_from_tool_result(messages: list, tool_use_index: int, tool_use_id: Optional[str]) -> str:
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
    """Whether *this* call's own completion just invoked a tool. LiteLLM
    normalizes the response to an OpenAI-style tool_calls list
    (function.name/function.arguments as a JSON string) regardless of the
    Anthropic-style content blocks used in the historical "messages" - so
    this must read payload["response"], not "messages"."""
    response = payload.get("response") or {}
    choices = response.get("choices") or []
    if not choices:
        return []
    message = choices[0].get("message") or {}
    calls = []
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


def _skill_name_and_version(payload: dict) -> tuple[str, str]:
    """skill_name is the bare skill name as-is (a skill's directory name
    never carries a "_v<version>" suffix - see AGENTS.md). skill_version
    comes from a "<skill_version>...</skill_version>" marker inside that
    skill's own entry in the "available skills" listing, which sits in this
    same payload's messages alongside the Skill tool_use - no cross-call
    lookup needed."""
    for name, arguments in _response_tool_calls(payload):
        if name == "Skill" and arguments.get("skill"):
            skill_name = arguments["skill"]
            skill_version = _version_marker_for_name(
                _flatten_messages_text(payload.get("messages")), skill_name, "skill_version"
            )
            return skill_name, skill_version
    return "", ""


def _provider_for_model(model: str) -> str:
    if model.startswith("claude-"):
        return "claude"
    if _PROVIDER_OPENAI_RE.match(model):
        return "openai"
    return "other"


def _classify_event(payload: dict) -> tuple[str, dict]:
    """The calculated_type/calculated_payload dispatcher - see the
    schema-sql-capture plan for the full category list and rationale for
    keeping this a single unified classification rather than spread across
    fields/tables. Priority order: did this call's own response invoke a
    tool (category A), and if not, what does the prompt that led to a plain
    text reply look like (category B, a port of panel-76's startsWith
    chain). 'unknown' is a real, searchable bucket for iterative extension,
    not an error - `_response_tool_calls`/`_last_user_text` are reused
    as-is rather than re-parsing payload["response"]/["messages"] again."""
    tool_calls = _response_tool_calls(payload)
    if tool_calls:
        first_name, first_args = tool_calls[0]
        if first_name == "Agent":
            listing_text = _flatten_messages_text(payload.get("messages"))
            subagent_type = first_args.get("subagent_type", "")
            return "agent_spawn", {
                "subagent_type": subagent_type,
                "agent_version": _version_marker_for_name(listing_text, subagent_type, "agent_version"),
                "description": first_args.get("description", ""),
            }
        if first_name == "Skill":
            return "skill_call", {"skill": first_args.get("skill", ""), "args": first_args.get("args", "")}
        if first_name == "AskUserQuestion":
            return "ask_user_question", {"questions": first_args.get("questions", [])}
        return "tool_call", {"tools": [{"tool": name, "args": args} for name, args in tool_calls]}

    prompt_text = _last_user_text(payload.get("messages"))
    if prompt_text.startswith(_JUDGE_CALL_PREFIX):
        # Structured confidence/reasoning extraction from the judge's
        # free-text response is a later classifier refinement (once real
        # examples are visible via reparse) - excerpt only for now.
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
        # The text itself already lives in agent_messages.response_text -
        # calculated_payload stays empty rather than duplicating it.
        return "llm_answer", {}
    return "unknown", {}


def _source_row(payload: dict, session_id: str, now: datetime) -> list:
    """The event_sources row - the full, untouched original payload
    (messages included), written once per call so a later reparse
    (webhook/src/reparse.py) can recompute calculated_type/calculated_payload/
    provider without needing .capture/*.json, which is out of scope as a
    parsing source (see event_sources's comment in schema.sql)."""
    return [
        payload.get("litellm_call_id", ""),
        session_id,
        now,
        json.dumps(payload, default=str),
    ]


def _first_tool_call_name(payload: dict) -> str:
    """The actual tool the model invoked this turn (e.g. "Agent", "Skill",
    "mcp__clickhouse__whatsup", "Read", "Bash", ...) - falls back to the
    call_type when the turn made no tool call at all (a plain text reply)."""
    calls = _response_tool_calls(payload)
    return calls[0][0] if calls else ""


def _agent_invocation_id(payload: dict) -> str:
    headers = ((payload.get("metadata") or {}).get("requester_custom_headers")) or {}
    return headers.get("x-claude-code-agent-id", "")


def _agent_name_and_version_for_invocation(client, agent_invocation_id: str) -> tuple[str, str]:
    """Best-effort lookup - blank if the parent's Agent tool_use/tool_result
    hasn't been ingested yet (e.g. the subagent's own first call raced
    ahead of it). agent_version is its own column now (extracted once, at
    the parent's insert time, from the "Available agent types" listing -
    see _agent_invocations_from_messages) rather than split out of
    subagent_type here."""
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
    "timestamp", "user_id", "session_id", "trace_id",
    "turn_id", "event_type", "tool_name", "agent_name",
    "agent_version", "skill_name", "skill_version", "command_name",
    "command_version", "agent_invocation_id", "status", "latency_ms",
    "failed_tool_name", "failed_tool_args", "failed_tool_error",
    "litellm_call_id", "calculated_type", "calculated_payload", "ingested_at",
]
_USAGE_COLUMNS = [
    "timestamp", "user_id", "session_id", "trace_id", "turn_id", "model",
    "agent_name", "agent_version", "skill_name", "skill_version",
    "command_name", "command_version", "agent_invocation_id", "mcp_tool_name",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "stop_reason",
    "cache_creation_1h_tokens", "cache_creation_5m_tokens",
    "cost", "input_cost", "output_cost", "cache_hit", "ttft_ms",
    "litellm_call_id", "provider", "ingested_at",
]
_MESSAGE_COLUMNS = [
    "timestamp", "user_id", "session_id", "trace_id", "turn_id",
    "agent_name", "agent_version", "skill_name", "skill_version",
    "command_name", "command_version", "agent_invocation_id", "prompt_text", "response_text",
    "litellm_call_id", "ingested_at",
]
_SOURCE_COLUMNS = ["litellm_call_id", "session_id", "ingested_at", "raw_payload_full"]
_GIT_BRANCH_COLUMNS = ["session_id", "git_branch", "git_repo", "captured_at"]
_PLAN_PROPOSAL_COLUMNS = ["session_id", "plan_text", "captured_at"]

_INVOCATION_SPAWNED_AT_IDX = _INVOCATION_COLUMNS.index("spawned_at")
_EVENT_TIMESTAMP_IDX = _EVENT_COLUMNS.index("timestamp")
_EVENT_AGENT_NAME_IDX = _EVENT_COLUMNS.index("agent_name")
_EVENT_AGENT_VERSION_IDX = _EVENT_COLUMNS.index("agent_version")
_USAGE_TIMESTAMP_IDX = _USAGE_COLUMNS.index("timestamp")
_USAGE_AGENT_NAME_IDX = _USAGE_COLUMNS.index("agent_name")
_USAGE_AGENT_VERSION_IDX = _USAGE_COLUMNS.index("agent_version")
_MESSAGE_TIMESTAMP_IDX = _MESSAGE_COLUMNS.index("timestamp")
_MESSAGE_AGENT_NAME_IDX = _MESSAGE_COLUMNS.index("agent_name")
_MESSAGE_AGENT_VERSION_IDX = _MESSAGE_COLUMNS.index("agent_version")
_SOURCE_INGESTED_AT_IDX = _SOURCE_COLUMNS.index("ingested_at")
_EVENT_INGESTED_AT_IDX = _EVENT_COLUMNS.index("ingested_at")
_USAGE_INGESTED_AT_IDX = _USAGE_COLUMNS.index("ingested_at")
_MESSAGE_INGESTED_AT_IDX = _MESSAGE_COLUMNS.index("ingested_at")


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
    client.insert("event_sources", [row], column_names=_SOURCE_COLUMNS)


def _insert_git_branch(client, row: list) -> None:
    client.insert("session_git_branch", [row], column_names=_GIT_BRANCH_COLUMNS)


def _insert_plan_proposal(client, row: list) -> None:
    client.insert("plan_proposals", [row], column_names=_PLAN_PROPOSAL_COLUMNS)


def _event_row(
    payload: dict, session_id: str, trace_id: str,
    agent_name: str, agent_version: str, skill_name: str, skill_version: str,
    command_name: str, command_version: str, agent_invocation_id: str,
    now: Optional[datetime] = None,
) -> list:
    start_time = payload.get("startTime")
    end_time = payload.get("endTime")
    # NOT payload["response_time"] - for streamed calls that's LiteLLM's
    # time-to-first-token, not the call's total duration (was ~1-3ms while
    # endTime-startTime showed multi-second real latency).
    latency_ms = (
        int((end_time - start_time) * 1000)
        if isinstance(start_time, (int, float)) and isinstance(end_time, (int, float))
        else None
    )
    # Blank, not LiteLLM's call_type, when the turn made no tool call at all
    # (a plain text reply) - callers that care about "was this a tool call"
    # (Top 10 slowest tool calls, error rate/latency by tool_name) already
    # filter tool_name != '', and call_type showing up as a fake "tool" was
    # exactly the noise that filter was meant to exclude.
    tool_name = _first_tool_call_name(payload)
    failed_tool_name, failed_tool_args, failed_tool_error = _failed_tool_call(payload.get("messages"))
    calculated_type, calculated_payload = _classify_event(payload)
    return [
        _to_dt(payload.get("endTime") or payload.get("startTime")),
        _user_id(payload),
        session_id,
        trace_id,
        0,  # turn_id: unknown from this source
        "litellm_call",
        tool_name,
        agent_name,
        agent_version,
        skill_name,
        skill_version,
        command_name,
        command_version,
        agent_invocation_id,
        payload.get("status", ""),
        latency_ms,
        failed_tool_name,
        failed_tool_args,
        failed_tool_error,
        payload.get("litellm_call_id", ""),
        calculated_type,
        json.dumps(calculated_payload, default=str),
        now or datetime.now(timezone.utc),
    ]


def _usage_row(
    payload: dict, session_id: str, trace_id: str,
    agent_name: str, agent_version: str, skill_name: str, skill_version: str,
    command_name: str, command_version: str, agent_invocation_id: str,
    now: Optional[datetime] = None,
) -> Optional[list]:
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

    completion_start = payload.get("completionStartTime")
    start_time = payload.get("startTime")
    ttft_ms = (
        int((completion_start - start_time) * 1000)
        if isinstance(completion_start, (int, float)) and isinstance(start_time, (int, float))
        else 0
    )

    called_tool = _first_tool_call_name(payload)
    mcp_tool_name = called_tool if called_tool.startswith("mcp__") else ""
    cost_breakdown = payload.get("cost_breakdown") or {}
    model = payload.get("model_group") or payload.get("model", "")

    return [
        _to_dt(payload.get("endTime") or payload.get("startTime")),
        _user_id(payload),
        session_id,
        trace_id,
        0,  # turn_id: unknown from this source
        model,
        agent_name,
        agent_version,
        skill_name,
        skill_version,
        command_name,
        command_version,
        agent_invocation_id,
        mcp_tool_name,
        prompt_tokens,
        completion_tokens,
        usage.get("cache_creation_input_tokens") or 0,
        usage.get("cache_read_input_tokens") or 0,
        stop_reason,
        ephemeral.get("ephemeral_1h_input_tokens") or 0,
        ephemeral.get("ephemeral_5m_input_tokens") or 0,
        payload.get("response_cost") or 0,
        cost_breakdown.get("input_cost") or 0,
        cost_breakdown.get("output_cost") or 0,
        1 if payload.get("cache_hit") else 0,
        ttft_ms,
        payload.get("litellm_call_id", ""),
        _provider_for_model(model),
        now or datetime.now(timezone.utc),
    ]


def _message_row(
    payload: dict, session_id: str, trace_id: str,
    agent_name: str, agent_version: str, skill_name: str, skill_version: str,
    command_name: str, command_version: str, agent_invocation_id: str,
    now: Optional[datetime] = None,
) -> Optional[list]:
    response = payload.get("response") or {}
    choices = response.get("choices") or []
    response_text = _flatten_content(choices[0].get("message", {}).get("content")) if choices else ""
    prompt_text = _last_user_text(payload.get("messages"))
    if not prompt_text and not response_text:
        return None

    return [
        _to_dt(payload.get("endTime") or payload.get("startTime")),
        _user_id(payload),
        session_id,
        trace_id,
        0,  # turn_id: unknown from this source
        agent_name,
        agent_version,
        skill_name,
        skill_version,
        command_name,
        command_version,
        agent_invocation_id,
        prompt_text,
        response_text,
        payload.get("litellm_call_id", ""),
        now or datetime.now(timezone.utc),
    ]


def ingest_standard_logging_payload(payload: dict) -> None:
    """Insert one LiteLLM StandardLoggingPayload into ClickHouse. Never
    raises - a malformed/unexpected payload shape must not break the
    webhook's ack to LiteLLM (LiteLLM would otherwise retry it forever)."""
    session_id = trace_id = ""
    try:
        session_id, trace_id = _session_and_trace_id(payload)
        client = get_client()
        now = datetime.now(timezone.utc)

        messages = payload.get("messages")
        _insert_agent_invocations(client, _agent_invocation_rows(session_id, messages, now=now))

        agent_invocation_id = _agent_invocation_id(payload)
        agent_name, agent_version = _agent_name_and_version_for_invocation(client, agent_invocation_id)
        skill_name, skill_version = _skill_name_and_version(payload)
        command_name, command_version = _active_command_name_and_version(messages)

        _insert_source(client, _source_row(payload, session_id, now))

        _insert_event(client, _event_row(
            payload, session_id, trace_id,
            agent_name, agent_version, skill_name, skill_version,
            command_name, command_version, agent_invocation_id, now,
        ))

        if payload.get("status") == "success":
            usage_row = _usage_row(
                payload, session_id, trace_id,
                agent_name, agent_version, skill_name, skill_version,
                command_name, command_version, agent_invocation_id, now,
            )
            if usage_row is not None:
                _insert_usage(client, usage_row)

            message_row = _message_row(
                payload, session_id, trace_id,
                agent_name, agent_version, skill_name, skill_version,
                command_name, command_version, agent_invocation_id, now,
            )
            if message_row is not None:
                _insert_message(client, message_row)
    except Exception:
        logger.exception(
            "failed to ingest LiteLLM payload into ClickHouse "
            "(litellm_call_id=%s trace_id=%s session_id=%s status=%s call_type=%s)",
            payload.get("litellm_call_id", ""), trace_id, session_id,
            payload.get("status", ""), payload.get("call_type", ""),
        )


def ingest_git_branch(session_id: str, git_branch: str, git_repo: str = "") -> None:
    """Insert a session's git branch/repo, reported by
    hooks/report_git_branch.py at SessionStart. Never raises - a
    tracking-side failure must not surface as an error to the CLI session
    that reported it."""
    try:
        client = get_client()
        _insert_git_branch(client, [session_id, git_branch, git_repo, datetime.now(timezone.utc)])
    except Exception:
        logger.exception("failed to ingest git branch (session_id=%s)", session_id)


def ingest_plan_proposal(session_id: str, plan_text: str) -> None:
    """Insert an ExitPlanMode call's plan text, reported by
    hooks/report_plan_proposal.py at PreToolUse. Never raises - a
    tracking-side failure must not surface as an error to the CLI session
    that reported it."""
    try:
        client = get_client()
        _insert_plan_proposal(client, [session_id, plan_text, datetime.now(timezone.utc)])
    except Exception:
        logger.exception("failed to ingest plan proposal (session_id=%s)", session_id)


def ingest_webhook_body(body: Any) -> None:
    """body is usually a list of StandardLoggingPayload dicts
    (log_format: json_array in litellm/config.yaml), but tolerate a single
    dict too."""
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
    """The messages-dependent, DB-free half of ingesting one
    StandardLoggingPayload - everything that can be computed with pure
    functions, no ClickHouse round-trip. Called synchronously in the
    webhook's request handler (see server.py) so ClickHouse itself is never
    touched in the request path - webhook only ever produces onto Redis,
    webhook-worker is the only thing that inserts.

    Includes source_row - the untouched original payload (messages
    included), destined for event_sources - alongside the compact
    per-table rows. This is the one field NOT stripped down, since
    event_sources is exactly where the full payload is supposed to land
    (see schema.sql's event_sources comment); MAXLEN/the redis service's
    mem_limit are sized around this larger per-event footprint (see
    config.yml), not the ~100KB stripped-messages figure from before
    event_sources existed.

    agent_name/agent_version can't be resolved here - that needs a SELECT
    against agent_invocations, which only makes sense once this batch's own
    invocation_rows have actually been inserted. Left blank; patched in by
    ingest_events_batch() once it knows them.

    Never raises internally - lets the caller (queue_client.enqueue) decide
    whether one bad payload should drop just that item or the whole batch,
    matching this module's usual "ingestion is best-effort" stance.
    """
    session_id, trace_id = _session_and_trace_id(payload)
    messages = payload.get("messages")
    now = datetime.now(timezone.utc)
    invocation_rows = _agent_invocation_rows(session_id, messages, now=now)
    agent_invocation_id = _agent_invocation_id(payload)
    skill_name, skill_version = _skill_name_and_version(payload)
    command_name, command_version = _active_command_name_and_version(messages)

    source_row = _source_row(payload, session_id, now)

    event_row = _event_row(
        payload, session_id, trace_id,
        "", "", skill_name, skill_version,
        command_name, command_version, agent_invocation_id, now,
    )

    usage_row = None
    message_row = None
    if payload.get("status") == "success":
        usage_row = _usage_row(
            payload, session_id, trace_id,
            "", "", skill_name, skill_version,
            command_name, command_version, agent_invocation_id, now,
        )
        message_row = _message_row(
            payload, session_id, trace_id,
            "", "", skill_name, skill_version,
            command_name, command_version, agent_invocation_id, now,
        )

    return {
        "agent_invocation_id": agent_invocation_id,
        "invocation_rows": [
            _serialize_row(row, _INVOCATION_SPAWNED_AT_IDX) for row in invocation_rows
        ],
        "source_row": _serialize_row(source_row, _SOURCE_INGESTED_AT_IDX),
        "event_row": _serialize_row_multi(event_row, _EVENT_TIMESTAMP_IDX, _EVENT_INGESTED_AT_IDX),
        "usage_row": _serialize_row_multi(usage_row, _USAGE_TIMESTAMP_IDX, _USAGE_INGESTED_AT_IDX),
        "message_row": _serialize_row_multi(message_row, _MESSAGE_TIMESTAMP_IDX, _MESSAGE_INGESTED_AT_IDX),
    }


def ingest_events_batch(events: list[dict]) -> None:
    """Runs in webhook-worker, not webhook - takes a batch of build_event()
    outputs read back off the Redis stream and writes them with exactly one
    client.insert() per table, instead of the up-to-4-inserts-per-payload
    the synchronous path used to do. This is the batching that takes load
    off ClickHouse under concurrent webhook traffic - see AGENTS.md.

    Never raises - a malformed/unexpected event in the batch must not
    crash the worker loop (the caller still needs to XACK or retry the
    batch's message ids regardless).
    """
    if not events:
        return
    try:
        client = get_client()

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
            client.insert("event_sources", source_rows, column_names=_SOURCE_COLUMNS)

        agent_fields_cache: dict[str, tuple[str, str]] = {}
        event_rows, usage_rows, message_rows = [], [], []

        for event in events:
            agent_invocation_id = event.get("agent_invocation_id") or ""
            if agent_invocation_id:
                if agent_invocation_id not in agent_fields_cache:
                    agent_fields_cache[agent_invocation_id] = _agent_name_and_version_for_invocation(
                        client, agent_invocation_id
                    )
                agent_name, agent_version = agent_fields_cache[agent_invocation_id]
            else:
                agent_name, agent_version = "", ""

            event_row = _deserialize_row_multi(event.get("event_row"), _EVENT_TIMESTAMP_IDX, _EVENT_INGESTED_AT_IDX)
            if event_row is not None:
                event_row[_EVENT_AGENT_NAME_IDX] = agent_name
                event_row[_EVENT_AGENT_VERSION_IDX] = agent_version
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

        if event_rows:
            client.insert("agent_events", event_rows, column_names=_EVENT_COLUMNS)
        if usage_rows:
            client.insert("agent_usage", usage_rows, column_names=_USAGE_COLUMNS)
        if message_rows:
            client.insert("agent_messages", message_rows, column_names=_MESSAGE_COLUMNS)
    except Exception:
        logger.exception("failed to ingest event batch (n=%d)", len(events))
