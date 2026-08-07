---
date: 2026-08-07
context: |
  User asked to analyze all agents and skills and design a refactor that
  pulls as much procedural content as possible out of agent bodies into
  skills, leaving agents as thin orchestrators or cheap-model shells.
  Three research passes (all 17 .claude/agents/*.md files, all 12
  .agents/skills/*/SKILL.md files, and AGENTS.md/agent_docs/harness-guardian
  conventions) informed the plan below. Approved via /plan; Batch 1 has
  already been executed by harness-expert as of this write.
---

# Refactor: extract procedural content from agents into skills

## Context

This repo already has an explicit design philosophy for where a rule should live.
`harness-expert.md`'s classification is UNIVERSAL / SCOPED / TASK / DEEP / ENFORCEABLE / OBSOLETE, backed by `harness-guardian`'s audit workflow.
Three research passes over all 17 `.claude/agents/*.md` files and all 12 `.agents/skills/*/SKILL.md` files show that classification hasn't been applied consistently to the agents themselves.
Several agents carry heavy step-by-step runbooks (decision trees, exact commands, numbered workflows) inline in their body instead of in a skill.
Two pairs of agents independently re-document the same mechanics.

Goal: apply the harness's own existing classification rule to its agents.
Pull TASK/DEEP content out of agent bodies into skills - the "how", not the "should I trigger / who do I delegate to".
Leave each agent as one of two shapes:

- An **orchestrator** - judgment, delegation graph, standing per-edit rules, short body, points at skill(s) for mechanics; usually `claude-sonnet-5`.
- A **cheap-model shell** - thin wrapper around one script/CLI with minimal own reasoning, mechanics defined explicitly enough that a cheap model can follow them without inferring anything; usually `claude-haiku-4-5`.

The clearest sign this matters: `dev-ops.md` runs on `claude-haiku-4-5` but carried a 148-line decision tree in prose.
That's exactly the case where a cheap model needs the decision procedure as an explicit, skill-owned checklist rather than something to reason through from paragraphs.

**Sequencing decision (user-confirmed):** do this content refactor against today's hand-written `.claude/agents/*.md` files now.
`plans/dual-harness-agent-compiler.md` (staged, unimplemented) proposes moving agent source to `.agents/agents/<name>.yaml` later.
If/when that lands, it migrates the already-slimmed bodies produced here, rather than the current bloated ones.
Don't block this plan on that one.

**Scope decision (user-confirmed):** full sweep - all 9 flagged agents, both documented duplications, and the 2 doc gaps - but executed as separate batches, not one edit.
Reason: every touched file routes through `harness-expert` (sole editor) and the harness-audit hooks.
`harness-guardian`'s own rule is "edit harness files between sessions, never mid-session", since each edit rebuilds the conversation cache at full price.

**Ownership note:** `harness-expert` is the sole editor of every `.claude/agents/*.md`, `.agents/skills/*`, `AGENTS.md`, and `agent_docs/*.md` file per its own frontmatter.
Every batch below must be executed by delegating to `harness-expert`, not by editing these files directly.

## Batch 1 - cheap-model shells with oversized prose - DONE

These four agents run on `claude-haiku-4-5` but carried the heaviest reasoning burden in prose.
Extracting the mechanics into skills also removed two documented duplications.
Executed by `harness-expert` in this session:

1. **New skill `grafana-dashboard-parsing`** (`.agents/skills/grafana-dashboard-parsing/SKILL.md`, v1.0.0) - `parse_dashboard.py` subcommand reference plus the per-file `RowsLayout` gotcha table.
   `dashboard-parser.md` (v1.3.1 -> v1.3.2) and `loadtest-sql.md` (v1.3.2 -> v1.3.3) both slimmed to point at it instead of each independently re-documenting the same CLI.

2. **New skill `grafana-query-macros`** (v1.0.0) - Grafana-to-plain-SQL macro/variable substitution rulebook, extracted from `loadtest-sql.md`.

3. **New skill `litellm-alerting-mechanics`** (v1.0.0) - per-`alert_type` trigger mechanics and the `litellm_alerts` verification query, extracted from `litellm-test-alerting.md` (v1.1.2 -> v1.1.3).
   The risk-tier authorization policy (safe-default / opt-in / infra-risk-never / not-testable) stayed in the agent - a standing permission judgment, not a mechanical procedure.

4. **New skill `service-lifecycle`** (v1.0.0) - restart/rebuild/recreate decision tree, Makefile target comparison table, exact recreate command, backup/restore mechanics, extracted from `dev-ops.md` (v1.17.0 -> v1.17.1).
   Also fixed: `dev-ops.md` claimed it could "delegate to `harness-expert`" for doc sync despite having no `Agent` tool.
   Resolved by rephrasing to surface that need back to the orchestrator instead of adding the tool, keeping the haiku shell minimal.

All four skills added to `AGENTS.md`'s `SKILLS:` list.

## Batch 2 - sonnet orchestrators, workflow extraction - DONE

Executed by `harness-expert` in this session.
All three version bumps were patch-level: compaction/dedup, no new capability or behavior change.

5. **New skill `loadtest-workflow`** (v1.0.0) - Phase 0-6 runbook (exact shell/SQL commands, thresholds, stop-condition values) extracted from `loadtest-runner.md` (v1.9.2 -> v1.9.3).
   Agent kept phase sequencing, delegation targets, both standing protocols (`NEED USER INPUT:`, status checkpoint), and the judgment of *when* a stop condition applies.

6. **New skill `query-benchmark-workflow`** (v1.0.0) - consolidates the `query_perf.py` toolkit and Workflow A/B/C previously duplicated between `sql-expert.md` (v1.2.3 -> v1.2.4) and `dashboards-expert.md` (v1.16.0 -> v1.16.1).
   Each agent kept only its own "when it applies" judgment: every schema change for `sql-expert`, every panel `rawSql` rewrite for `dashboards-expert`.

7. **New skill `stale-ref-sweep`** (v1.0.0) - 5-step sweep procedure and 3-bucket reporting format extracted from `stale-ref-sweeper.md` (v1.1.2 -> v1.1.3).
   The classification judgment itself (stale-live vs. legitimate-historical vs. ambiguous, with the `event_sources` migration example) stayed verbatim in the agent body.

All three skills added to `AGENTS.md`'s `SKILLS:` list.
`make harness-index` confirmed unchanged - `agent_docs/harness-index.md` already reflected all four version bumps.

## Batch 3 - meta cleanup and doc gaps - DONE

Executed by `harness-expert` in this session.

8. **Self-audit of `harness-expert.md`**: measured first (3051 tokens / 173 lines, confirmed the largest agent body), then classified every section rather than assuming the plan's hunch.
   Two sections qualified as genuine extraction candidates, both duplication against material already owned elsewhere rather than TASK/DEEP content needing a new skill: the markdown-formatting section restated the md-format skill's own read-once-gate conditions, and the self-delegation-gate section restated the hook's exact denylist verbatim from `hooks/harness_audit/self_delegation_gate.py`.
   Both compacted to directive + pointer.
   Everything else (Scope, Version marker, Description content spec, Body style, Token economy) stayed - `harness-guardian/SKILL.md` itself attributes ownership of these per-edit standing rules to `harness-expert.md`, so they can't move to a skill.
   Result: 3051 -> 2920 tokens, 173 -> 156 lines.
   Patch bump v1.23.1 -> v1.23.2.
   Side effect caught during Verify: trimming the self-delegation-gate prose had orphaned `AGENTS.md`'s pointer (it referenced the now-gone prose copy) - fixed to point at the hook file directly.

9. **Doc gaps**:
   - Confirmed `query-performance-sync` is real and actively invoked (`dashboards-expert.md`), added it to `AGENTS.md`'s `SKILLS:` list.
   - `trace-debugging` decision: wired an explicit `Skill(trace-debugging)` reference into `clickhouse-analyst.md` (v1.6.4 -> v1.6.5), not `sql-expert.md`.
     Reasoning: `clickhouse-analyst` is the general delegate for cost/token/error/latency/adoption analysis against `agent_events`/`agent_usage` - exactly what `trace-debugging`'s own TRIGGER clause covers.
     `sql-expert` is scoped to DBA duties (schema fit, query performance, benchmarking), not session/call-chain debugging, so it was the weaker fit.

Verification run as part of this batch: `hooks/harness_audit/audit.py` exits clean (one pre-existing, unrelated violation in a plans file untouched by this work); `sync_harness.py --check` confirms `agent_docs/harness-index.md` is current.

## Execution notes

- One batch per session/turn - don't chain multiple batches into one edit spree, per `harness-guardian`'s cache-hygiene rule.
- Every new skill goes under `.agents/skills/<name>/SKILL.md`, the canonical path (`.claude/skills` is a symlink - never create there directly), with frontmatter matching the existing style: `name` + `description` with a `TRIGGER -` / `SKIP` clause, no `tools`/`model` field.
- Each new skill needs exactly one added line in `AGENTS.md`'s `SKILLS:` list.
- Version-bump decisions for touched agents (patch vs. minor) are `harness-expert`'s call per its own versioning rule - don't hardcode a bump level in the delegation request.

## Verification

- After each batch, run `hooks/harness_audit/audit.py` for a before/after token-budget comparison on every touched agent and new skill.
- Run `sync_harness.py --check` (per `harness-guardian`'s Verify step) to confirm dual-harness (Claude Code / Codex) consistency.
- Spot-check behavior preservation: re-invoke one refactored agent per batch on a real task it previously handled (e.g. ask `dashboard-parser` to look up a specific panel), and confirm it reads the new skill and produces the same result as before the extraction.
