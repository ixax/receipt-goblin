---
name: stale-ref-sweep
description: >
  Mechanical sweep procedure and 3-bucket reporting format for a repo-wide stale-name check: pin down the entity, grep every file type, report fixed/historical/ambiguous.
  TRIGGER - read before running any stale-name sweep, whether following a rename/removal or confirming an entity name is still current before writing docs/comments.
  SKIP for the classification judgment itself - stale live reference vs. legitimate-historical vs. ambiguous stays in stale-ref-sweeper.md.
  v1.0.0
---

## 1. Pin down the entity

Minimum input: the old name, plus the new name if it's a rename.
Given a diff/migration/commit instead, read it first to extract the exact identifier.
Match whole identifiers, not substrings (`event_sources` -> `ingest_raw` must not hit `event_sources_backup`).
For the write-time check ("is X still current?"): treat X as the old name with no replacement, and confirm whether X still exists as named anywhere authoritative (schema, source, config) before reporting go/no-go.

## 2. Sweep the whole repo

Grep the old name across every file type - code, `.md`, YAML comments, docstrings, config, dashboards.
`Grep` for targeted searches; `Bash grep -rn` when the wrapper lacks what you need (`-w` whole-word, excluding `.git`/`node_modules` noise).
Never stop at the first hit or trust that the caller listed every location - un-swept lingering names are exactly why this sweep exists.

## Before editing prose

Before editing `.md` prose, read `Skill(md-format)`.
A one-line comment swap in code doesn't need it; a multi-sentence README/AGENTS.md paragraph edit does.

## Scope boundary

A rename that left a code bug (a live call site on the old name) is out of scope for an Edit fix - report the code issue (file, line, what's wrong) instead of patching logic.

## Reporting format

Three buckets: fixed (file/line, old -> new), historical (file/line, why), ambiguous (file/line, why, awaiting decision).
Quote only the specific reference, never large context blocks.
