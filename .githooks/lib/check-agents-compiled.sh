#!/bin/sh
# Exits non-zero if any .claude/agents/*.md or .codex/agents/*.toml is stale
# relative to its .agents/agents/*.yaml source, or orphaned (compiled output
# with no matching source).
# compile_agents.py --check does the actual comparison - this just runs it
# and surfaces the fix.
# Needs uv/python3, unlike check-lock.sh/check-versions.sh - callers must
# run check-uv.sh first.

if uv run python3 scripts/compile_agents.py --check; then
    exit 0
fi

echo "error: .claude/agents/*.md / .codex/agents/*.toml out of sync with .agents/agents/*.yaml"
echo "regenerate and stage the outputs:"
echo "  make compile-agents && git add .claude/agents .codex/agents"
exit 1
