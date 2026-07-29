#!/usr/bin/env python3
"""PostToolUse hook: regenerate agent_docs/harness-index.md after a
SKILL.md/agent body edit. Stdlib only.

Wire in .claude/settings.json:
  "PostToolUse": [{"matcher": "Edit|Write",
    "hooks": [{"type": "command",
               "command": "python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/sync_hook.py\""}]}]
Exit 2 on regeneration failure (works for agents without Bash).

Separate from audit_hook.py deliberately - that hook checks budgets, this one
keeps the generated index in sync. One hook, one job, per this repo's
convention (report_git_branch.py, guard_destructive.py).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOK_DIR.parent.parent
SYNC_HARNESS_SCRIPT = REPO_ROOT / "scripts" / "sync_harness.py"


def is_index_source(rel: str) -> bool:
    """True if rel is one of the files scripts/sync_harness.py derives
    agent_docs/harness-index.md from: any SKILL.md (either CLI's skills tree),
    or a Subagent body under .claude/agents/.
    """
    return (
        os.path.basename(rel) == "SKILL.md"
        or (rel.startswith(os.path.join(".claude", "agents") + os.sep) and rel.endswith(".md"))
    )


def main() -> int:
    payload = json.load(sys.stdin)
    path = payload.get("tool_input", {}).get("file_path", "")
    rel = os.path.relpath(os.path.realpath(path), os.getcwd()) if path else ""

    if not is_index_source(rel):
        return 0

    result = subprocess.run(
        [sys.executable, str(SYNC_HARNESS_SCRIPT)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"harness-index regeneration failed after editing {rel}:\n{result.stderr}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
