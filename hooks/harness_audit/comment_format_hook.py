#!/usr/bin/env python3
"""PostToolUse hook: check md-format's one-sentence-per-line rule against
comments/docstrings in an edited .py/.yml/.yaml file. Stdlib only.

Wire in .claude/settings.json:
  "PostToolUse": [{"matcher": "Edit|Write",
    "hooks": [{"type": "command",
               "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/comment_format_hook.py\""}]}]
Exit 2 feeds violations back to the editing agent (works for agents without Bash).

Scoped to what the agent just wrote, not the whole file: for Edit, only
tool_input.new_string is checked, so pre-existing violations elsewhere in
a large file never block an unrelated edit. For Write the agent owns the
whole file's content, so the full tool_input.content is checked.
"""
import json
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
from comment_format import check_text  # noqa: E402


def main() -> int:
    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    path = tool_input.get("file_path", "")
    if not path or not path.endswith((".py", ".yml", ".yaml")):
        return 0
    is_py = path.endswith(".py")

    if tool_name == "Edit":
        text = tool_input.get("new_string", "")
    elif tool_name == "Write":
        text = tool_input.get("content", "")
    else:
        return 0

    violations = check_text(text, is_py)
    if violations:
        lines = "\n".join(f"  ~{i}: {s}" for i, s in violations)
        print(
            f"md-format one-sentence-per-line (comment) violated in {path}:\n{lines}\n"
            "Split each flagged line so it holds one sentence, per .claude/skills/md-format/SKILL.md.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
