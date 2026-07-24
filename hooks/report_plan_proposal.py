#!/usr/bin/env python3
"""PreToolUse hook (Claude Code only, matcher: ExitPlanMode). Reports the
proposed plan text to the webhook. Exists because LiteLLM's
StandardLoggingPayload arguments come back empty "{}" for ExitPlanMode, so
the plan must be read from this hook's own tool_input instead. Must never
raise on network failures or block the tool call, but missing tracking env
vars are a misconfiguration and are allowed to crash the hook.
"""
import json
import os
import sys
import urllib.error
import urllib.request

INGEST_API_URL = os.environ["AGENT_CLI_TRACKING_API_URL"]
# Checked against LiteLLM's /key/info by the webhook before accepting the report.
LITELLM_VIRTUAL_KEY = os.environ["LITELLM_VIRTUAL_KEY"]
REQUEST_TIMEOUT = float(os.environ.get("AGENT_CLI_TRACKING_TIMEOUT", "3"))


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id", "")
    plan_text = (payload.get("tool_input") or {}).get("plan", "")
    if not session_id or not plan_text:
        return

    url = INGEST_API_URL.rstrip("/") + "/api/v1/plan-proposal"
    data = json.dumps({"session_id": session_id, "plan_text": plan_text}).encode("utf-8")
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
        print(f"[report_plan_proposal] POST failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
    sys.exit(0)
