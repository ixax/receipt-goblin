#!/usr/bin/env python3
"""Handles SessionStart and SessionEnd hooks.

SessionStart additionally scans .claude/agents and .claude/skills and
upserts every agent/skill it finds into the ClickHouse registries, so a
fresh clone/checkout is immediately reflected in Grafana without a manual
registration step.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import get_user_id, next_ids, post_json, read_hook_input  # noqa: E402
from register_agents import scan_and_register  # noqa: E402


def main():
    payload = read_hook_input()
    event_type = payload.get("hook_event_name", "SessionStart")
    session_id = payload.get("session_id", "")
    user_id = get_user_id()

    turn_id, sequence_id = next_ids(session_id, new_turn=False)

    if event_type == "SessionStart":
        registered = scan_and_register(user_id=user_id)
        payload["_registered"] = registered

    post_json(
        "/ingest/event",
        {
            "session_id": session_id,
            "trace_id": session_id,
            "turn_id": turn_id,
            "sequence_id": sequence_id,
            "event_type": event_type,
            "status": payload.get("reason", ""),
            "raw_payload": payload,
        },
        user_id=user_id,
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
