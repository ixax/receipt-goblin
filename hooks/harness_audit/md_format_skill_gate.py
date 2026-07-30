#!/usr/bin/env python3
"""PreToolUse hook: force reading the md-format skill before the FIRST
Edit/Write in a session that touches markdown prose or a multi-line
comment/docstring.
Stdlib only.

Root cause this fixes: md-format's SKILL.md description says "read BEFORE
EVERY Edit/Write touching .md prose ... and before writing a multi-sentence
comment", but that's advisory.
The agent may or may not act on it.
The reactive PostToolUse hooks (comment_format_hook.py, audit_hook.py) only
catch one narrow rule (one-sentence-per-line, via regex) after the fact,
and only for that one rule.
They say nothing about heading hierarchy, enumeration-vs-list, quoting, or
table alignment, and they never make the agent actually read the skill's
reasoning.
This hook makes the read itself mandatory, once per session, before the
qualifying edit lands.

Wire in .claude/settings.json:
  "PreToolUse": [{"matcher": "Edit|Write",
    "hooks": [{"type": "command",
               "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/md_format_skill_gate.py\""}]}]

Detects a prior read two ways, since not every agent has the Skill tool -
most Subagents don't (`tools:` in their frontmatter omits it) and instead
read a skill's SKILL.md directly with Read, per their own body text (e.g.
harness-expert, dashboard-panels-builder). So "already read" means either:
  1. a Skill tool_use with input.skill == "md-format", or
  2. a Read tool_use whose file_path is md-format's SKILL.md,
anywhere earlier in the session transcript. Once read, the skill's content
is already in context, so the gate stays open for the rest of the session.
"""
import json
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
from comment_format import comment_lines_in_text  # noqa: E402

SKILL_NAME = "md-format"
SKILL_FILE_SUFFIX = str(Path(".claude") / "skills" / "md-format" / "SKILL.md")


def _block_satisfies_gate(block: dict) -> bool:
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return False
    name = block.get("name")
    tool_input = block.get("input") or {}
    if name == "Skill" and tool_input.get("skill") == SKILL_NAME:
        return True
    if name == "Read" and str(tool_input.get("file_path", "")).endswith(SKILL_FILE_SUFFIX):
        return True
    return False


def _subagent_transcripts(transcript_path: str) -> list:
    """Task-spawned subagents get their own transcript file under
    <session-dir>/subagents/agent-<id>.jsonl, sibling to the main
    <session-id>.jsonl the orchestrator writes to.
    A subagent's own Skill/Read calls land only in its own file, never in the main one.
    So a subagent-issued Edit/Write must check both, or a compliant subagent can never satisfy this gate on its own.
    Confirmed live: harness-expert and dev-ops each read the skill in-session and were denied on every retry regardless."""
    p = Path(transcript_path)
    subagents_dir = p.parent / p.stem / "subagents"
    if not subagents_dir.is_dir():
        return []
    return sorted(str(f) for f in subagents_dir.glob("agent-*.jsonl"))


def already_read(transcript_path: str) -> bool:
    for path in [transcript_path, *_subagent_transcripts(transcript_path)]:
        try:
            with open(path, "r") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            for block in (entry.get("message") or {}).get("content") or []:
                if _block_satisfies_gate(block):
                    return True
    return False


def has_multiline_comment_block(text: str, is_py: bool) -> bool:
    """Heuristic for the SKILL's "multi-sentence comment/docstring" trigger:
    2+ consecutive comment/docstring lines is treated as a likely
    multi-sentence block, whether or not it's already correctly split."""
    prev_lineno = None
    run_len = 0
    for lineno, content in comment_lines_in_text(text, is_py):
        if not content:
            continue
        if prev_lineno is not None and lineno == prev_lineno + 1:
            run_len += 1
        else:
            run_len = 1
        prev_lineno = lineno
        if run_len >= 2:
            return True
    return False


def qualifies(path: str, text: str) -> bool:
    if path.endswith(".md"):
        return True
    if path.endswith((".py", ".yml", ".yaml")):
        return has_multiline_comment_block(text, path.endswith(".py"))
    return False


def main() -> int:
    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    path = tool_input.get("file_path", "")
    if not path:
        return 0

    if tool_name == "Edit":
        text = tool_input.get("new_string", "")
    elif tool_name == "Write":
        text = tool_input.get("content", "")
    else:
        return 0

    if not qualifies(path, text):
        return 0

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path or already_read(transcript_path):
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Read the md-format skill before editing {path}: "
                "call Skill(md-format) if you have that tool, otherwise "
                "Read .claude/skills/md-format/SKILL.md directly, then "
                "retry this edit. Required once per session before the "
                "first markdown-prose or multi-sentence-comment edit."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
