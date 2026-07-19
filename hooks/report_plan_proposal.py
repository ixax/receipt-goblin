#!/usr/bin/env python3
"""PreToolUse hook (Claude Code only, matcher: ExitPlanMode - Codex CLI has
no plan-mode equivalent): reports the proposed plan text to the webhook.
Exists because LiteLLM's StandardLoggingPayload doesn't carry it -
agent_events.raw_payload's tool_calls[0].function.arguments comes back as an
empty "{}" for every observed ExitPlanMode call, unlike every other tool
(confirmed against live data), so the plan has to be read straight from this
hook's own tool_input instead. See plan_proposals in clickhouse/schema.sql.
Stdlib only. Must never raise on network failures (swallowed and logged to
stderr) or block the tool call - but AGENT_CLI_TRACKING_API_URL/
LITELLM_VIRTUAL_KEY have no fallback, so a missing/unset value is a
misconfiguration, not a transient failure, and is allowed to crash this hook
(KeyError, non-zero exit) rather than silently pointing at a guessed URL or
skipping auth.
"""
import json
import os
import sys
import urllib.error
import urllib.request

INGEST_API_URL = os.environ["AGENT_CLI_TRACKING_API_URL"]
# Personal LiteLLM virtual key (see `make env`) - webhook checks this
# against LiteLLM's own /key/info before accepting the report, so this
# isn't just a header we're adding for show.
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
