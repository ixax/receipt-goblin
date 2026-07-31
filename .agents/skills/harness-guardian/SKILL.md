---
name: harness-guardian
description: >
  Token-economy audit workflow for the agent harness: file budgets,
  rule classification and relocation, cache hygiene, dual-harness
  (Claude Code + Codex) checks. Never triggers proactively - invoked
  explicitly by name, or read by the harness-expert subagent when it
  performs a structural audit. Deterministic checks run via the
  PostToolUse hook, not from this skill.
  <version>1.2.1</version>
---

# Harness Guardian

Keep the always-loaded context layer minimal.
Every rule lives at the cheapest layer that still guarantees the behaviour.

## Budgets (hard limits)

Current thresholds (token estimate: `bytes / 4`) live in
`hooks/harness_audit/budgets.py` — don't restate numbers here, they drift
out of sync.

## Workflow

### 1. Measure
Budget checks are deterministic and run automatically: the PostToolUse hook (`hooks/harness_audit/audit_hook.py`, wired in `.claude/settings.json`) calls `hooks/harness_audit/audit.py` after any Edit/Write touching the harness and feeds violations back to the editing agent (exit 2).
For a full standalone audit, run the script from the repo root (any agent with Bash, or the orchestrator).
harness-expert has no Bash and consumes hook feedback instead of running the script itself.
Exit code 1 means violations found.

### 2. Classify
For every line in over-budget files assign exactly one tag:

- **UNIVERSAL** — needed in every session → stays in CLAUDE.md.
- **SCOPED** — applies to a path subset → move to `.claude/rules/<topic>.md`
  with `globs:` frontmatter, or a nested CLAUDE.md.
- **TASK** — needed only inside one workflow → move to the matching
  SKILL.md body (create the skill if missing).
- **DEEP** — reference material within a workflow → move to that skill's
  `references/`, leave a one-line pointer in SKILL.md.
- **ENFORCEABLE** — a machine can check it (formatting, naming, file
  endings, forbidden paths) → implement as a hook or linter rule, then
  delete the prose line entirely.
- **OBSOLETE** — fixed bugs, dead workarounds, stale versions → delete.

When unsure between UNIVERSAL and TASK, choose TASK.
An under-triggered skill costs one missed load.
A bloated CLAUDE.md costs every session.

### 3. Relocate
Apply moves.
Rules:
- A rule exists in exactly one file. Delete the source line when moving.
- Replace any inlined code snippet with a `path:line` reference.
- Convert prose to imperative bullets; collapse near-duplicate examples
  to one canonical example.
- Notes meant for humans go into `<!-- block comments -->` (stripped
  before context).
- When creating a skill, write the description first: what it does plus
  the concrete trigger phrases and file paths that should activate it.
  All "when to use" wording goes in the description, none in the body.

### 4. Verify
Re-run `hooks/harness_audit/audit.py` — must exit 0.
Also run `python3 scripts/sync_harness.py --check`.
Nonzero exit means `agent_docs/harness-index.md` is stale — regenerate via `make harness-index` or `python3 scripts/sync_harness.py`.
Then sanity-check behaviour is preserved: for each ENFORCEABLE rule converted to a hook, trigger it once and confirm the hook blocks/fixes as the prose used to instruct.

### 5. Report
Output a short table: file, tokens before, tokens after, lines moved (with destination), lines deleted.
Append one line per audit to `thoughts/harness-audit-log.md` (date, total always-loaded tokens) so growth is visible as a trend.

## Cache hygiene (always-loaded files are the cached prefix)

- No volatile content in CLAUDE.md, AGENTS.md, or rules: no dates,
  timestamps, sprint numbers, branch names, counters, "current" state.
  Inject volatile context via a SessionStart hook message instead.
- Edit harness files between sessions, never mid-session — each edit
  rebuilds the conversation cache at full price.
- Keep the tool/MCP set stable per session: configure per project
  before start; model states as tools, do not toggle the tool set.
- One model family per session; switch models only via a subagent
  with a hand-off message.
- Watch cache hit rate as an SLO: sustained < 60% on a stable
  workload is a design smell — audit for prefix instability
  (reordered sections, changed tool definitions, injected dynamics).
  The audit script flags volatile-looking lines in always-loaded files.

## Dual-harness layout (Claude Code + Codex CLI)

The harness must behave identically under both tools.
Enforce this layout:

- `AGENTS.md` is the canonical root file; `CLAUDE.md` is a symlink to it.
  Same for nested directories.
  Never let the two diverge as real files.
- Codex concatenates the AGENTS.md chain root-down with a 32 KiB default
  cap (`project_doc_max_bytes`) and silently truncates beyond it — the
  audit script checks the combined chain size.
- Skills follow the shared Agent Skills standard: one `skills/` directory
  in the repo, symlinked into `.claude/skills/` and `.codex/skills/`.
  Descriptions must work for both trigger mechanics (Claude implicit +
  Codex implicit/`$skill`).
- `.claude/rules/` with globs is a Claude-only optimisation. Anything
  that must hold in both harnesses goes into a nested AGENTS.md instead;
  glob rules may only duplicate-as-scoping, never carry unique rules.
- Enforcement that must hold everywhere lives in pre-commit/CI and
  linters; `.claude/hooks` and Codex config are thin per-tool adapters
  of the same checks, not the source of truth.

## Standing rules for any harness edit

- New rule proposed → run steps 2–3 on that rule before adding it.
  Default destination is NOT CLAUDE.md.
- Never add a rule a formatter or linter already enforces.
- Never duplicate content between CLAUDE.md, rules, and skills.
- Reject additions that push a file past its budget; free the space
  first by classifying and relocating existing lines.
