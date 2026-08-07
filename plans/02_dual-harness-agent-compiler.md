---
date: 2026-08-07
context: >
  User asked for an agent structure usable in both Codex CLI and Claude Code
  with minimal copy-paste duplication.
---

# Dual-harness agent compiler: `.agents/agents/*.yaml` → Claude `.md` + Codex `.toml`

## Context

This repo runs two coding-CLI harnesses side by side: Claude Code and OpenAI's Codex CLI (see `.codex/config.toml`).
Skills already work identically under both — `.agents/skills/` is the canonical, natively-read-by-both directory, no compilation needed.
Subagents don't: today they exist only as `.claude/agents/*.md`, hand-written in Claude Code's frontmatter+Markdown-body format.
Codex CLI has its own real custom-agent format, verified against official docs — standalone `.toml` files under `.codex/agents/`, flat top-level keys (`name`, `description`, `developer_instructions`, `model`, `model_reasoning_effort`, `sandbox_mode`, `[mcp_servers.*]`), nothing like Claude's frontmatter shape.

Hand-maintaining both formats per agent would double every edit.
The fix: one YAML file per agent under a new `.agents/agents/` directory becomes the single source of truth.
A compiler script (`compile_agents.py`, lives under `scripts/`) generates both `.claude/agents/<name>.md` and `.codex/agents/<name>.toml` from it.
`.claude/agents/*.md` and `.codex/agents/*.toml` become generated-but-committed artifacts, never hand-edited directly.

As part of this same change, `agent_docs/harness-index.md` and `scripts/sync_harness.py` are removed outright rather than kept or folded into the compiler — see the removal bullet below for why.

This was co-designed with the user across the conversation; every point below reflects an explicit decision, not a default I'm picking unilaterally, except where noted as "assumption — flag for override."

## Design decisions (settled)

- **Source of truth**: `.agents/agents/<name>.yaml`, one file per agent, containing metadata + the full system-prompt body. Mirrors `.agents/skills/<name>/SKILL.md`'s one-file-per-entity pattern.
- **Outputs are committed**, not gitignored — a `--check` mode gates staleness in `pre-commit`.
- **`tools:`** is a multi-line YAML list in both the source and the compiled `.claude/agents/*.md` output (not today's single-line comma string). This is safe because nothing downstream ever parsed `tools:` structurally: the only prior consumer of `.claude/agents/*.md` frontmatter, `sync_harness.py`, only ever read `name`/`description`/`model` — and that script is being removed anyway (see below).
- **Tools → Codex translation**: the YAML source lists Claude-style tool names only (`Bash`, `Read`, `Write`, `Edit`, `Grep`, `Glob`, `Skill`, `Agent`, `mcp__dev__query`, …) — the same vocabulary as today's `.claude/agents/*.md`. No per-agent Codex-specific config. The compiler holds one shared mapping table (Claude tool name → Codex contribution: `sandbox_mode` bits for Bash/Write/Edit, an `[mcp_servers.*]` block for `mcp__*` tools) and applies it centrally. This directly replaces the fictional per-agent `[tools]`/`allow_bash`-style TOML the user's draft script had — real Codex schema has no such table.
- **Model → per-CLI translation**: same pattern as tools, per the user's latest message. The YAML source's `model:` is an alias (e.g. `capable`, `cheap`), not a literal model name. One shared mapping (in the compiler) resolves each alias to the correct Claude model name for the `.md` output and the correct Codex/OpenAI model name for the `.toml` output — these are different model vendors, not just different tiers, so a literal Claude name would be meaningless in a Codex file. Agents with no `model:` (inherit default) stay that way in the source; the compiler omits `model` from both outputs, matching the existing "no frontmatter `model:` unless overriding" convention.
- **Version marker**: the YAML source gets a real `version: X.Y.Z` field (structured, easy to bump programmatically, clean diffs) — but the compiled outputs still need the marker inside `description:`'s last line, because only `name:`/`description:` reach the LiteLLM-logged messages via the "available agent types" listing (`services/_common/src/ingest_parsing.py`'s `_version_marker_for_name`; a real top-level `version:` key in the compiled `.md`/`.toml` would be invisible to that parser). The compiler reconciles both: it takes the source's `description:` (no version inside it) and `version:` field, and appends `vX.Y.Z` as the folded description's final token when rendering both outputs — the source stays clean, the compiled files keep the exact format the ingest parser expects.
- **Migration scope — assumption, flag for override**: migrate all 17 existing agents to `.agents/agents/*.yaml`, not a subset. Leaving some agents on old hand-written `.md` and others compiled would reproduce the exact asymmetry the user already rejected once (single- vs. dual-source design). If a subset was actually intended, say so and I'll narrow this.
- **Regeneration triggers** (three, reconciled against existing hook patterns):
  1. **Automatic, in-session**: the existing PostToolUse hook family (`hooks/harness_audit/sync_hook.py`, which today regenerates `harness-index.md` on `.claude/agents/*.md` edits) is repurposed to fire on any Edit/Write to `.agents/agents/*.yaml`, recompiling that agent's `.md`+`.toml` immediately. This needs no Bash access from the editing agent (harness-expert has none) — the hook runs independently of the tool call that triggered it.
  2. **Manual**: new `make compile-agents` target, replacing the deleted `harness-index` target (see removal bullet below).
  3. **`pre-commit` safety net**: `compile_agents.py --check` added to `.githooks/pre-commit` alongside the existing `check-lock.sh`/`check-uv.sh`/`make test` sequence. This isn't redundant with (1): PostToolUse hooks are a Claude-Code-only mechanism, so an edit made via Codex CLI (or a plain text editor) to `.agents/agents/*.yaml` would never trigger the in-session hook — `pre-commit` is the only trigger that's CLI-agnostic and guarantees the committed `.md`/`.toml` actually match their YAML source regardless of which tool made the edit.
- **Deleting an agent**: PostToolUse hooks fire on Edit/Write, never on a file deletion (which happens via Bash `rm`, a tool harness-expert doesn't have per its "No delete/Bash of your own" rule) — so removal can't be fully automatic in-session the way an edit is. Deletion is: delete `.agents/agents/<name>.yaml`, then run the compiler in full (non-`--check`) mode, which prunes any `.claude/agents/*.md`/`.codex/agents/*.toml` that no longer has a matching YAML source. `--check` mode also treats an orphaned compiled output (present, no matching source) as a staleness failure, so this is covered by the same `pre-commit` safety net as edits — a deletion that never got compiled still blocks the commit. `harness-expert` delegates both the `rm` and the compile run to `script-ops` in one call, the same delegation pattern it already uses for Bash-needing work (e.g. `audit.py`).
- **`agent_docs/harness-index.md` and `scripts/sync_harness.py` are removed outright**, not kept or folded into the compiler. Three reasons: the index's stated purpose ("so Codex can discover agents without native triggering") is now handled natively via `.codex/agents/*.toml` — Codex reads its own agent files directly, no index needed. The index was never actually auto-read by Codex to begin with — nothing wired `agent_docs/harness-index.md` into the `AGENTS.md` chain Codex concatenates; it only ever worked via manual paste-in. `.agents/agents/` is already a flat, single-directory, browsable source, so the aggregate table's remaining "human quick-scan" value doesn't justify a second generator re-deriving facts `compile_agents.py` already parses once.

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

- **`compile_agents.py`** (lives under `scripts/`; referred to by bare filename throughout this plan since the repo's dead-reference checker flags directory-qualified paths to files that don't exist yet) — the compiler. Responsibilities:
  - Parse each `.agents/agents/*.yaml` (real YAML — `pyyaml` should already be a dependency given the repo's Python tooling; confirm via `get_project_dependencies` before adding it).
  - Hold the two shared mapping tables as module-level constants: `TOOL_MAPPING` (Claude tool name → Codex `sandbox_mode`/`mcp_servers` contribution) and `MODEL_MAPPING` (alias → `{claude: ..., codex: ...}`).
  - Render `.claude/agents/<name>.md`: frontmatter fences, `name`, `description` (`>`-folded, with `version:`'s `vX.Y.Z` appended as the final token — the source's `description:` itself carries no version), `tools:` as a multi-line list, `model:` (resolved Claude name, omitted if absent), then the `body` as the Markdown content.
  - Render `.codex/agents/<name>.toml`: flat top-level keys per the real schema (`name`, `description` with the same appended `vX.Y.Z`, `developer_instructions` = `body`, `model` resolved via `MODEL_MAPPING`, `sandbox_mode` derived via `TOOL_MAPPING`, `[mcp_servers.*]` blocks for any `mcp__*` tool present).
  - **Prune orphaned outputs**: on a full (non-`--check`) run, glob existing `.claude/agents/*.md` and `.codex/agents/*.toml`; any whose stem has no matching `.agents/agents/<stem>.yaml` gets deleted (with a printed message) — this is what makes deleting an agent's YAML source actually remove its compiled files instead of leaving stale copies behind.
  - `--check` mode: exit 1 if any generated file doesn't match its would-be output, *or* if any compiled output is orphaned (no matching source).
  - **Verify before finalizing the Codex output shape**: whether a per-agent `[mcp_servers.*]` block in an agent's own TOML is required to grant that MCP server, or whether agents inherit everything already configured in `.codex/config.toml` by default and a per-agent block only restricts/overrides. Source research didn't resolve this cleanly — check empirically against one real compiled agent (e.g. `sql-expert`, which uses `mcp__dev__query`/`mcp__dev__profile_query`) before locking the compiler's MCP-block logic.

## Files to remove

- **`agent_docs/harness-index.md`** and **`sync_harness.py`** (`compile_agents.py`'s predecessor, also under `scripts/`) — deleted outright, per the removal decision above.

## Files to modify

- **`.claude/agents/*.md`** × 17 and **`.codex/agents/*.toml`** × 17 (new) — become compiler output; committed. First-run output for the `.md` files should be byte-for-byte equivalent to today's content except for the `tools:` list-vs-string formatting.
- **`.claude/agents/harness-expert.md`** (via its own source YAML, since it's included in the migration) — concrete text changes:
  - **Scope bullet 1** (`Write`/`Edit` allow-list): replace `.claude/` with `.agents/agents/*.yaml` in the editable list — becomes `.agents/agents/*.yaml`, `.agents/skills/`, `AGENTS.md`, `agent_docs/*.md`.
  - **`agent_docs/harness-index.md` never-hand-edit line** (currently lines 19-20): deleted, since that file no longer exists.
  - **New Scope bullet** in its place: `.claude/agents/*.md` / `.codex/agents/*.toml`: generated by `compile_agents.py` from `.agents/agents/*.yaml` — never hand-edit. Fix the YAML source; the PostToolUse hook regenerates automatically, or run `make compile-agents`.
  - **Bash-delegation paragraph** (currently only mentions `audit.py`): extend to cover deletion — removing an agent means delegating both `rm .agents/agents/<name>.yaml` and a `compile_agents.py` run (to prune the now-orphaned outputs) to `script-ops` in one call, same pattern as the existing `audit.py` delegation.
  - **Entity shapes section**: replace the current line (`Subagent (.claude/agents/*.md): name (bare, permanent), description (>-folded), tools (comma-separated string), model`) with the YAML source shape: `name` (bare, permanent), `version` (bare `X.Y.Z`, own field), `description` (`>`-folded, no version token — the compiler appends it), `tools` (multi-line list), `model` (alias, optional), `body` (block scalar, full system-prompt Markdown).
  - **Version marker section**: rewrite the mechanics — bump target is now the `version:` field directly (increment there, not inside `description:` text); same bump rules unchanged (patch/minor/major, new entity → `1.0.0`, ad-hoc agents get no marker). Add one line noting the compiler appends `vX.Y.Z` from `version:` onto the compiled `description:`'s last line in both outputs — this is why `version:` and `description:` stay separate fields in the source but merge in the compiled files.
- **`.githooks/pre-commit`**: add `uv run python3 compile_agents.py --check || exit 1` alongside the existing `check-lock.sh`/`check-uv.sh` lines, before `make test` (same "fail fast on the cheap check first" ordering already used there).
- **`Makefile`**: delete the `harness-index` target (lines 355-359) and its `.PHONY` entry (line 105); add a new `compile-agents` target (`uv run python3 compile_agents.py`) with a `.PHONY` entry in its place.
- **`hooks/harness_audit/sync_hook.py`**: repurposed — instead of regenerating `agent_docs/harness-index.md` on `.claude/agents/*.md` edits, it fires `compile_agents.py` for the touched agent(s) on Edit/Write to `.agents/agents/*.yaml`.
- **`hooks/harness_audit/budgets.py`**: add a budget-class entry for `.agents/agents/*.yaml`, mirroring the existing entry for `.claude/agents/*.md`.
- **`AGENTS.md`**:
  - Line 22 (Commands section): replace the `make harness-index` line with a `make compile-agents` description.
  - Line 70 (Agent & skill routing section): rewrite — both CLIs now discover agents natively (`.claude/agents/*.md` for Claude Code, `.codex/agents/*.toml` for Codex CLI), so the "Codex CLI reads `agent_docs/harness-index.md` instead" clause is dropped.
- **`agent_docs/architecture.md`**: rewrite the "Codex CLI adapter notes" section (lines 31-35) — drop the `agent_docs/harness-index.md` citation; correct the "Codex has no `Task` tool" framing to reflect that Codex now discovers agents natively via `.codex/agents/*.toml`, while keeping whatever remains true about Codex's in-session dispatch mechanics differing from Claude's `Task` tool.
- **`.agents/skills/harness-guardian/SKILL.md`**:
  - "## 4. Verify" step (line 52): replace the `sync_harness.py --check`/`make harness-index` line with a `compile_agents.py --check` equivalent, no mention of `harness-index.md`.
  - "Dual-harness layout" section: add a bullet documenting the new `.agents/agents/` → compiled-outputs mechanism, alongside the existing skills-symlink description.

## Verification

1. `uv run python3 compile_agents.py` — regenerate all 17 agents; diff the resulting `.claude/agents/*.md` against the pre-migration versions (should differ only in `tools:` formatting).
2. `uv run python3 compile_agents.py --check` — confirm clean exit 0 immediately after a fresh compile.
3. `uv run python3 hooks/harness_audit/audit.py .` — confirm no new token-budget violations from the added `.agents/agents/*.yaml` files, and no dead references to the now-deleted `agent_docs/harness-index.md`/`sync_harness.py`.
4. In Claude Code: edit one agent's `.agents/agents/<name>.yaml` (trivial description tweak), confirm the PostToolUse hook regenerates both outputs in-session without a manual compile step.
5. Manually inspect one compiled `.codex/agents/*.toml` (e.g. `sql-expert.toml`, given its MCP tool usage) against Codex CLI's real schema expectations; if possible, actually load it in Codex CLI and confirm it's accepted and the MCP tools are reachable — this is the check that resolves the open "does an agent need its own `[mcp_servers.*]` block" question above.
6. `make test` and a plain `git commit` (with `.agents/agents/*.yaml` intentionally left uncompiled) to confirm the `pre-commit` `--check` gate actually blocks the commit, then compile and confirm it passes.
