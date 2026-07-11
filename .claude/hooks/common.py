"""Shared helpers for Claude Code tracking hooks.

Stdlib only - no third-party dependencies, so these hooks run on any host
that has a plain Python 3 interpreter (Windows/macOS/Linux), no venv needed.
"""
import getpass
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

INGEST_API_URL = os.environ.get("AGENT_CLI_TRACKING_API_URL", "http://localhost:8000")
REQUEST_TIMEOUT = float(os.environ.get("AGENT_CLI_TRACKING_TIMEOUT", "3"))
DEBUG = os.environ.get("AGENT_CLI_TRACKING_DEBUG") == "1"


def debug(msg):
    if DEBUG:
        print(f"[claude-tracking][debug] {msg}", file=sys.stderr)


def _claude_account_email():
    """Best-effort read of the logged-in Claude account's email from the
    global (not project-scoped) ~/.claude.json. Undocumented, internal
    Claude Code state - shape can change between versions, so this must
    never raise and must degrade to "" (caller falls back to host+user)."""
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        return (data.get("oauthAccount") or {}).get("emailAddress") or ""
    except Exception:
        return ""


def get_user_id():
    """Cross-platform user identity. Prefers the Claude account email
    (same person, tracked consistently across machines) over
    hostname + system username, which is machine-specific and fragments
    one person's usage across every device they run Claude Code on."""
    email = _claude_account_email()
    if email:
        return email
    try:
        host = platform.node() or socket.gethostname()
    except Exception:
        host = "unknown-host"
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown-user"
    return f"{host}-{user}"


def parse_frontmatter(filepath):
    """Parse the '--- ... ---' YAML-like block at the top of a markdown file.

    Deliberately a simple flat key: value parser (stdlib only, no pyyaml) -
    sufficient for the agent/skill frontmatter fields used in this project
    (name, version, description, tools, model). Nested/list YAML is not
    supported, except for the single-key '>'/'|' block scalar (folded/
    literal) needed for multi-line `description:` fields; values are
    otherwise returned as plain strings.
    """
    path = Path(filepath)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result = {}
    i = 1
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped or stripped.startswith("#") or ":" not in stripped or line[:1].isspace():
            i += 1
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value and value[0] in (">", "|") and value.strip("+-") in (">", "|"):
            folded = value[0] == ">"
            block_lines = []
            i += 1
            while i < len(lines) and (lines[i].strip() == "" or lines[i][:1].isspace()):
                block_lines.append(lines[i].strip())
                i += 1
            while block_lines and block_lines[-1] == "":
                block_lines.pop()
            result[key] = " ".join(block_lines) if folded else "\n".join(block_lines)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
        i += 1
    return result


def read_hook_input():
    """Read and parse the JSON payload Claude Code writes to the hook's stdin."""
    raw = sys.stdin.read()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        debug(f"could not decode stdin as JSON: {raw[:200]!r}")
        return {}
    debug(f"hook input: {json.dumps(payload)[:2000]}")
    return payload


def post_json(path, payload, user_id=None):
    """POST a JSON payload to the ingest API. Never raises: a tracking outage
    must not block or slow down the actual Claude Code session."""
    url = INGEST_API_URL.rstrip("/") + path
    data = json.dumps(payload, default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-User-Id": user_id or get_user_id(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            resp.read()
        return True
    except Exception as exc:
        print(f"[claude-tracking] POST {path} failed: {exc}", file=sys.stderr)
        return False


def _project_dir():
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()


def _tracking_dir():
    # Shared by Claude Code and Codex CLI hooks (both import this module
    # directly, no copy-paste of the state-file logic). Each product's
    # session_id comes from its own id space (Claude Code UUIDs, Codex's
    # own session ids), so collisions across products aren't a real risk.
    d = _project_dir() / ".state" / "tracking"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_state_file(session_id):
    safe = session_id.replace("/", "_") or "unknown-session"
    return _tracking_dir() / f"{safe}.json"


def _load_state(session_id):
    f = _session_state_file(session_id)
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"turn_id": 0, "sequence_id": 0, "tool_starts": {}}


def _save_state(session_id, state):
    f = _session_state_file(session_id)
    try:
        f.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        debug(f"could not persist state file: {exc}")


def next_ids(session_id, new_turn=False):
    """Return (turn_id, sequence_id) for the next event in this session.

    new_turn=True (UserPromptSubmit) bumps turn_id and resets sequence_id.
    Every other event just increments sequence_id within the current turn.
    Best-effort: hooks for one session normally run sequentially, one
    process per event, so a simple on-disk counter is sufficient here.
    """
    state = _load_state(session_id)
    if new_turn:
        state["turn_id"] = state.get("turn_id", 0) + 1
        state["sequence_id"] = 0
    state["sequence_id"] = state.get("sequence_id", 0) + 1
    _save_state(session_id, state)
    return state["turn_id"], state["sequence_id"]


def mark_tool_start(session_id, tool_key):
    """Record a tool call's start time, keyed by tool_use_id (or a fallback
    key), so PostToolUse/PostToolUseFailure can compute latency_ms."""
    state = _load_state(session_id)
    state.setdefault("tool_starts", {})[tool_key] = time.time()
    _save_state(session_id, state)


def pop_tool_latency_ms(session_id, tool_key):
    """Return elapsed ms since mark_tool_start(session_id, tool_key), or
    None if no matching start was recorded."""
    state = _load_state(session_id)
    starts = state.setdefault("tool_starts", {})
    started = starts.pop(tool_key, None)
    _save_state(session_id, state)
    if started is None:
        return None
    return int((time.time() - started) * 1000)


def mark_permission_request(session_id, tool_key):
    """Record when a PermissionRequest was shown to the user, keyed by
    tool_use_id (or a fallback key), so the eventual PreToolUse (approved)
    or PermissionDenied (denied) can compute how long the prompt sat
    waiting for a decision. Kept in a separate state bucket from
    tool_starts so this doesn't collide with the PreToolUse->PostToolUse
    execution-duration timer."""
    state = _load_state(session_id)
    state.setdefault("permission_starts", {})[tool_key] = time.time()
    _save_state(session_id, state)


def pop_permission_wait_ms(session_id, tool_key):
    """Return elapsed ms since mark_permission_request(session_id, tool_key),
    or None if no matching PermissionRequest was recorded (e.g. the tool
    was already allowed and never prompted)."""
    state = _load_state(session_id)
    starts = state.setdefault("permission_starts", {})
    started = starts.pop(tool_key, None)
    _save_state(session_id, state)
    if started is None:
        return None
    return int((time.time() - started) * 1000)


def get_state_value(session_id, key, default=None):
    return _load_state(session_id).get(key, default)


def set_state_value(session_id, key, value):
    state = _load_state(session_id)
    state[key] = value
    _save_state(session_id, state)


def resolve_agent_version(agent_name):
    """Best-effort local lookup of an agent's version from its frontmatter,
    avoiding a network round-trip to the registry on every hook call."""
    if not agent_name:
        return ""
    path = _project_dir() / ".claude" / "agents" / f"{agent_name}.md"
    return parse_frontmatter(path).get("version", "")


def resolve_skill_version(skill_name):
    if not skill_name:
        return ""
    path = _project_dir() / ".claude" / "skills" / skill_name / "SKILL.md"
    return parse_frontmatter(path).get("version", "")


def agents_dir():
    return _project_dir() / ".claude" / "agents"


def skills_dir():
    return _project_dir() / ".claude" / "skills"
