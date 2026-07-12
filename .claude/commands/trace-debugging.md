---
description: Stable alias for the trace-debugging skill (troubleshooting agent call chains via session_id/trace_id).
disable-model-invocation: true
---

This command exists only to give the `trace-debugging` skill a stable, version-independent slash command - the skill's own directory is renamed on every version bump (`trace-debugging_v<version>`) so the version is recoverable from the agent-tracking ClickHouse pipeline, which would otherwise break `/trace-debugging`.

Invoke the Skill tool with skill: "trace-debugging_v1.0.1" and args: "$ARGUMENTS".
