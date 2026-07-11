#!/usr/bin/env python3
"""Handles SessionStart and SessionEnd hooks.

SessionStart additionally scans .claude/agents and .claude/skills and
upserts every agent/skill it finds into the ClickHouse registries, so a
fresh clone/checkout is immediately reflected in Grafana without a manual
registration step.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from base import BaseSessionHook  # noqa: E402
from register_agents import scan_and_register  # noqa: E402


class ClaudeSessionHook(BaseSessionHook):
    def event_type(self):
        return self.payload.get("hook_event_name", "SessionStart")

    def on_session_start(self, event_type):
        if event_type == "SessionStart":
            registered = scan_and_register(user_id=self.user_id)
            self.payload["_registered"] = registered

    def extract_status(self):
        return self.payload.get("reason", "")


if __name__ == "__main__":
    ClaudeSessionHook().run()
    sys.exit(0)
