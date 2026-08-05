---
name: harness-guardian
description: >
  Token-economy audit workflow for the agent harness: file budgets,
  rule classification and relocation, cache hygiene, dual-harness
  (Claude Code + Codex) layout checks.
  Never triggers proactively - invoked explicitly by name, or read by
  the harness-expert subagent before a structural audit or relocation
  pass.
  SKIP for routine per-edit budget checks - the PostToolUse hook runs
  those automatically.
  v1.5.1
---

Keep the always-loaded context layer minimal.
Every rule lives at the cheapest layer that still guarantees the behaviour.
Per-edit standing rules (classification default, one-rule-one-file, volatility ban, budget-reject) are owned by `.claude/agents/harness-expert.md` - this skill is the workflow for a full structural audit.

## 1. Measure

Budgets (token estimate: `bytes / 4`) live in `hooks/harness_audit/budgets.py` - never restate numbers.
The PostToolUse hook (`hooks/harness_audit/audit_hook.py`, wired in `.claude/settings.json`) runs `hooks/harness_audit/audit.py` after any Edit/Write touching the harness and feeds violations back to the editing agent (exit 2).
Full standalone audit: `uv run python3 hooks/harness_audit/audit.py .` from the repo root - any Bash-capable agent; harness-expert spawns `script-ops` for it.
Exit code 1 means violations found.

## 2. Classify

Assign every line in an over-budget file exactly one tag:

- UNIVERSAL - needed in every session -> stays in AGENTS.md.
- SCOPED - path subset -> `.claude/rules/<topic>.md` with `globs:` frontmatter, or a nested AGENTS.md.
- TASK - one workflow only -> the matching SKILL.md body (create the skill if missing).
- DEEP - reference material within a workflow -> that skill's `references/`, one-line pointer left in SKILL.md.
- ENFORCEABLE - machine-checkable (formatting, naming, file endings, forbidden paths) -> hook or linter rule, then delete the prose.
- OBSOLETE - fixed bugs, dead workarounds, stale versions -> delete.

Unsure between UNIVERSAL and TASK -> TASK.
An under-triggered skill costs one missed load; a bloated AGENTS.md costs every session.

## 3. Relocate

- A rule exists in exactly one file - delete the source line when moving.
- Replace inlined code snippets with `path:line` references.
- Convert prose to imperative bullets; collapse near-duplicates to one canonical example.
- Notes meant for humans go into `<!-- block comments -->` (stripped before context).
- New skill: write the description first - what it does plus the concrete trigger phrases and file paths that activate it.
  All "when to use" wording goes in the description, none in the body.

## 4. Verify

Re-run `hooks/harness_audit/audit.py` - must exit 0.
Run `uv run python3 scripts/sync_harness.py --check`; nonzero means `agent_docs/harness-index.md` is stale - regenerate via `make harness-index`.
For each ENFORCEABLE rule converted to a hook: trigger it once, confirm the hook blocks/fixes as the prose used to instruct.

## 5. Report

Output a table: file, tokens before, tokens after, lines moved (with destination), lines deleted.
Append one line to `thoughts/harness-audit-log.md` (date, total always-loaded tokens) so growth is visible as a trend.

## Cache hygiene (always-loaded files are the cached prefix)

- No volatile content in AGENTS.md, nested AGENTS.md, or rules: dates, timestamps, sprint numbers, branch names, counters, "current" state.
  Inject volatile context via a SessionStart hook message instead.
- Edit harness files between sessions, never mid-session - each edit rebuilds the conversation cache at full price.
- Keep the tool/MCP set stable per session: configure per project before start; model states as tools, don't toggle the tool set.
- One model family per session; switch models only via a subagent with a hand-off message.
- Cache hit rate is an SLO: sustained < 60% on a stable workload is a design smell.
  Audit for prefix instability - reordered sections, changed tool definitions, injected dynamics; the audit script flags volatile-looking lines.

## Dual-harness layout (Claude Code + Codex CLI)

The harness must behave identically under both tools:

- `AGENTS.md` is the sole doc file, root and nested - this repo deliberately maintains no `CLAUDE.md` anywhere, symlink or real file.
  Both CLIs read `AGENTS.md` directly.
  Never recreate `CLAUDE.md` on noticing it "missing" - that absence is the convention, not a gap to fix.
- Codex concatenates the AGENTS.md chain root-down with a 32 KiB default cap (`project_doc_max_bytes`) and silently truncates beyond it - the audit script checks the combined chain size.
- `.agents/skills/` is the canonical skills directory (Agent Skills open standard, read natively by Codex CLI); `.claude/skills` is a single directory-level symlink to it.
  A new skill goes directly under `.agents/skills/<name>/`; nothing extra on the `.claude/` side.
  Descriptions must work for both trigger mechanics (Claude implicit + Codex implicit/`$skill`).
- `.claude/rules/` with globs is a Claude-only optimisation.
  Anything that must hold in both harnesses goes into a nested AGENTS.md; glob rules may only duplicate-as-scoping, never carry unique rules.
- Enforcement that must hold everywhere lives in pre-commit/CI and linters; `.claude/hooks` and Codex config are thin per-tool adapters of the same checks, not the source of truth.
