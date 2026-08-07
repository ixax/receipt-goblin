#!/usr/bin/env python3
"""PostToolUse hook: check md-format's one-sentence-per-line rule against
comments/docstrings in an edited .py/.yml/.yaml file, or against
description/rawSql-comment prose in an edited Grafana dashboard .json
file.
Stdlib only.

Wire in .claude/settings.json:
  "PostToolUse": [{"matcher": "Edit|Write",
    "hooks": [{"type": "command",
               "command": "uv run python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/comment_format_hook.py\""}]}]
Exit 2 feeds violations back to the editing agent (works for agents without Bash).

Scoped to what the agent just wrote, not the whole file: for Edit, only
tool_input.new_string is checked, so pre-existing violations elsewhere in
a large file never block an unrelated edit.
For Write the agent owns the whole file's content, so the full
tool_input.content is checked.
"""
import json
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
from comment_format import (  # noqa: E402
    check_agent_yaml_text,
    check_json_text,
    check_text,
    is_agent_yaml,
    is_dashboard_json,
)
from md_format_skill_gate import under_excluded_dir  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    path = tool_input.get("file_path", "")
    is_json = is_dashboard_json(path)
    is_yaml_agent = is_agent_yaml(path)
    if not path or under_excluded_dir(path) or not (path.endswith((".py", ".yml", ".yaml")) or is_json):
        return 0

    if tool_name == "Edit":
        text = tool_input.get("new_string", "")
    elif tool_name == "Write":
        text = tool_input.get("content", "")
    else:
        return 0

    if is_json:
        violations = check_json_text(text)
        kind = "json"
    elif is_yaml_agent:
        violations = check_agent_yaml_text(text)
        kind = "agent-yaml"
    else:
        violations = check_text(text, path.endswith(".py"))
        kind = "comment"
    if violations:
        lines = "\n".join(f"  ~{i}: {s}" for i, s in violations)
        print(
            f"md-format one-sentence-per-line ({kind}) violated in {path}:\n{lines}\n"
            "Split each flagged line so it holds one sentence, per .agents/skills/md-format/SKILL.md.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
