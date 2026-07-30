#!/usr/bin/env python3
"""SessionStart hook.
Injects the Opus/Fable cascading-delegation workflow into context, but only
when the session's model is Opus or Fable.
Sonnet/Haiku sessions get no additionalContext, so AGENTS.md stays lean for
the common case.
payload["model"] is not documented as guaranteed by Claude Code.
Absence is treated as "not expensive", i.e. the rule is skipped rather than
applied by default.
"""
import json
import sys

EXPENSIVE_MODEL_MARKERS = ("opus", "fable")

CASCADE_WORKFLOW = """\
## Expensive-model delegation policy (Opus/Fable only)

You are running on a high-cost model (Opus/Fable) this session.
Follow a cascading handoff to conserve budget instead of doing everything \
yourself.

- Plan only: produce a concrete implementation plan via the Plan tool or a \
`todo/*.md` file.
- Do not write or edit implementation code yourself.
- Hand off to Sonnet by spawning a `claude` (Sonnet) subagent via the Agent \
tool.
- Pass Sonnet only the final plan, not your intermediate reasoning.
- Sonnet implements, debugs, and resolves failing tests on its own.
- Sonnet may spawn Haiku subagents for isolated low-effort utility work \
(formatting, boilerplate, simple tests).
- Do not resume control for routine errors or failing tests.
- Escalate back only if the plan turns out logically impossible to \
implement as specified.
- Escalate back if an unexpected blocker forces a major architectural or \
schema redesign, such as ClickHouse, Redis, or LiteLLM structures.
- Routine bugs are never a reason to escalate.
"""


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    model = (payload.get("model") or "").lower()
    if not any(marker in model for marker in EXPENSIVE_MODEL_MARKERS):
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": CASCADE_WORKFLOW,
        }
    }))


if __name__ == "__main__":
    main()
    sys.exit(0)
