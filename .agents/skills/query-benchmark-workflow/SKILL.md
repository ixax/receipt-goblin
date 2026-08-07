---
name: query-benchmark-workflow
description: >
  Before/after query-performance benchmarking workflow for Grafana dashboard panels: the query_perf.py toolkit, the query-perf-runner delegation shape, and the current-speed/before-after/one-off workflows.
  TRIGGER - read before benchmarking a panel's current speed, before any panel rawSql rewrite, or before delegating to query-perf-runner.
  SKIP for the judgment of when a rewrite needs this - every schema change for sql-expert.md, every panel rawSql rewrite for dashboards-expert.md.
  v1.0.0
---

## The toolkit (`services/grafana/scripts/query_perf.py`)

Read its docstring once - source of truth for exact syntax.
In short: `resolve` turns a panel's `rawSql` into runnable SQL (macro/`$variable` substitution, one fixed table in the script); `save-run` records `profile_query` results into a timestamped JSON under `.claude/data/query_perf_runs/` (persists across sessions - not scratch, see AGENTS.md's `.claude/data/` note); `diff`/`report` compare or print run files.
`resolve`/`save-run`/`diff`/`report` are pure Python - only the `profile_query` calls between them need an agent, and that execution is `query-perf-runner`'s job.

Skip panel-76 ("Trace") and companion panel-77 always - `resolve` already excludes them by default; don't override.

## Delegating to `query-perf-runner`

A cheap, mechanical haiku agent: `resolve` -> loop `profile_query` -> `save-run`, or `diff`, returning only a short summary/diff table.
Give it: dashboard file (usually the default), panel selector, label, any `--hours`/`--var` overrides.
It can't ask clarifying questions (no `AskUserQuestion`, one-shot delegation) - under-specify and it picks the script's defaults and states the assumption.
Read `.claude/agents/query-perf-runner.md` for its exact behavior before delegating.

## Workflow A - current speed, no rewrite

1. Delegate to `query-perf-runner`, Job 1: panel selector = whatever the caller named, else `--all` (per-project default - never ask "which panels").
   Label like `now-<short-topic>`.
2. It returns a run file path; run `uv run python3 services/grafana/scripts/query_perf.py report <path>` and present that table.

## Workflow B - evaluating/making a rewrite, mandatory before/after, no exceptions

Applies whenever a panel's SQL is about to change for any reason: an explicit speed-up ask, or a side effect (schema change touching panel SQL, a bug fix touching a WHERE).
"It should be faster" is not a finding - a `diff` table is.

1. `query-perf-runner`, Job 1, affected panel(s), label `before` (or `before-<topic>` for several in one session).
2. Make the edit.
3. `query-perf-runner` again, same selector, label `after`/`after-<topic>`.
4. `uv run python3 services/grafana/scripts/query_perf.py diff <before-run> <after-run>`; report the table.
   Exit code 1 means something got worse - say so plainly, don't bury it.
5. If the rewrite changes what the query returns (not just how it runs): verify separately via `mcp__dev__query` on both versions and diff actual result values before trusting the perf numbers - a faster query returning wrong data is not a fix.

## Workflow C - a one-off query, not (yet) a dashboard panel

Use `mcp__dev__profile_query` directly - the `query_perf.py`/runner machinery exists for panel-tracked, repeatable runs.
