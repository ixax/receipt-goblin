---
name: test-linter
description: >
  Minimal test skill that checks a file for obvious style issues.
  Use to verify the tracking stack end to end.
version: 2.0.0
---

# test-linter

Given a file path, read it and list any obvious style issues you notice
(inconsistent indentation, overly long lines, missing final newline,
trailing whitespace). Report findings as a short bullet list; this skill
exists to exercise the tracking hooks, not to replace a real linter.
