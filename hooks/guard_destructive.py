#!/usr/bin/env python3
"""PreToolUse guard for Bash: forces a confirmation prompt on destructive
DB/infra commands, overriding any blanket Bash allow rule (e.g. Bash(*) in
a local settings override)."""
import json
import re
import subprocess
import sys

PATTERNS = [
    (r"\bdrop\s+table\b", "DROP TABLE"),
    (r"\bdrop\s+database\b", "DROP DATABASE"),
    (r"\btruncate\s+table\b", "TRUNCATE TABLE"),
    (r"\btruncate\b", "TRUNCATE"),
    (r"\balter\s+table\b.*\bdrop\s+partition\b", "ALTER TABLE ... DROP PARTITION"),
    (r"docker\s+volume\s+rm", "docker volume rm"),
    (r"docker\s+compose\s+down\b.*(-v\b|--volumes\b)", "docker compose down -v/--volumes"),
    (r"docker\s+system\s+prune", "docker system prune"),
    (r"\brm\s+-rf\b", "rm -rf"),
    (r"git\s+push\b.*--force", "git push --force"),
    (r"git\s+reset\s+--hard\b", "git reset --hard"),
    (r"git\s+clean\s+-f", "git clean -f"),
    (r"git\s+checkout\s+--(\s|$)", "git checkout --"),
    (r"git\s+restore\b", "git restore"),
]

# `git stash` snapshots and clears the *entire* working tree, not just files the current agent/session touched.
# On a repo checkout shared with other concurrent Claude Code sessions, a bare `stash`/`stash pop` cycle can silently grab another session's in-flight uncommitted edits.
# A later pop can then conflict with, or if mishandled lose, that unrelated work.
# See `agent_docs/git-safety.md`'s `git stash` section - discovered live, not hypothetical.
_STASH_RE = re.compile(r"\bgit\s+stash\b")


def _stash_status_note(cwd: str) -> str:
    """Best-effort `git status --short` summary for the prompt.
    Shows the user what a bare `git stash` is about to sweep up before they decide.
    Empty string on any failure (git missing, not a repo, etc), never raises."""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return ""
    preview = "\n".join(lines[:15])
    more = f"\n... and {len(lines) - 15} more" if len(lines) > 15 else ""
    return (
        f"\n\n{len(lines)} uncommitted path(s) currently in the tree - confirm every one of these is yours "
        f"before stashing (a bare `git stash` sweeps up all of them, not just what you edited):\n{preview}{more}"
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if payload.get("tool_name") != "Bash":
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return
    lowered = command.lower()

    if _STASH_RE.search(lowered):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    "Command matches 'git stash'. This repo's working tree is often shared with other "
                    "concurrent sessions - stash grabs every uncommitted change, not just yours, and a "
                    "later pop can collide with unrelated in-flight work. If anything in the list below "
                    "isn't something you edited yourself this task, stop and ask the user for confirmation "
                    "instead of proceeding."
                    + _stash_status_note(payload.get("cwd", ""))
                ),
            }
        }))
        return

    for pattern, label in PATTERNS:
        if re.search(pattern, lowered):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": (
                        f"Command matches destructive pattern '{label}'. "
                        "Confirm this is intentional before it runs."
                    ),
                }
            }))
            return


if __name__ == "__main__":
    main()
