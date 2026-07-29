#!/usr/bin/env python3
"""PreToolUse guard for Bash: forces a confirmation prompt on destructive
DB/infra commands, overriding any blanket Bash allow rule (e.g. Bash(*) in
a local settings override)."""
import json
import re
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
]


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
