#!/usr/bin/env python3
"""PostToolUse hook: run harness audit when a harness file is edited. Stdlib only.

Wire in .claude/settings.json:
  "PostToolUse": [{"matcher": "Edit|Write",
    "hooks": [{"type": "command",
               "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/audit_hook.py\""}]}]
Exit 2 feeds violations back to the editing agent (works for agents without Bash).

Index regeneration (agent_docs/harness-index.md) is a separate concern, its
own hook: sync_hook.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
AUDIT_SCRIPT = HOOK_DIR / "audit.py"


def is_harness_path(rel: str) -> bool:
    """True if rel is under either CLI's harness tree, under agent_docs/ (deep-dive
    docs harness-expert owns), or is an AGENTS.md/CLAUDE.md anywhere. Dual-harness
    aware - this repo tracks .claude/ and .codex/ equally.
    """
    return (
        rel.startswith(".claude" + os.sep)
        or rel.startswith(".codex" + os.sep)
        or rel.startswith("agent_docs" + os.sep)
        or os.path.basename(rel) in ("AGENTS.md", "CLAUDE.md")
        or os.sep + "AGENTS.md" in rel
        or os.sep + "CLAUDE.md" in rel
    )


def main() -> int:
    payload = json.load(sys.stdin)
    path = payload.get("tool_input", {}).get("file_path", "")
    # Resolve both sides before relpath: os.getcwd() returns a symlink-resolved
    # path, so an unresolved file_path (e.g. through a symlinked tmp dir) would
    # otherwise produce a "../../.." relpath that fails the prefix checks below.
    rel = os.path.relpath(os.path.realpath(path), os.getcwd()) if path else ""

    if not is_harness_path(rel):
        return 0

    result = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), "."],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        marker = "VIOLATIONS:"
        out = result.stdout
        report = out[out.index(marker):] if marker in out else out
        print(f"Harness budget audit failed after editing {rel}:\n{report}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
