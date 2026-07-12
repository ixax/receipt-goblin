#!/usr/bin/env python3
"""SessionStart hook (Claude Code and Codex CLI, see .claude/settings.json /
.codex/hooks.json): reports the current git branch for this session to the
webhook. This is the one lifecycle hook this stack still has - see
session_git_branch in clickhouse/schema.sql for why. Stdlib only, must never
raise or block/slow down the CLI session it runs in.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

INGEST_API_URL = os.environ.get("AGENT_CLI_TRACKING_API_URL", "http://localhost:8010")
REQUEST_TIMEOUT = float(os.environ.get("AGENT_CLI_TRACKING_TIMEOUT", "3"))


def _current_branch(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd") or os.getcwd()
    git_branch = _current_branch(cwd)
    if not session_id or not git_branch:
        return

    url = INGEST_API_URL.rstrip("/") + "/api/v1/session-git-branch"
    data = json.dumps({"session_id": session_id, "git_branch": git_branch}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            resp.read()
    except Exception as exc:
        print(f"[report_git_branch] POST failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
    sys.exit(0)
