# Dual-harness agent compiler: `.agents/agents/*.yaml` → Claude `.md` + Codex `.toml`

## Context

This repo runs two coding-CLI harnesses side by side (Claude Code and OpenAI's Codex CLI, see `.codex/config.toml`). Skills already work identically under both (`.agents/skills/` is the canonical, natively-read-by-both directory — no compilation needed). Subagents don't: today they exist only as `.claude/agents/*.md`, hand-written in Claude Code's frontmatter+Markdown-body format. Codex CLI has its own real custom-agent format (verified against official docs) — standalone `.toml` files under `.codex/agents/`, flat top-level keys (`name`, `description`, `developer_instructions`, `model`, `model_reasoning_effort`, `sandbox_mode`, `[mcp_servers.*]`), nothing like Claude's frontmatter shape.

Hand-maintaining both formats per agent would double every edit. The fix: one YAML file per agent under a new `.agents/agents/` directory becomes the single source of truth; a compiler script generates both `.claude/agents/<name>.md` and `.codex/agents/<name>.toml` from it. `.claude/agents/*.md` and `.codex/agents/*.toml` become generated-but-committed artifacts (same treatment as `agent_docs/harness-index.md` today) — never hand-edited directly.

This was co-designed with the user across the conversation; every point below reflects an explicit decision, not a default I'm picking unilaterally, except where noted as "assumption — flag for override."

## Design decisions (settled)

- **Source of truth**: `.agents/agents/<name>.yaml`, one file per agent, containing metadata + the full system-prompt body. Mirrors `.agents/skills/<name>/SKILL.md`'s one-file-per-entity pattern.
- **Outputs are committed**, not gitignored (matches `agent_docs/harness-index.md` precedent — a `--check` mode gates staleness, same as `sync_harness.py --check`).
- **`tools:`** is a multi-line YAML list in both the source and the compiled `.claude/agents/*.md` output (not today's single-line comma string). Confirmed safe: `scripts/sync_harness.py`'s frontmatter parser never reads `tools:` at all (only `name`/`description`/`model`), so this format change breaks nothing there.
- **Tools → Codex translation**: the YAML source lists Claude-style tool names only (`Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Skill`, `Agent`, `mcp__dev__query`, …) — the same vocabulary as today's `.claude/agents/*.md`. No per-agent Codex-specific config. The compiler holds one shared mapping table (Claude tool name → Codex contribution: `sandbox_mode` bits for Bash/Write/Edit, an `[mcp_servers.*]` block for `mcp__*` tools) and applies it centrally. This directly replaces the fictional per-agent `[tools]`/`allow_bash`-style TOML the user's draft script had — real Codex schema has no such table.
- **Model → per-CLI translation**: same pattern as tools, per the user's latest message. The YAML source's `model:` is an alias (e.g. `capable`, `cheap`), not a literal model name. One shared mapping (in the compiler) resolves each alias to the correct Claude model name for the `.md` output and the correct Codex/OpenAI model name for the `.toml` output — these are different model vendors, not just different tiers, so a literal Claude name would be meaningless in a Codex file. Agents with no `model:` (inherit default) stay that way in the source; the compiler omits `model` from both outputs, matching the existing "no frontmatter `model:` unless overriding" convention.
- **Version marker**: the YAML source gets a real `version: X.Y.Z` field (structured, easy to bump programmatically, clean diffs) — but the compiled outputs still need the marker inside `description:`'s last line, because only `name:`/`description:` reach the LiteLLM-logged messages via the "available agent types" listing (`services/_common/src/ingest_parsing.py`'s `_version_marker_for_name`; a real top-level `version:` key in the compiled `.md`/`.toml` would be invisible to that parser). The compiler reconciles both: it takes the source's `description:` (no version inside it) and `version:` field, and appends `vX.Y.Z` as the folded description's final token when rendering both outputs — the source stays clean, the compiled files keep the exact format the ingest parser expects.
- **Migration scope — assumption, flag for override**: migrate all 17 existing agents to `.agents/agents/*.yaml`, not a subset. Leaving some agents on old hand-written `.md` and others compiled would reproduce the exact asymmetry the user already rejected once (single- vs. dual-source design). If a subset was actually intended, say so and I'll narrow this.
- **Regeneration triggers** (three, per the user's message, reconciled against existing hook patterns):
  1. **Automatic, in-session**: extend the existing PostToolUse hook family (`hooks/harness_audit/sync_hook.py` already does exactly this for `harness-index.md` on `.claude/agents/*.md` edits) to also fire on any Edit/Write to `.agents/agents/*.yaml`, recompiling that agent's `.md`+`.toml` immediately. This needs no Bash access from the editing agent (harness-expert has none) — the hook runs independently of the tool call that triggered it, same as the existing precedent.
  2. **Manual**: new `make compile-agents` target, mirroring `make harness-index`.
  3. **`pre-commit` safety net**: `scripts/compile_agents.py --check` added to `.githooks/pre-commit` alongside the existing `check-lock.sh`/`check-uv.sh`/`make test` sequence. This isn't redundant with (1): PostToolUse hooks are a Claude-Code-only mechanism, so an edit made via Codex CLI (or a plain text editor) to `.agents/agents/*.yaml` would never trigger the in-session hook — `pre-commit` is the only trigger that's CLI-agnostic and guarantees the committed `.md`/`.toml` actually match their YAML source regardless of which tool made the edit.
- **Deleting an agent**: PostToolUse hooks fire on Edit/Write, never on a file deletion (which happens via Bash `rm`, a tool harness-expert doesn't have per its "No delete/Bash of your own" rule) — so removal can't be fully automatic in-session the way an edit is. Deletion is: delete `.agents/agents/<name>.yaml`, then run the compiler in full (non-`--check`) mode, which prunes any `.claude/agents/*.md`/`.codex/agents/*.toml` that no longer has a matching YAML source. `--check` mode also treats an orphaned compiled output (present, no matching source) as a staleness failure, so this is covered by the same `pre-commit` safety net as edits — a deletion that never got compiled still blocks the commit. `harness-expert` delegates both the `rm` and the compile run to `script-ops` in one call, the same delegation pattern it already uses for Bash-needing work (e.g. `audit.py`).
- **`scripts/sync_harness.py` needs no logic changes** — it still reads `.claude/agents/*.md` (now a generated file, but same shape) and only ever used `name`/`description`/`model`. Its docstring's claim "Codex has no Task-tool/subagent equivalent" is now false and gets corrected as a comment fix.

## Files to create

- **`.agents/agents/<name>.yaml`** × 17 — one per existing agent under `.claude/agents/`. Schema:
  ```yaml
  name: script-ops
  version: 1.7.2
  description: >
    Cheap-model executor for mechanical data/file work ...
    (no version token here - the compiler appends it)
  model: cheap                # alias — omit entirely if no override today
  tools:
    - Bash
    - Read
    - Write
    - Edit
    - Glob
    - Grep
    - Skill
  body: |
    You are ...
    (full current Markdown system-prompt body, verbatim)
  ```
  Content for each is a direct transcription of the corresponding current `.claude/agents/<name>.md` — this is a format migration, not a rewrite; no agent's behavior/wording changes as part of this task.

- **`scripts/compile_agents.py`** — the compiler, modeled directly on `scripts/sync_harness.py`'s structure (same `ROOT`/glob/`--check` pattern). Responsibilities:
  - Parse each `.agents/agents/*.yaml` (real YAML, not the minimal hand-rolled parser — `pyyaml` should already be a dependency given the repo's Python tooling; confirm via `get_project_dependencies` before adding it).
  - Hold the two shared mapping tables as module-level constants: `TOOL_MAPPING` (Claude tool name → Codex `sandbox_mode`/`mcp_servers` contribution) and `MODEL_MAPPING` (alias → `{claude: ..., codex: ...}`).
  - Render `.claude/agents/<name>.md`: frontmatter fences, `name`, `description` (`>`-folded, with `version:`'s `vX.Y.Z` appended as the final token — the source's `description:` itself carries no version), `tools:` as a multi-line list, `model:` (resolved Claude name, omitted if absent), then the `body` as the Markdown content.
  - Render `.codex/agents/<name>.toml`: flat top-level keys per the real schema (`name`, `description` with the same appended `vX.Y.Z`, `developer_instructions` = `body`, `model` resolved via `MODEL_MAPPING`, `sandbox_mode` derived via `TOOL_MAPPING`, `[mcp_servers.*]` blocks for any `mcp__*` tool present).
  - **Prune orphaned outputs**: on a full (non-`--check`) run, glob existing `.claude/agents/*.md` and `.codex/agents/*.toml`; any whose stem has no matching `.agents/agents/<stem>.yaml` gets deleted (with a printed message) — this is what makes deleting an agent's YAML source actually remove its compiled files instead of leaving stale copies behind.
  - `--check` mode: exit 1 if any generated file doesn't match its would-be output, *or* if any compiled output is orphaned (no matching source) — same contract as `sync_harness.py --check`, extended to cover deletions.
  - **Verify before finalizing the Codex output shape**: whether a per-agent `[mcp_servers.*]` block in an agent's own TOML is required to grant that MCP server, or whether agents inherit everything already configured in `.codex/config.toml` by default and a per-agent block only restricts/overrides. My source research didn't resolve this cleanly — check empirically against one real compiled agent (e.g. `sql-expert`, which uses `mcp__dev__query`/`mcp__dev__profile_query`) before locking the compiler's MCP-block logic.

## Files to modify

- **`.claude/agents/*.md`** × 17 and **`.codex/agents/*.toml`** × 17 (new) — become compiler output; committed. First-run output for the `.md` files should be byte-for-byte equivalent to today's content except for the `tools:` list-vs-string formatting.
- **`.claude/agents/harness-expert.md`** (via its own source YAML, since it's included in the migration) — concrete text changes:
  - **Scope bullet 1** (`Write`/`Edit` allow-list): replace `.claude/` with `.agents/agents/*.yaml` in the editable list — becomes `.agents/agents/*.yaml`, `.agents/skills/`, `AGENTS.md`, `agent_docs/*.md`.
  - **New Scope bullet**, next to the existing `agent_docs/harness-index.md` never-hand-edit line: `.claude/agents/*.md` / `.codex/agents/*.toml`: generated by `scripts/compile_agents.py` from `.agents/agents/*.yaml` — never hand-edit. Fix the YAML source; the PostToolUse hook regenerates automatically, or run `make compile-agents`.
  - **Bash-delegation paragraph** (currently only mentions `audit.py`): extend to cover deletion — removing an agent means delegating both `rm .agents/agents/<name>.yaml` and a `compile_agents.py` run (to prune the now-orphaned outputs) to `script-ops` in one call, same pattern as the existing `audit.py` delegation.
  - **Entity shapes section**: replace the current line (`Subagent (.claude/agents/*.md): name (bare, permanent), description (>-folded), tools (comma-separated string), model`) with the YAML source shape: `name` (bare, permanent), `version` (bare `X.Y.Z`, own field), `description` (`>`-folded, no version token — the compiler appends it), `tools` (multi-line list), `model` (alias, optional), `body` (block scalar, full system-prompt Markdown).
  - **Version marker section**: rewrite the mechanics — bump target is now the `version:` field directly (increment there, not inside `description:` text); same bump rules unchanged (patch/minor/major, new entity → `1.0.0`, ad-hoc agents get no marker). Add one line noting the compiler appends `vX.Y.Z` from `version:` onto the compiled `description:`'s last line in both outputs — this is why `version:` and `description:` stay separate fields in the source but merge in the compiled files.
- **`.githooks/pre-commit`**: add `uv run python3 scripts/compile_agents.py --check || exit 1` alongside the existing `check-lock.sh`/`check-uv.sh` lines, before `make test` (same "fail fast on the cheap check first" ordering already used there).
- **`Makefile`**: new `compile-agents` target; wire it as a prerequisite of (or paired with) the existing `harness-index` target so `.claude/agents/*.md` is fresh before `harness-index` reads it.
- **`hooks/harness_audit/sync_hook.py`** (or a new sibling hook, whichever keeps the file cohesive): extend the watched-path matching to also fire `compile_agents.py` for the touched agent(s) on Edit/Write to `.agents/agents/*.yaml`.
- **`hooks/harness_audit/budgets.py`**: add a budget-class entry for `.agents/agents/*.yaml`, mirroring the existing entry for `.claude/agents/*.md`.
- **`AGENTS.md`**: update the "Agent & skill routing" section's line describing how Codex discovers agents — it currently reflects the old harness-index-only, no-native-Codex-agent-support state.
- **`agent_docs/architecture.md`**: correct the "Codex CLI adapter notes" section (currently claims Codex has no Task-tool/subagent equivalent).
- **`.agents/skills/harness-guardian/SKILL.md`**: extend the "Dual-harness layout" section to document the new `.agents/agents/` → compiled-outputs mechanism, alongside the existing skills-symlink description.
- **`scripts/sync_harness.py`**: docstring-only fix (lines 9-10's "Codex has no Task-tool/subagent equivalent" claim); no logic change.

## Verification

1. `uv run python3 scripts/compile_agents.py` — regenerate all 17 agents; diff the resulting `.claude/agents/*.md` against the pre-migration versions (should differ only in `tools:` formatting).
2. `uv run python3 scripts/compile_agents.py --check` — confirm clean exit 0 immediately after a fresh compile.
3. `uv run python3 scripts/sync_harness.py --check` — confirm `agent_docs/harness-index.md` still matches (should be unaffected, since it only reads `name`/`description`/`model`).
4. `uv run python3 hooks/harness_audit/audit.py .` — confirm no new token-budget violations from the added `.agents/agents/*.yaml` files.
5. In Claude Code: edit one agent's `.agents/agents/<name>.yaml` (trivial description tweak), confirm the PostToolUse hook regenerates both outputs in-session without a manual compile step.
6. Manually inspect one compiled `.codex/agents/*.toml` (e.g. `sql-expert.toml`, given its MCP tool usage) against Codex CLI's real schema expectations; if possible, actually load it in Codex CLI and confirm it's accepted and the MCP tools are reachable — this is the check that resolves the open "does an agent need its own `[mcp_servers.*]` block" question above.
7. `make test` and a plain `git commit` (with `.agents/agents/*.yaml` intentionally left uncompiled) to confirm the `pre-commit` `--check` gate actually blocks the commit, then compile and confirm it passes.
