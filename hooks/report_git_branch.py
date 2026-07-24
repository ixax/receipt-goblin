#!/usr/bin/env python3
"""SessionStart/CwdChanged hook (Claude Code; SessionStart only for Codex
CLI - see .claude/settings.json / .codex/hooks.json): reports the session's
git branch/repo to the webhook. Must never raise on git/network failures
(swallowed, logged to stderr), but missing tracking env vars are a
misconfiguration and are allowed to crash the hook rather than fall back
silently.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

INGEST_API_URL = os.environ["AGENT_CLI_TRACKING_API_URL"]
# Checked against LiteLLM's /key/info by the webhook before accepting the report.
LITELLM_VIRTUAL_KEY = os.environ["LITELLM_VIRTUAL_KEY"]
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
    # Prefer origin's URL basename so the same repo reports the same name
    # regardless of local clone dir; fall back to toplevel dir basename.
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
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LITELLM_VIRTUAL_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            resp.read()
    except Exception as exc:
        print(f"[report_git_branch] POST failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
    sys.exit(0)
