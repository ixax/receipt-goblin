#!/usr/bin/env python3
"""PreToolUse hook: same md-format skill gate as md_format_skill_gate.py,
but for Bash commands that write a .md/.py/.yml/.yaml file directly
(heredoc, `>`/`>>` redirection, `tee`) instead of going through the Edit
or Write tools.
Stdlib only.

Root cause this fixes: md_format_skill_gate.py only matches the Edit/Write
tools.
An agent with Bash access (most subagents that write docs) can write a
.md file with `cat > file.md <<EOF ... EOF` and the Edit/Write gate never
sees the call, so the skill is never enforced.

Wire in .claude/settings.json, as a second hook under the existing "Bash"
PreToolUse matcher:
  "PreToolUse": [{"matcher": "Bash",
    "hooks": [..., {"type": "command",
               "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/md_format_bash_gate.py\""}]}]

Heuristic only, deliberately narrow: it looks for an explicit redirection
target (`>`, `>>`, or `tee`) ending in one of the four extensions, and for
.py/.yml/.yaml only fires when a heredoc body is present and contains a
2+-line comment run.
Commands with no matched target (e.g. `sed -i`, `python -c "..."`, a
Makefile recipe) are intentionally left uncovered - broadening this to
catch every possible write path risks blocking unrelated Bash commands on
false positives, which is worse than the gap it leaves.
"""
import json
import re
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
from md_format_skill_gate import (  # noqa: E402
    already_read,
    has_multiline_comment_block,
    under_excluded_dir,
)

TARGET_RE = re.compile(
    r"(?:>{1,2}|\btee\b(?:\s+-a)?)\s+"
    r"(['\"]?)([^\s'\";|&<>]+\.(?:md|py|ya?ml))\1"
)
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def heredoc_body(command: str) -> str:
    """First heredoc's body text, or "" if the command has none."""
    m = HEREDOC_RE.search(command)
    if not m:
        return ""
    terminator = m.group(2)
    rest = command[m.end():]
    lines = rest.splitlines()
    body = []
    for line in lines:
        if line.strip() == terminator:
            break
        body.append(line)
    return "\n".join(body)


def qualifies(command: str) -> str:
    """Returns the qualifying target path, or "" if nothing qualifies."""
    targets = [m.group(2) for m in TARGET_RE.finditer(command)]
    if not targets:
        return ""
    body = heredoc_body(command)
    for target in targets:
        if under_excluded_dir(target):
            continue
        if target.endswith(".md"):
            return target
        if target.endswith((".py", ".yml", ".yaml")) and body:
            if has_multiline_comment_block(body, target.endswith(".py")):
                return target
    return ""


def main() -> int:
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command", "")
    target = qualifies(command)
    if not target:
        return 0

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path or already_read(transcript_path):
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Read the md-format skill before writing {target} via Bash: "
                "call Skill(md-format) if you have that tool, otherwise "
                "Read .claude/skills/md-format/SKILL.md directly, then "
                "retry this command. Required once per session before the "
                "first markdown-prose or multi-sentence-comment write, "
                "including writes made through Bash instead of Edit/Write."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
