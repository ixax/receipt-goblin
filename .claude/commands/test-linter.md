---
description: Stable alias for the test-linter skill (minimal test skill that checks a file for style issues).
disable-model-invocation: true
---

This command exists only to give the `test-linter` skill a stable, version-independent slash command - the skill's own directory is renamed on every version bump (`test-linter_v<version>`) so the version is recoverable from the agent-tracking ClickHouse pipeline, which would otherwise break `/test-linter`.

Invoke the Skill tool with skill: "test-linter_v2.0.0" and args: "$ARGUMENTS".
