#!/usr/bin/env python3
"""Handles Codex CLI's SessionStart hook. Codex has no SessionEnd event
(only the 5 stable events plus SubagentStart/Stop, PreCompact/PostCompact,
PermissionRequest), so there's nothing to mirror .claude/hooks/log_session.py's
SessionEnd branch. Also skips that script's registry scan - Codex has no
.claude/agents or .claude/skills equivalent to register."""
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / ".claude" / "hooks"))
import common  # noqa: E402
from common import get_user_id, post_json, read_hook_input  # noqa: E402

# See log_event.py for why this is a bound partial rather than a global.
next_ids = partial(common.next_ids, namespace=".codex")


def main():
    payload = read_hook_input()
    session_id = payload.get("session_id", "")
    user_id = get_user_id()

    turn_id, sequence_id = next_ids(session_id, new_turn=False)

    post_json(
        "/ingest/event",
        {
            "session_id": session_id,
            "trace_id": session_id,
            "turn_id": turn_id,
            "sequence_id": sequence_id,
            "event_type": "SessionStart",
            "status": payload.get("source", ""),
            "raw_payload": payload,
        },
        user_id=user_id,
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
