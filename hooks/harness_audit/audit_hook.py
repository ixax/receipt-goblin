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


# Violation kinds that inherently span multiple files (duplicate-rule
# detection, aggregate byte/token totals, the CLAUDE.md/AGENTS.md pairing
# check) - these stay in the gate for every harness edit since fixing them
# genuinely may require touching a file other than the one just edited.
# Per-file kinds (budget, description word count, dead reference, volatile
# content, md-format one-sentence-per-line) are filtered to the edited
# file only, below - otherwise a large pre-existing backlog anywhere in
# the tree would block every future unrelated edit.
GLOBAL_VIOLATION_PREFIXES = ("duplicate rule in", "AGENTS.md chain")


def relevant_violations(report: str, rel: str):
    lines = []
    for line in report.splitlines():
        s = line.strip()
        if not s.startswith("- "):
            lines.append(line)
            continue
        body = s[2:]
        if body.startswith(rel + ":") or body.startswith(GLOBAL_VIOLATION_PREFIXES):
            lines.append(line)
        elif ": CLAUDE.md and AGENTS.md are separate files" in body and (
            rel in ("CLAUDE.md", "AGENTS.md") or rel.endswith((os.sep + "CLAUDE.md", os.sep + "AGENTS.md"))
        ):
            lines.append(line)
    return "\n".join(lines)


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
        report = relevant_violations(report, rel)
        if not report.strip() or report.strip() == "VIOLATIONS:":
            return 0
        print(f"Harness budget audit failed after editing {rel}:\n{report}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
