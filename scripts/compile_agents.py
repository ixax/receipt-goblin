#!/usr/bin/env python3
"""Compile .agents/agents/*.yaml into .claude/agents/*.md and .codex/agents/*.toml.

Single source of truth per agent lives in .agents/agents/<name>.yaml.
Never hand-edit the compiled .claude/agents/*.md or .codex/agents/*.toml files.
Fix the YAML source, then re-run this script (or let the PostToolUse hook do it
for a .agents/agents/*.yaml edit made in Claude Code).

Usage:
  uv run python3 scripts/compile_agents.py           # (re)write every compiled file
  uv run python3 scripts/compile_agents.py --check   # exit 1 if any output is stale/orphaned
"""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / ".agents" / "agents"
CLAUDE_DIR = ROOT / ".claude" / "agents"
CODEX_DIR = ROOT / ".codex" / "agents"

# Claude tool name -> Codex contribution.
# Bash/Write/Edit widen sandbox_mode.
# Each mcp__* tool contributes one [mcp_servers.*] block, keyed by server name.
# Server name/URL mirrors .codex/config.toml (Codex) and .mcp.json (Claude).
# Both point at the same MCP processes, just named differently per harness's own config today.
TOOL_MAPPING = {
    "Bash": {"sandbox": "workspace-write"},
    "Write": {"sandbox": "workspace-write"},
    "Edit": {"sandbox": "workspace-write"},
    "Read": {},
    "Grep": {},
    "Glob": {},
    "Skill": {},
    "Agent": {},
    "Monitor": {},
    "SendMessage": {},
    "mcp__dev__query": {"mcp_server": "clickhouse", "url": "http://localhost:8001/mcp"},
    "mcp__dev__profile_query": {"mcp_server": "clickhouse", "url": "http://localhost:8001/mcp"},
    "mcp__stats__me": {"mcp_server": "stats", "url": "http://localhost:8002/mcp"},
    # litellm-test-alerting.md names this tool mcp__clickhouse__query, not mcp__dev__query.
    # This is a pre-existing naming inconsistency: .mcp.json only defines "dev"/"stats", never "clickhouse".
    # Carried over verbatim by this migration.
    # Same backend/URL as mcp__dev__query.
    "mcp__clickhouse__query": {"mcp_server": "clickhouse", "url": "http://localhost:8001/mcp"},
}

# model: alias -> per-CLI literal model name.
# Codex identifiers are a best-effort mapping, not empirically verified against a real Codex CLI install (see plan verification step 5).
# "inherit" (and an absent model: field) resolve to None on both CLIs -
# the compiler omits the model line entirely, so each harness falls back to its own default.
MODEL_MAPPING = {
    "cheap": {"claude": "claude-haiku-4-5", "codex": "gpt-5.1-codex-mini"},
    "capable": {"claude": "claude-sonnet-5", "codex": "gpt-5.1-codex"},
    "inherit": {"claude": None, "codex": None},
}


def resolved_model(agent: dict, cli: str) -> str | None:
    alias = agent.get("model") or "inherit"
    return MODEL_MAPPING[alias][cli]


def load_agents() -> list[dict]:
    agents = []
    for path in sorted(SRC_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["_stem"] = path.stem
        agents.append(data)
    return agents


def desc_lines_with_version(agent: dict) -> list[str]:
    lines = agent["description"].rstrip("\n").split("\n")
    lines.append(f"v{agent['version']}")
    return lines


def render_claude_md(agent: dict) -> str:
    lines = ["---", f"name: {agent['name']}", "description: >"]
    lines += [f"  {line}" for line in desc_lines_with_version(agent)]
    if agent.get("tools"):
        lines.append("tools:")
        lines += [f"  - {t}" for t in agent["tools"]]
    model = resolved_model(agent, "claude")
    if model:
        lines.append(f"model: {model}")
    lines.append("---")
    lines.append("")
    body = agent["body"].rstrip("\n")
    return "\n".join(lines) + "\n" + body + "\n"


def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_multiline_str(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return '"""\n' + escaped.rstrip("\n") + '\n"""'


def render_codex_toml(agent: dict) -> str:
    desc = " ".join(desc_lines_with_version(agent))
    parts = [
        f"name = {toml_str(agent['name'])}",
        f"description = {toml_str(desc)}",
        f"developer_instructions = {toml_multiline_str(agent['body'].rstrip(chr(10)))}",
    ]
    model = resolved_model(agent, "codex")
    if model:
        parts.append(f"model = {toml_str(model)}")

    tools = agent.get("tools", [])
    sandbox = "workspace-write" if any(TOOL_MAPPING.get(t, {}).get("sandbox") for t in tools) else "read-only"
    parts.append(f"sandbox_mode = {toml_str(sandbox)}")

    mcp_servers = {}
    for t in tools:
        contrib = TOOL_MAPPING.get(t, {})
        if "mcp_server" in contrib:
            mcp_servers[contrib["mcp_server"]] = contrib["url"]

    out = "\n".join(parts) + "\n"
    for server, url in sorted(mcp_servers.items()):
        out += f"\n[mcp_servers.{server}]\nurl = {toml_str(url)}\n"
    return out


def main() -> int:
    check = "--check" in sys.argv
    agents = load_agents()
    stale = []

    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    CODEX_DIR.mkdir(parents=True, exist_ok=True)

    wanted_md = set()
    wanted_toml = set()

    for agent in agents:
        stem = agent["_stem"]
        md_path = CLAUDE_DIR / f"{stem}.md"
        toml_path = CODEX_DIR / f"{stem}.toml"
        wanted_md.add(md_path)
        wanted_toml.add(toml_path)

        for path, content in (
            (md_path, render_claude_md(agent)),
            (toml_path, render_codex_toml(agent)),
        ):
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current == content:
                continue
            if check:
                stale.append(path)
            else:
                path.write_text(content, encoding="utf-8", newline="\n")

    for existing, wanted in (
        (CLAUDE_DIR.glob("*.md"), wanted_md),
        (CODEX_DIR.glob("*.toml"), wanted_toml),
    ):
        for path in existing:
            if path in wanted:
                continue
            if check:
                stale.append(path)
            else:
                path.unlink()
                print(f"compile-agents: pruned orphaned {path.relative_to(ROOT)}")

    if check:
        if stale:
            print(
                "STALE: compiled files don't match their .agents/agents/*.yaml source (or are orphaned):",
                file=sys.stderr,
            )
            for path in stale:
                print(f"  {path.relative_to(ROOT)}", file=sys.stderr)
            print("Run: make compile-agents", file=sys.stderr)
            return 1
        print("compile-agents: up to date")
        return 0

    print(
        f"compile-agents: wrote {len(agents)} agents to "
        f"{CLAUDE_DIR.relative_to(ROOT)} and {CODEX_DIR.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
