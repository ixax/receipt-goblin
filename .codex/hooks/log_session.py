#!/usr/bin/env python3
"""Handles Codex CLI's SessionStart hook. Codex has no SessionEnd event
(only the 5 stable events plus SubagentStart/Stop, PreCompact/PostCompact,
PermissionRequest), so there's nothing to mirror ClaudeSessionHook's
SessionEnd branch. Also skips the registry scan ClaudeSessionHook does on
SessionStart - Codex has no .claude/agents or .claude/skills equivalent to
register, so BaseSessionHook's default no-op on_session_start() is used
as-is."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hooks"))
from base import BaseSessionHook  # noqa: E402


class CodexSessionHook(BaseSessionHook):
    def extract_status(self):
        return self.payload.get("source", "")


if __name__ == "__main__":
    CodexSessionHook().run()
    sys.exit(0)
