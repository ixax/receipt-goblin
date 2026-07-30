#!/usr/bin/env python3
"""UserPromptSubmit hook: warns when the session's context has grown large enough that, if `/goal` is active, its periodic judge_call checks would be expensive.
Claude Code's /goal judge_call always misses prompt cache and rewrites the full current context on every check (see `agent_docs/incidents.md`, "/goal judge calls never hit prompt cache") - cost scales with context size, not with anything we can see or control from a hook.
This can't detect whether /goal is actually set, so the warning is phrased conditionally and is advisory only, not a block.

Reads total context size (cache_read + cache_creation + input tokens) off the last assistant turn in the session transcript, so it needs no ClickHouse round-trip and reflects the current turn immediately.
Warns at most once per threshold tier per session (state file under the system temp dir), so it doesn't repeat on every prompt once a tier is crossed.
"""
import json
import os
import sys
import tempfile

# (token_threshold, label) - thresholds derived from `agent_docs/incidents.md`'s
# judge_call cost data: judge cache-write cost scales roughly $2.50 per 1000
# tokens, so these correspond to roughly $0.20 / $0.50 / $1.00+ per judge check.
TIERS = [
    (80_000, "moderate"),
    (200_000, "high"),
    (400_000, "critical"),
]

STATE_DIR = os.path.join(tempfile.gettempdir(), "claude_goal_scope_guard")


def _last_assistant_usage(transcript_path: str) -> dict:
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
    except OSError:
        return {}

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        usage = (entry.get("message") or {}).get("usage")
        if usage:
            return usage
    return {}


def _highest_tier_crossed(total_tokens: int) -> int:
    crossed = -1
    for i, (threshold, _label) in enumerate(TIERS):
        if total_tokens >= threshold:
            crossed = i
    return crossed


def _last_warned_tier(session_id: str) -> int:
    path = os.path.join(STATE_DIR, f"{session_id}.json")
    try:
        with open(path, "r") as f:
            return json.load(f).get("last_warned_tier", -1)
    except (OSError, json.JSONDecodeError):
        return -1


def _record_warned_tier(session_id: str, tier: int) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{session_id}.json")
    with open(path, "w") as f:
        json.dump({"last_warned_tier": tier}, f)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = payload.get("session_id", "")
    transcript_path = payload.get("transcript_path", "")
    if not session_id or not transcript_path:
        return

    usage = _last_assistant_usage(transcript_path)
    if not usage:
        return

    total_tokens = (
        usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("input_tokens", 0)
    )

    tier = _highest_tier_crossed(total_tokens)
    if tier < 0 or tier <= _last_warned_tier(session_id):
        return

    _, label = TIERS[tier]
    print(
        f"[goal_scope_guard] Session context is now ~{total_tokens:,} tokens ({label} tier). "
        "If /goal is active, each judge_call check rewrites the entire context to cache "
        "(no cache hit possible) - cost scales with this number. Consider `/goal clear` "
        "or wrapping up soon. See agent_docs/incidents.md for the underlying data."
    )
    _record_warned_tier(session_id, tier)


if __name__ == "__main__":
    main()
    sys.exit(0)
