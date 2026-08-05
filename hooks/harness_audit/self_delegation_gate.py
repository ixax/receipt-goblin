#!/usr/bin/env python3
"""PreToolUse hook: deny open-ended read-only research (Grep, Glob, and
investigation-shaped Bash) when issued directly by the main agent instead
of delegated to code-locator/Explore/script-ops.
Stdlib only.

Root cause this fixes: delegation of read-only research to script-ops/
code-locator/Explore was purely advisory.
It was one AGENTS.md bullet plus prose in the agents' own descriptions, with
no mechanical enforcement.
`.claude/settings.local.json` pre-allows many of these commands
(`git status*`, `git diff*`, `git log*`, `ls*`, `find*`, `grep*`) with zero
friction, so nothing stopped the main agent from just running them inline.

Wire in .claude/settings.json:
  "PreToolUse": [
    {"matcher": "Grep|Glob",
     "hooks": [{"type": "command",
                "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/self_delegation_gate.py\""}]},
    {"matcher": "Bash",
     "hooks": [..., {"type": "command",
                "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/self_delegation_gate.py\""}]}
  ]

Scope guard: fires only for the main agent's own tool calls.
`transcript_path` is NOT the signal - it's identical (the top-level
session file) whether the call came from the main agent or a subagent,
confirmed by logging a live payload from each.
The actual signal is `agent_id`/`agent_type`: present only when a
Task/Agent-tool-spawned subagent issues the call, absent for the main
agent's own calls.
script-ops needs Bash, code-locator/Explore need Grep/Glob, so any call
carrying `agent_id` is allowed unconditionally.
"""
import json
import re
import sys

GREP_GLOB_MESSAGE = (
    "Do not run Grep or Glob directly in the main conversation: dispatch "
    "code-locator for a small/known search, or Explore/script-ops for "
    "broader discovery."
)

BASH_MESSAGE = (
    "read-only repo investigation belongs to script-ops - hand it a goal, "
    "not a command."
)

# Investigation-shaped Bash, mirroring script-ops.md's stated scope.
DENYLIST = [
    re.compile(r"\bgit\s+(log|diff|show|blame)\b"),
    re.compile(r"\bfind\b"),
    re.compile(r"\bgrep\b"),
    re.compile(r"\brg\b"),
    re.compile(r"\bdocker\s+(logs|ps|inspect)\b"),
]


def ls_recursive(command: str) -> bool:
    """`ls -R`/`ls -lR` (recursive listing) only - plain `ls` stays allowed."""
    for segment in re.split(r"[|;&]", command):
        m = re.search(r"(?:^|\s)ls\b(.*)$", segment)
        if not m:
            continue
        for arg in m.group(1).split():
            if arg.startswith("-") and "R" in arg[1:]:
                return True
    return False


def cat_multi_file(command: str) -> bool:
    """Multi-file `cat` dump (2+ file args) - a single `cat file` stays allowed."""
    for segment in re.split(r"[|;&]", command):
        m = re.search(r"\bcat\b(.*)$", segment)
        if not m:
            continue
        args = [a for a in m.group(1).split() if a and not a.startswith("-")]
        if len(args) >= 2:
            return True
    return False


def bash_denylisted(command: str) -> bool:
    if any(pattern.search(command) for pattern in DENYLIST):
        return True
    return ls_recursive(command) or cat_multi_file(command)


def main() -> int:
    payload = json.load(sys.stdin)
    if payload.get("agent_id"):
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name in ("Grep", "Glob"):
        message = GREP_GLOB_MESSAGE
    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if not bash_denylisted(command):
            return 0
        message = BASH_MESSAGE
    else:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
