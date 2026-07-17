#!/usr/bin/env python3
"""SessionStart hook (Claude Code and Codex CLI, see .claude/settings.json /
.codex/hooks.json): reports the current git branch and repo for this session
to the webhook. This is the one lifecycle hook this stack still has - see
session_git_branch in clickhouse/schema.sql for why. Stdlib only. Must never
raise on git/network failures (those are swallowed and logged to stderr) -
but AGENT_CLI_TRACKING_API_URL has no fallback, so a missing/unset value is
a misconfiguration, not a transient failure, and is allowed to crash this
hook (KeyError, non-zero exit) rather than silently pointing at a guessed URL.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

INGEST_API_URL = os.environ["AGENT_CLI_TRACKING_API_URL"]
REQUEST_TIMEOUT = float(os.environ.get("AGENT_CLI_TRACKING_TIMEOUT", "3"))


def _run_git(cwd: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def _current_branch(cwd: str) -> str:
    return _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def _current_repo(cwd: str) -> str:
    # Prefer the "origin" remote's URL basename, so the same repo reports
    # the same name regardless of what its local clone directory is called.
    # Falls back to the toplevel directory's basename when there's no
    # "origin" remote (e.g. a local-only repo).
    remote_url = _run_git(cwd, "remote", "get-url", "origin")
    if remote_url:
        name = remote_url.rstrip("/").rsplit("/", 1)[-1]
        return name[:-4] if name.endswith(".git") else name

    toplevel = _run_git(cwd, "rev-parse", "--show-toplevel")
    return os.path.basename(toplevel) if toplevel else ""


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd") or os.getcwd()
    git_branch = _current_branch(cwd)
    git_repo = _current_repo(cwd)
    if not session_id or not git_branch:
        return

    url = INGEST_API_URL.rstrip("/") + "/api/v1/session-git-branch"
    data = json.dumps({
        "session_id": session_id, "git_branch": git_branch, "git_repo": git_repo,
    }).encode("utf-8")
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
