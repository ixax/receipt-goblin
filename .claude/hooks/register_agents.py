#!/usr/bin/env python3
"""Scan .claude/agents/*.md and .claude/skills/*/SKILL.md and upsert their
frontmatter (name, version, description) into the ClickHouse registries.

Runs automatically on SessionStart (via log_session.py) and can also be run
standalone, e.g. after editing an agent/skill without starting a new session:

    python3 .claude/hooks/register_agents.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import agents_dir, debug, get_user_id, parse_frontmatter, post_json, skills_dir  # noqa: E402


def scan_and_register(user_id=None):
    user_id = user_id or get_user_id()
    registered = {"agents": 0, "skills": 0}

    for md_file in sorted(agents_dir().glob("*.md")):
        fm = parse_frontmatter(md_file)
        name = fm.get("name")
        if not name:
            debug(f"skipping agent file without name: {md_file}")
            continue
        ok = post_json(
            "/registry/agent",
            {
                "agent_name": name,
                "version": fm.get("version", "0.0.0"),
                "description": fm.get("description", ""),
                "source_file": str(md_file),
            },
            user_id=user_id,
        )
        if ok:
            registered["agents"] += 1

    for skill_md in sorted(skills_dir().glob("*/SKILL.md")):
        fm = parse_frontmatter(skill_md)
        name = fm.get("name")
        if not name:
            debug(f"skipping skill file without name: {skill_md}")
            continue
        ok = post_json(
            "/registry/skill",
            {
                "skill_name": name,
                "version": fm.get("version", "0.0.0"),
                "description": fm.get("description", ""),
                "source_file": str(skill_md),
            },
            user_id=user_id,
        )
        if ok:
            registered["skills"] += 1

    return registered


if __name__ == "__main__":
    result = scan_and_register()
    print(f"registered {result['agents']} agent(s), {result['skills']} skill(s)")
