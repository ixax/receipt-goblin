---
description: Stable alias for the md-format skill (formatting conventions for markdown prose/tables).
disable-model-invocation: true
---

This command exists only to give the `md-format` skill a stable, version-independent slash command - the skill's own directory is renamed on every version bump (`md-format_v<version>`) so the version is recoverable from the agent-tracking ClickHouse pipeline, which would otherwise break `/md-format`.

Invoke the Skill tool with skill: "md-format_v1.0.0" and args: "$ARGUMENTS".
