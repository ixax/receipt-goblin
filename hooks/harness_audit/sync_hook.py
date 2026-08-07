#!/usr/bin/env python3
"""PostToolUse hook: recompile .claude/agents/*.md and .codex/agents/*.toml
after an .agents/agents/*.yaml edit.
Stdlib only.

Wire in .claude/settings.json:
  "PostToolUse": [{"matcher": "Edit|Write",
    "hooks": [{"type": "command",
               "command": "uv run python3 \"$CLAUDE_PROJECT_DIR/hooks/harness_audit/sync_hook.py\""}]}]
Exit 2 on compile failure (works for agents without Bash, e.g. harness-expert).

compile_agents.py has no per-agent filter - every invocation regenerates all
agents from their .agents/agents/*.yaml sources.
So this hook always runs a full compile rather than targeting just the touched file.

Separate from audit_hook.py deliberately - that hook checks budgets, this one
keeps the compiled outputs in sync with their YAML source.
One hook, one job, per this repo's convention (report_git_branch.py, guard_destructive.py).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOOK_DIR.parent.parent
COMPILE_AGENTS_SCRIPT = REPO_ROOT / "scripts" / "compile_agents.py"


def is_index_source(rel: str) -> bool:
    """True if rel is an agent source YAML that scripts/compile_agents.py
    compiles into .claude/agents/*.md and .codex/agents/*.toml.
    """
    return rel.startswith(os.path.join(".agents", "agents") + os.sep) and rel.endswith(".yaml")


def main() -> int:
    payload = json.load(sys.stdin)
    path = payload.get("tool_input", {}).get("file_path", "")
    rel = os.path.relpath(os.path.realpath(path), os.getcwd()) if path else ""

    if not is_index_source(rel):
        return 0

    result = subprocess.run(
        [sys.executable, str(COMPILE_AGENTS_SCRIPT)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(
            f"agent compile failed after editing {rel}:\n{result.stderr}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
