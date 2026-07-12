---
description: Stable alias for the test-summarizer skill (minimal test skill that summarizes given text).
disable-model-invocation: true
---

This command exists only to give the `test-summarizer` skill a stable, version-independent slash command - the skill's own directory is renamed on every version bump (`test-summarizer_v<version>`) so the version is recoverable from the agent-tracking ClickHouse pipeline, which would otherwise break `/test-summarizer`.

Invoke the Skill tool with skill: "test-summarizer_v1.0.0" and args: "$ARGUMENTS".
