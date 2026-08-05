"""Unit tests for the pure (no-ClickHouse-access) functions in
ingest_parsing.py, exercised against real payloads in
_common/tests/captures/*.json.
DB-touching functions are out of scope (see test_ingest_db.py)."""

import json
from datetime import datetime, timezone

from common import ingest_parsing as ip
from conftest import load_capture

# ---------------------------------------------------------------------------
# _to_dt
# ---------------------------------------------------------------------------

def test_to_dt_success_converts_epoch_seconds():
    dt = ip._to_dt(1750000000.5)
    assert dt == datetime.fromtimestamp(1750000000.5, tz=timezone.utc)


def test_to_dt_unsuccess_falls_back_to_now_for_falsy_input():
    before = datetime.now(timezone.utc)
    dt = ip._to_dt(None)
    after = datetime.now(timezone.utc)
    assert before <= dt <= after


# ---------------------------------------------------------------------------
# _flatten_content
# ---------------------------------------------------------------------------

def test_flatten_content_success_joins_text_and_placeholders():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "name": "Bash"},
        {"type": "tool_result", "content": "ignored"},
    ]
    assert ip._flatten_content(content) == "hello\n[tool_use:Bash]\n[tool_result]"


def test_flatten_content_unsuccess_non_list_non_str_returns_empty():
    assert ip._flatten_content(None) == ""
    assert ip._flatten_content(42) == ""


def test_flatten_content_success_extracts_responses_api_text_blocks():
    content = [
        {"type": "input_text", "text": "ты тут?"},
        {"type": "output_text", "text": "Да, я здесь."},
    ]
    assert ip._flatten_content(content) == "ты тут?\nДа, я здесь."


# ---------------------------------------------------------------------------
# _last_user_text
# ---------------------------------------------------------------------------

def test_last_user_text_success_returns_plain_prompt():
    payload = load_capture("success_plain")
    text = ip._last_user_text(payload["messages"])
    assert "test-summarizer skill" in text


def test_last_user_text_unsuccess_skips_pure_tool_result_continuation():
    payload = load_capture("success_with_failed_tool_reaction", index=1)
    # trailing messages are all tool_result continuations - must not return
    # a bare tool_result placeholder.
    text = ip._last_user_text(payload["messages"])
    assert "[tool_result]" != text
    assert "test-summarizer skill" in text


def test_last_user_text_success_returns_responses_api_prompt():
    payload = load_capture("chatgpt_responses_shape")
    text = ip._last_user_text(payload["messages"])
    assert text == "ты тут?"


def test_last_user_text_unsuccess_skips_non_message_items():
    payload = load_capture("chatgpt_responses_shape")
    text = ip._last_user_text(payload["messages"])
    assert "tool schema listing" not in text
    assert "ls" not in text


# ---------------------------------------------------------------------------
# _codex_collaboration_mode_change
# ---------------------------------------------------------------------------

def _developer_message(text):
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}


def _user_message(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def test_codex_collaboration_mode_change_success_finds_switch_notice():
    messages = [
        _developer_message("<collaboration_mode># Plan Mode (Conversational)\n\nYou work in 3 phases..."),
        _user_message("как нам посчитать количество файлов"),
        {"type": "reasoning"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "..."}]},
        _developer_message("<collaboration_mode># Collaboration Mode: Default\n\nYou are now in Default..."),
        _user_message('<codex_internal_context source="goal">\n<objective>\ndo the thing\n</objective>'),
    ]
    assert ip._codex_collaboration_mode_change(messages) == "Collaboration Mode: Default"


def test_codex_collaboration_mode_change_success_reports_starting_mode_on_first_call():
    # The startup preamble sits several messages before the real first
    # prompt (AGENTS.md/environment_context in between), so the "switch"
    # adjacency check alone would miss it - but this is the session's first
    # call (no assistant turn yet), so the starting mode should still be
    # reported instead of silently saying nothing.
    messages = [
        _developer_message("<collaboration_mode># Plan Mode (Conversational)\n\nYou work in 3 phases..."),
        _user_message("# AGENTS.md instructions..."),
        _user_message("как нам посчитать количество файлов"),
    ]
    assert ip._codex_collaboration_mode_change(messages) == "Plan Mode (Conversational)"


def test_codex_collaboration_mode_change_unsuccess_later_call_with_no_switch_stays_empty():
    # Not the first call (an assistant turn already happened) and no fresh
    # switch notice immediately precedes the latest prompt - must stay "".
    messages = [
        _developer_message("<collaboration_mode># Plan Mode (Conversational)\n\nYou work in 3 phases..."),
        _user_message("как нам посчитать количество файлов"),
        {"type": "reasoning"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "..."}]},
        _user_message("второй вопрос без переключения режима"),
    ]
    assert ip._codex_collaboration_mode_change(messages) == ""


def test_codex_collaboration_mode_change_unsuccess_later_call_does_not_refire():
    # Once more turns follow the switch, it no longer sits immediately
    # before the latest user turn, so a later call must not re-report it.
    messages = [
        _developer_message("<collaboration_mode># Collaboration Mode: Default\n\n..."),
        _user_message('<codex_internal_context source="goal">\n<objective>\nfirst\n</objective>'),
        {"type": "reasoning"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "..."}]},
        _user_message('<codex_internal_context source="goal">\n<objective>\nsecond\n</objective>'),
    ]
    assert ip._codex_collaboration_mode_change(messages) == ""


def test_codex_collaboration_mode_change_unsuccess_claude_code_payload_returns_empty():
    payload = load_capture("success_plain")
    assert ip._codex_collaboration_mode_change(payload["messages"]) == ""


# ---------------------------------------------------------------------------
# _active_command_name
# ---------------------------------------------------------------------------

def test_active_command_name_success_recovers_slash_command():
    payload = load_capture("success_with_command", index=1)
    assert ip._active_command_name(payload["messages"]) == "mcp"


def test_active_command_name_unsuccess_freeform_prompt_returns_empty():
    payload = load_capture("success_with_command", index=0)
    assert ip._active_command_name(payload["messages"]) == ""


def test_active_command_name_success_recovers_codex_internal_context():
    # Codex CLI's persistent-goal continuation wrapper - real capture shape.
    text = (
        '<codex_internal_context source="goal">\n'
        "Continue working toward the active thread goal.\n"
        "<objective>\nпосчитать количество файлов в папке hooks\n</objective>\n"
        "</codex_internal_context>"
    )
    messages = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}]
    assert ip._active_command_name(messages) == "goal"


def test_active_command_name_success_recovers_arbitrary_codex_context_source():
    # Not hardcoded to "goal" - any future context name is picked up as-is.
    text = '<codex_internal_context source="plan">\n<objective>\ndo the thing\n</objective>\n</codex_internal_context>'
    messages = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}]
    assert ip._active_command_name(messages) == "plan"


def test_prompt_kind_and_display_success_renders_codex_goal_context_as_command():
    text = (
        '<codex_internal_context source="goal">\n'
        "Continue working toward the active thread goal.\n"
        "<objective>\nпосчитать количество файлов в папке hooks\n</objective>\n"
        "</codex_internal_context>"
    )
    prompt_kind, display_text, display_arg = ip._prompt_kind_and_display(text, "goal")
    assert prompt_kind == "command"
    assert display_text == "/goal посчитать количество файлов в папке hooks"


def test_prompt_kind_and_display_success_classifies_away_recap():
    text = (
        "The user stepped away and is coming back. Recap in under 40 words, "
        "1-2 plain sentences, no markdown. Lead with the overall goal and current "
        "task, then the one next action."
    )
    prompt_kind, display_text, display_arg = ip._prompt_kind_and_display(text, "")
    assert prompt_kind == "away_recap"
    assert display_text == "[background] away recap"
    assert display_arg == ""


def test_prompt_kind_and_display_success_classifies_compact_summary():
    response_text = (
        "<analysis>\nLet me go through this conversation chronologically.\n"
        "1. Did some stuff.\n</analysis>\n\n"
        "<summary>\n1. Primary Request and Intent:\n" + ("word " * 150) + "\n</summary>"
    )
    prompt_kind, display_text, display_arg = ip._prompt_kind_and_display(
        "[tool_result]\nunrelated tool output", "", response_text
    )
    assert prompt_kind == "compact"
    assert display_text == "/compact"
    assert "analysis" not in display_arg
    assert len(display_arg.split()) <= 101  # 100 words + trailing "..."
    assert display_arg.endswith("...")


def test_prompt_kind_and_display_success_classifies_compact_summary_without_summary_tag():
    # If compaction's output shape ever drops the <summary> wrapper, fall
    # back to whatever text remains after stripping <analysis>.
    response_text = "<analysis>reasoning here</analysis>\n\nplain leftover text"
    prompt_kind, display_text, display_arg = ip._prompt_kind_and_display("hi", "", response_text)
    assert prompt_kind == "compact"
    assert display_text == "/compact"
    assert display_arg == "plain leftover text"


def test_prompt_kind_and_display_success_compact_takes_priority_over_empty_prompt():
    # Keyed off response_text, so it must fire even when prompt_text is
    # empty - unlike every other branch, which bails out early on that.
    response_text = "<analysis>x</analysis>\n\n<summary>done</summary>"
    prompt_kind, display_text, display_arg = ip._prompt_kind_and_display("", "", response_text)
    assert prompt_kind == "compact"
    assert display_arg == "done"


def test_prompt_kind_and_display_success_strips_two_leading_system_reminders():
    # Claude Code can inject more than one system-reminder as separate
    # leading content blocks of the same user turn (e.g. a skills-listing
    # reminder followed by a memory/claudeMd reminder) before the real task
    # text - both must be stripped, not just the first.
    text = (
        "<system-reminder>\nSkills available: foo, bar\n</system-reminder>\n"
        "<system-reminder>\nMemory: the user likes concise replies\n</system-reminder>\n\n"
        "Run this exact command and report its output: make status"
    )
    prompt_kind, display_text, display_arg = ip._prompt_kind_and_display(text, "")
    assert prompt_kind == "real"
    assert display_text == "Run this exact command and report its output: make status"


# ---------------------------------------------------------------------------
# _failed_tool_call
# ---------------------------------------------------------------------------

def test_failed_tool_call_success_finds_paired_failing_tool_use():
    payload = load_capture("success_with_failed_tool_reaction", index=0)
    tool_name, args_json, error_text = ip._failed_tool_call(payload["messages"])
    assert tool_name == "Bash"
    assert "shuf" in args_json  # args come from the failing call, not a later one
    assert "command not found" in error_text


def test_failed_tool_call_unsuccess_no_trailing_error_returns_blank():
    payload = load_capture("success_plain")
    assert ip._failed_tool_call(payload["messages"]) == ("", "", "")


# ---------------------------------------------------------------------------
# session_and_trace_id
# ---------------------------------------------------------------------------

def test_session_and_trace_id_success_prefers_claude_code_header():
    payload = load_capture("success_with_agent_and_skill")
    session_id, trace_id = ip.session_and_trace_id(payload)
    assert session_id == "ea219a89-9dd0-4f32-8c66-6f4d01e9788c"
    assert trace_id == payload["trace_id"]


def test_session_and_trace_id_unsuccess_falls_back_without_headers():
    payload = {"trace_id": "", "litellm_call_id": "call-123", "metadata": {}}
    session_id, trace_id = ip.session_and_trace_id(payload)
    assert session_id == "call-123"
    assert trace_id == "call-123"


def test_session_and_trace_id_success_uses_codex_turn_metadata():
    payload = load_capture("chatgpt_responses_cached")
    session_id, trace_id = ip.session_and_trace_id(payload)
    assert session_id == "019f8f18-0972-7110-bded-703a93ad9d6d"
    assert trace_id == payload["trace_id"]


def test_session_and_trace_id_success_claude_header_wins_over_codex():
    payload = {
        "trace_id": "",
        "litellm_call_id": "call-123",
        "metadata": {
            "requester_custom_headers": {
                "x-claude-code-session-id": "claude-session",
                "x-codex-turn-metadata": json.dumps({"session_id": "codex-session"}),
            }
        },
    }
    session_id, trace_id = ip.session_and_trace_id(payload)
    assert session_id == "claude-session"


# ---------------------------------------------------------------------------
# _user_agent
# ---------------------------------------------------------------------------

def test_user_agent_success_reads_claude_cli_string():
    payload = load_capture("success_with_agent_and_skill")
    assert ip._user_agent(payload) == "claude-cli/2.1.207 (external, cli)"


def test_user_agent_success_reads_codex_tui_string():
    # Real production value confirmed via a read-only ClickHouse query -
    # the bundled Codex-shaped captures don't happen to set user_agent.
    payload = {
        "metadata": {
            "user_agent": "codex-tui/0.145.0 (Mac OS 15.2.0; x86_64) Apple_Terminal/455 (codex-tui; 0.145.0)"
        }
    }
    assert ip._user_agent(payload) == (
        "codex-tui/0.145.0 (Mac OS 15.2.0; x86_64) Apple_Terminal/455 (codex-tui; 0.145.0)"
    )


def test_user_agent_unsuccess_missing_metadata_returns_empty():
    assert ip._user_agent({}) == ""


def test_user_agent_unsuccess_absent_key_returns_empty():
    assert ip._user_agent({"metadata": {}}) == ""


def test_codex_session_id_unsuccess_malformed_json_returns_empty():
    assert ip._codex_session_id({"x-codex-turn-metadata": "not-json"}) == ""


def test_codex_session_id_unsuccess_no_header_returns_empty():
    assert ip._codex_session_id({}) == ""


# ---------------------------------------------------------------------------
# _split_name_version
# ---------------------------------------------------------------------------

def test_split_name_version_success_splits_on_last_v():
    assert ip._split_name_version("test-researcher_v1.0.0") == ("test-researcher", "1.0.0")


def test_split_name_version_unsuccess_no_version_suffix():
    assert ip._split_name_version("claude") == ("claude", "")


# ---------------------------------------------------------------------------
# _version_marker_for_name / _flatten_messages_text
# ---------------------------------------------------------------------------

def test_version_marker_for_name_unsuccess_marker_at_start_of_listing_line_returns_empty():
    # Marker must be strictly the last token of the description now - a
    # marker anywhere else on the line no longer counts.
    text = (
        "Available agent types for the Agent tool:\n"
        "- clickhouse-analyst: v1.1.0 Delegate target for...\n"
        "- general-purpose: General-purpose agent for researching...\n"
    )
    assert ip._version_marker_for_name(text, "clickhouse-analyst") == ""


def test_version_marker_for_name_unsuccess_marker_in_middle_of_listing_line_returns_empty():
    text = (
        "Available agent types for the Agent tool:\n"
        "- clickhouse-analyst: Delegate target for... v1.1.0 more text after.\n"
    )
    assert ip._version_marker_for_name(text, "clickhouse-analyst") == ""


def test_version_marker_for_name_success_finds_marker_at_end_of_listing_line():
    text = (
        "Available agent types for the Agent tool:\n"
        "- clickhouse-analyst: Delegate target for questions answerable from ClickHouse. v1.1.0\n"
    )
    assert ip._version_marker_for_name(text, "clickhouse-analyst") == "1.1.0"


def test_version_marker_for_name_unsuccess_name_has_no_marker_returns_empty():
    text = "- general-purpose: General-purpose agent for researching...\n"
    assert ip._version_marker_for_name(text, "general-purpose") == ""
    assert ip._version_marker_for_name(text, "") == ""


# ---------------------------------------------------------------------------
# _user_id
# ---------------------------------------------------------------------------

def test_user_id_success_reads_real_user_id():
    payload = {"metadata": {"user_api_key_user_id": "u-123", "user_api_key_alias": "someone"}}
    assert ip._user_id(payload) == "u-123"


def test_user_id_falls_back_to_alias_when_no_real_id():
    payload = {"metadata": {"user_api_key_alias": "someone"}}
    assert ip._user_id(payload) == "someone"


def test_user_id_unsuccess_falls_back_to_unknown():
    assert ip._user_id({}) == "unknown-user"


def test_user_name_prefers_alias_over_real_id():
    payload = {"metadata": {"user_api_key_user_id": "u-123", "user_api_key_alias": "someone"}}
    assert ip._user_name(payload) == "someone"


def test_user_name_falls_back_to_real_id_when_no_alias():
    payload = {"metadata": {"user_api_key_user_id": "u-123"}}
    assert ip._user_name(payload) == "u-123"


def test_user_name_unsuccess_falls_back_to_unknown():
    assert ip._user_name({}) == "unknown-user"


# ---------------------------------------------------------------------------
# _group_id / _group_alias
# ---------------------------------------------------------------------------

def test_group_id_success_reads_stable_team_id():
    payload = {"metadata": {"user_api_key_team_id": "cc4f422e-f253-40b2-9dcb-749f9d5e7976", "user_api_key_team_alias": "team-a"}}
    assert ip._group_id(payload) == "cc4f422e-f253-40b2-9dcb-749f9d5e7976"


def test_group_id_unsuccess_no_team_falls_back_to_empty():
    payload = {"metadata": {"user_api_key_alias": "someone"}}
    assert ip._group_id(payload) == ""


def test_group_alias_success_reads_team_alias():
    payload = {"metadata": {"user_api_key_team_alias": "team-a"}}
    assert ip._group_alias(payload) == "team-a"


def test_group_alias_unsuccess_no_team_falls_back_to_empty():
    payload = {"metadata": {"user_api_key_alias": "someone"}}
    assert ip._group_alias(payload) == ""


# ---------------------------------------------------------------------------
# _agent_invocations_from_messages / _agent_id_from_tool_result
# ---------------------------------------------------------------------------

def test_agent_invocations_from_messages_success_finds_spawned_subagent():
    payload = load_capture("success_with_agent_and_skill")
    invocations = ip._agent_invocations_from_messages(payload["messages"])
    # predates the <agent_version> marker - falls back to splitting the
    # "_v<version>" suffix (old convention, via _split_name_version).
    assert invocations == [("aac9d05f148e9ae4a", "test-researcher", "1.0.0", "Summarize Makefile contents")]


def test_agent_invocations_from_messages_success_recovers_version_marker():
    messages = [
        {
            "role": "system",
            "content": (
                "Available agent types for the Agent tool:\n"
                "- clickhouse-analyst: Delegate target for... v1.1.0\n"
            ),
        },
        {"role": "assistant", "content": [{"type": "tool_use", "name": "Agent", "id": "toolu_1", "input": {"subagent_type": "clickhouse-analyst", "description": "look up cost"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "agentId: deadbeef"}]},
    ]
    invocations = ip._agent_invocations_from_messages(messages)
    assert invocations == [("deadbeef", "clickhouse-analyst", "1.1.0", "look up cost")]


def test_agent_invocations_from_messages_unsuccess_no_agent_calls_returns_empty():
    payload = load_capture("success_plain")
    assert ip._agent_invocations_from_messages(payload["messages"]) == []


def test_agent_id_from_tool_result_unsuccess_mismatched_tool_use_id():
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "name": "Agent", "id": "toolu_1"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_other", "content": "agentId: deadbeef"}]},
    ]
    assert ip._agent_id_from_tool_result(messages, 0, "toolu_1") == ""


# ---------------------------------------------------------------------------
# _response_tool_calls / _first_tool_call_name / _active_skill_name_and_version
# ---------------------------------------------------------------------------

def test_response_tool_calls_success_parses_function_arguments():
    payload = load_capture("success_with_agent_and_skill")
    calls = ip._response_tool_calls(payload)
    assert calls == [("Skill", {"skill": "test-summarizer", "args": "Summarize /Users/ixax/PycharmProjects/claude-wrapper/README.md"})]


def test_response_tool_calls_unsuccess_plain_text_reply_returns_empty():
    payload = load_capture("success_with_command", index=0)
    assert ip._response_tool_calls(payload) == []


def test_response_tool_calls_success_parses_function_call_output():
    payload = load_capture("chatgpt_responses_shape")
    payload["response"]["output"] = [
        {"type": "function_call", "name": "shell", "arguments": "{\"command\": \"ls\"}"}
    ]
    assert ip._response_tool_calls(payload) == [("shell", {"command": "ls"})]


def test_response_tool_calls_success_parses_custom_tool_call_output():
    # Codex's "exec" tool: "input" is raw JS source, not a JSON arguments
    # blob - real capture from a live session running `docker compose ps`.
    payload = load_capture("chatgpt_responses_shape")
    payload["response"]["output"] = [
        {"type": "custom_tool_call", "name": "exec", "input": "await tools.exec_command({cmd: 'ls'})"}
    ]
    assert ip._response_tool_calls(payload) == [
        ("exec", {"command": "await tools.exec_command({cmd: 'ls'})"})
    ]


def test_response_text_success_falls_back_to_responses_output():
    payload = load_capture("chatgpt_responses_shape")
    payload["response"]["output"] = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Да, я здесь."}]}
    ]
    assert ip._response_text(payload) == "Да, я здесь."


def test_response_text_unsuccess_empty_output_returns_blank():
    payload = load_capture("chatgpt_responses_shape")
    assert ip._response_text(payload) == ""


def test_first_tool_call_name_success_returns_first_call():
    payload = load_capture("success_with_agent_and_skill")
    assert ip._first_tool_call_name(payload) == "Skill"


def test_first_tool_call_name_unsuccess_plain_text_reply_returns_empty():
    payload = load_capture("success_with_command", index=0)
    assert ip._first_tool_call_name(payload) == ""


def test_active_skill_name_and_version_success_splits_skill_argument():
    payload = load_capture("success_with_agent_and_skill")
    # predates the <skill_version> marker convention - version comes back blank.
    assert ip._active_skill_name_and_version(payload, payload.get("messages")) == ("test-summarizer", "")


def test_active_skill_name_and_version_success_recovers_version_marker():
    messages = [
        {
            "role": "system",
            "content": (
                "available skills for the Skill tool:\n"
                "- test-linter: Minimal test skill... v2.0.0\n"
            ),
        },
    ]
    payload = {
        "messages": messages,
        "response": {"choices": [{"message": {"tool_calls": [
            {"function": {"name": "Skill", "arguments": json.dumps({"skill": "test-linter", "args": "check foo.py"})}}
        ]}}]},
    }
    assert ip._active_skill_name_and_version(payload, messages) == ("test-linter", "2.0.0")


def test_active_skill_name_and_version_unsuccess_no_skill_call():
    payload = load_capture("success_plain")
    assert ip._active_skill_name_and_version(payload, payload.get("messages")) == ("", "")


def test_active_skill_name_and_version_success_propagates_through_tool_result_continuation():
    # This call's own response is plain text - no Skill tool_use - but an
    # earlier assistant turn in the same continuation chain invoked Skill,
    # and the intervening user turn is a tool-result-only continuation, not
    # a fresh prompt - so the skill attribution should still propagate.
    messages = [
        {"role": "user", "content": "run the linter skill"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "test-linter", "args": "check foo.py"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
    ]
    payload = {"messages": messages, "response": {"choices": [{"message": {"content": "Done."}}]}}
    assert ip._active_skill_name_and_version(payload, messages) == ("test-linter", "")


def test_active_skill_name_and_version_success_propagates_through_skill_body_injection():
    # Real Claude Code shape (confirmed against a live capture): a Skill
    # invocation's tool_result comes bundled with the skill body text as a
    # second block in the *same* user message, not a tool_result-only
    # message - the continuation check must still recognize this as a
    # continuation, not a fresh user turn, or propagation stops immediately
    # after the triggering call.
    messages = [
        {"role": "user", "content": "run the linter skill"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "test-linter", "args": "check foo.py"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Launching skill: test-linter"},
                {"type": "text", "text": "# test-linter\n\nMinimal test skill body..."},
            ],
        },
    ]
    payload = {"messages": messages, "response": {"choices": [{"message": {"content": "Done."}}]}}
    assert ip._active_skill_name_and_version(payload, messages) == ("test-linter", "")


def test_active_skill_name_and_version_success_most_recent_skill_wins():
    # Two Skill invocations in the same continuation chain - walking
    # backward should return the more recent one.
    messages = [
        {"role": "user", "content": "run two skills"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "test-linter", "args": "check foo.py"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_2", "name": "Skill", "input": {"skill": "test-summarizer", "args": "summarize bar.md"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}]},
    ]
    payload = {"messages": messages, "response": {"choices": [{"message": {"content": "Done."}}]}}
    assert ip._active_skill_name_and_version(payload, messages) == ("test-summarizer", "")


def test_active_skill_name_and_version_unsuccess_stops_at_fresh_user_turn():
    # A skill fired in an earlier turn, but a genuine fresh user prompt
    # (not a tool-result continuation) followed it - the skill context
    # should not leak into this unrelated later turn.
    messages = [
        {"role": "user", "content": "run the linter skill"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "test-linter", "args": "check foo.py"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "now do something unrelated"},
    ]
    payload = {"messages": messages, "response": {"choices": [{"message": {"content": "Sure."}}]}}
    assert ip._active_skill_name_and_version(payload, messages) == ("", "")


def test_active_skill_name_and_version_unsuccess_empty_messages_no_index_error():
    payload = {"messages": [], "response": {"choices": [{"message": {"content": "Hi."}}]}}
    assert ip._active_skill_name_and_version(payload, []) == ("", "")
    assert ip._active_skill_name_and_version(payload, None) == ("", "")


# ---------------------------------------------------------------------------
# _agent_invocation_id
# ---------------------------------------------------------------------------

def test_agent_invocation_id_success_reads_header():
    payload = load_capture("success_subagent_call")
    assert ip._agent_invocation_id(payload) == "aac9d05f148e9ae4a"


def test_agent_invocation_id_unsuccess_missing_header_returns_empty():
    payload = load_capture("success_plain")
    assert ip._agent_invocation_id(payload) == ""


# ---------------------------------------------------------------------------
# _agent_invocation_rows
# ---------------------------------------------------------------------------

def test_agent_invocation_rows_success_builds_one_row_per_spawn():
    payload = load_capture("success_with_agent_and_skill")
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    rows = ip._agent_invocation_rows("session-1", payload["messages"], "parent-agent-1", now=now)
    assert rows == [
        ["aac9d05f148e9ae4a", "session-1", "test-researcher", "1.0.0", "Summarize Makefile contents", "parent-agent-1", now]
    ]


def test_agent_invocation_rows_unsuccess_no_spawns_returns_empty_list():
    payload = load_capture("success_plain")
    assert ip._agent_invocation_rows("session-1", payload["messages"], "") == []


# ---------------------------------------------------------------------------
# _event_row
# ---------------------------------------------------------------------------

def test_event_row_success_reports_status_and_latency():
    payload = load_capture("success_plain")
    row = ip._event_row(payload, ip.EventContext("session-1", "trace-1"))
    columns = ip._EVENT_COLUMNS
    values = dict(zip(columns, row))
    assert values["status"] == "success"
    assert values["session_id"] == "session-1"
    assert values["latency_ms"] is not None and values["latency_ms"] >= 0
    assert values["calculated_type"] == "title_gen"  # prompt starts with "<session>"
    assert values["group_id"] == "206ec527-2402-4c8b-b5b5-8bd65b8bca0f"  # capture's user_api_key_team_id


def test_event_row_unsuccess_failure_payload_has_no_tool_name_or_latency():
    payload = load_capture("failure")
    row = ip._event_row(payload, ip.EventContext("session-1", "trace-1"))
    values = dict(zip(ip._EVENT_COLUMNS, row))
    assert values["status"] == "failure"
    assert values["tool_name"] == ""


# ---------------------------------------------------------------------------
# _usage_row
# ---------------------------------------------------------------------------

def test_usage_row_success_extracts_token_counts():
    payload = load_capture("success_plain")
    row = ip._usage_row(payload, ip.EventContext("session-1", "trace-1"))
    assert row is not None
    values = dict(zip(ip._USAGE_COLUMNS, row))
    assert values["input_tokens"] == 723
    assert values["output_tokens"] == 16
    assert values["group_id"] == "206ec527-2402-4c8b-b5b5-8bd65b8bca0f"


def test_usage_row_unsuccess_no_billable_tokens_returns_none():
    payload = load_capture("failure")
    assert ip._usage_row(payload, ip.EventContext("session-1", "trace-1")) is None


def test_usage_row_unsuccess_negative_ttft_clamps_to_zero():
    # LiteLLM occasionally reports completionStartTime before startTime on
    # streamed calls (same quirk as endTime<startTime for latency_ms) -
    # observed in real .capture traffic; a negative value can't pack into
    # ttft_ms's UInt32 column and used to crash the whole insert batch.
    payload = load_capture("success_plain")
    payload = dict(payload, startTime=100.5, completionStartTime=100.4)
    row = ip._usage_row(payload, ip.EventContext("session-1", "trace-1"))
    assert row is not None
    values = dict(zip(ip._USAGE_COLUMNS, row))
    assert values["ttft_ms"] == 0


def test_usage_row_success_falls_back_to_responses_api_cache_fields():
    payload = load_capture("chatgpt_responses_cached")
    row = ip._usage_row(payload, ip.EventContext("session-1", "trace-1"))
    assert row is not None
    values = dict(zip(ip._USAGE_COLUMNS, row))
    assert values["input_tokens"] == 19167
    assert values["output_tokens"] == 42
    assert values["cache_read_tokens"] == 17920
    assert values["cache_creation_tokens"] == 0
    # cache_hit is None in this payload, but cached tokens were present - counts as a hit.
    assert values["cache_hit"] == 1


# ---------------------------------------------------------------------------
# _message_row
# ---------------------------------------------------------------------------

def test_message_row_success_captures_prompt_and_response_text():
    payload = load_capture("success_plain")
    row = ip._message_row(payload, ip.EventContext("session-1", "trace-1"))
    assert row is not None
    values = dict(zip(ip._MESSAGE_COLUMNS, row))
    assert "test-summarizer skill" in values["prompt_text"]
    assert values["response_text"]
    assert values["group_id"] == "206ec527-2402-4c8b-b5b5-8bd65b8bca0f"


def test_message_row_unsuccess_no_prompt_or_response_text_returns_none():
    payload = {"messages": [], "response": {"choices": []}}
    assert ip._message_row(payload, ip.EventContext("session-1", "trace-1")) is None


# ---------------------------------------------------------------------------
# build_event - queue-facing, DB-free half of ingestion. source_row
# deliberately carries the full payload; only per-table rows are stripped.
# ---------------------------------------------------------------------------

def test_build_event_success_returns_json_safe_dict_with_source_row():
    payload = load_capture("success_plain")
    event = ip.build_event(payload)

    json.dumps(event)  # must not raise - safe to XADD onto Redis
    assert event["source_row"] is not None
    assert event["event_row"] is not None
    assert event["usage_row"] is not None
    assert event["message_row"] is not None
    # timestamps are ISO strings, not datetime objects, so this is safe to XADD.
    assert isinstance(event["event_row"][ip._EVENT_TIMESTAMP_IDX], str)
    assert isinstance(event["source_row"][ip._SOURCE_INGESTED_AT_IDX], str)
    # the full original payload really is in there, untouched
    source_payload = json.loads(event["source_row"][ip._SOURCE_COLUMNS.index("raw_payload_full")])
    assert "messages" in source_payload


def test_build_event_unsuccess_failure_payload_has_no_usage_or_message_row():
    payload = load_capture("failure")
    event = ip.build_event(payload)

    assert event["event_row"] is not None
    assert event["usage_row"] is None
    assert event["message_row"] is None


def test_build_event_success_populates_invocation_row_parent_agent_id_from_header():
    # success_with_agent_and_skill has no x-claude-code-agent-id header (a main-session payload).
    # success_subagent_call's is a real subagent id.
    # Grafting the latter's header onto the former's messages gives a payload that both spawns
    # a fork and is itself a fork, so invocation_rows[0]'s parent_agent_id can be checked against
    # a real (non-blank) value.
    subagent_payload = load_capture("success_subagent_call")
    header = subagent_payload["metadata"]["requester_custom_headers"]["x-claude-code-agent-id"]

    payload = load_capture("success_with_agent_and_skill")
    payload["metadata"]["requester_custom_headers"]["x-claude-code-agent-id"] = header

    event = ip.build_event(payload)

    assert event["agent_invocation_id"] == header
    row = dict(zip(ip._INVOCATION_COLUMNS, event["invocation_rows"][0]))
    assert row["parent_agent_id"] == header


def test_build_event_success_blank_parent_agent_id_for_main_session():
    payload = load_capture("success_with_agent_and_skill")
    event = ip.build_event(payload)

    assert event["agent_invocation_id"] == ""
    row = dict(zip(ip._INVOCATION_COLUMNS, event["invocation_rows"][0]))
    assert row["parent_agent_id"] == ""


# ---------------------------------------------------------------------------
# _backfill_missing_skill_versions
# ---------------------------------------------------------------------------


def test_backfill_missing_skill_versions_fills_from_sibling_row_same_table():
    # session_id, skill_name, skill_version
    rows = [
        ["sess-1", "md-format", "1.7.1"],
        ["sess-1", "md-format", ""],
    ]
    ip._backfill_missing_skill_versions([(rows, 0, 1, 2)])
    assert rows[1][2] == "1.7.1"


def test_backfill_missing_skill_versions_fills_across_tables():
    event_rows = [["sess-1", "md-format", ""]]
    usage_rows = [["sess-1", "md-format", "1.7.1"]]
    message_rows = [["sess-1", "md-format", ""]]
    ip._backfill_missing_skill_versions([
        (event_rows, 0, 1, 2),
        (usage_rows, 0, 1, 2),
        (message_rows, 0, 1, 2),
    ])
    assert event_rows[0][2] == "1.7.1"
    assert message_rows[0][2] == "1.7.1"


def test_backfill_missing_skill_versions_does_not_leak_across_sessions():
    rows = [
        ["sess-1", "md-format", "1.7.1"],
        ["sess-2", "md-format", ""],
    ]
    ip._backfill_missing_skill_versions([(rows, 0, 1, 2)])
    assert rows[1][2] == ""


def test_backfill_missing_skill_versions_does_not_leak_across_skill_names():
    rows = [
        ["sess-1", "md-format", "1.7.1"],
        ["sess-1", "clickhouse-sql", ""],
    ]
    ip._backfill_missing_skill_versions([(rows, 0, 1, 2)])
    assert rows[1][2] == ""


def test_backfill_missing_skill_versions_leaves_genuinely_versionless_rows_blank():
    # No row in the batch ever resolved a version for this (session, skill) -
    # nothing to backfill from, so it must stay blank rather than error.
    rows = [
        ["sess-1", "Explore", ""],
        ["sess-1", "Explore", ""],
    ]
    ip._backfill_missing_skill_versions([(rows, 0, 1, 2)])
    assert rows[0][2] == ""
    assert rows[1][2] == ""


def test_backfill_missing_skill_versions_ignores_rows_with_no_skill_name():
    rows = [
        ["sess-1", "", ""],
        ["sess-1", "md-format", "1.7.1"],
    ]
    ip._backfill_missing_skill_versions([(rows, 0, 1, 2)])
    assert rows[0] == ["sess-1", "", ""]
