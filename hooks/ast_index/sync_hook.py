#!/usr/bin/env python3
"""PostToolUse hook: keep agent_docs/ast_index/ live during a session.
Regenerates the edited file's cache entry after an Edit/Write to a tracked .py file.
Stdlib only.

Wire in .claude/settings.json:
  "PostToolUse": [{"matcher": "Edit|Write",
    "hooks": [{"type": "command",
               "command": "uv run python3 \"$CLAUDE_PROJECT_DIR/hooks/ast_index/sync_hook.py\""}]}]
Exit 2 on regeneration failure (works for agents without Bash).

Separate from hooks/harness_audit/sync_hook.py deliberately.
That hook keeps .claude/agents/*.md and .codex/agents/*.toml compiled from .agents/agents/*.yaml, this one keeps agent_docs/ast_index/ in sync.
One hook, one job, per this repo's convention (report_git_branch.py, guard_destructive.py).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOK_DIR.parent.parent
AST_INDEX_SCRIPT = REPO_ROOT / "scripts" / "ast_index.py"

EXCLUDE_DIR_NAMES = {".venv", "__pycache__", "node_modules", ".git"}


def is_tracked_source(rel: str) -> bool:
    """True if rel is a .py file scripts/ast_index.py would index."""
    if not rel.endswith(".py"):
        return False
    parts = Path(rel).parts
    return not any(part in EXCLUDE_DIR_NAMES for part in parts)


def main() -> int:
    payload = json.load(sys.stdin)
    path = payload.get("tool_input", {}).get("file_path", "")
    rel = os.path.relpath(os.path.realpath(path), os.getcwd()) if path else ""
    rel = rel.replace(os.sep, "/")

    if not is_tracked_source(rel):
        return 0

    result = subprocess.run(
        [sys.executable, str(AST_INDEX_SCRIPT), "build", "--file", rel],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"ast-index regeneration failed after editing {rel}:\n{result.stderr}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
