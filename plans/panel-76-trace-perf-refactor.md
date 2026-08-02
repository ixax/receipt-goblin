# Panel-76 ("Trace") further speed-up, plus background hierarchy research

## Context

This started as a broader question — whether the ClickHouse data model needs refactoring toward an OTel-style explicit parent/span linkage, since several dashboard queries reconstruct trees.
That research (kept below as background) found the real, concrete pain was concentrated in one place: panel 76 ("Trace") in `services/grafana/dashboards/agents_overview.json`, which had a confirmed live perf bug (`.agents/skills/clickhouse-sql/GOTCHAS.md`: "CTE re-execution multiplication ... confirmed on panel 76").

The user has since refactored panel-76 themselves (uncommitted, `git status` shows the file modified) and asked specifically: **look at it again, can it still be sped up?** Re-reading the live `rawSql` now (not the stale docs) found:

- **The user's fix already landed and is real.** A comment block in the query ("MERGED (candidate2 rewrite)") shows three separate `UNION ALL` branches that each independently re-scanned `prompt_final` (tie=2 main marker, tie=4 judge_reason, tie=4 stop_hook_reason) were collapsed into one pass using `arrayFilter` + `ARRAY JOIN` over a per-row tuple array. This is exactly the documented GOTCHAS.md remedy, correctly applied.
- **The same anti-pattern still exists one layer down, on `scoped_events`.** Grepped the live SQL: `scoped_events` (the base per-event CTE — joins `agent_events` to `dedup_messages` and computes 3 window functions) is referenced **8 times directly** (`plan_proposals_match`, `tool_render`, `reply_trunc`, `reply_render`, `failure_error`, the main tie=3 branch, the tie=1.5 collaboration-mode branch, the AskUserQuestion tie=4 branch) — plus once more inside `prompt_calc`, which is itself re-executed on every one of `prompt_final`'s **4 references** (`stats_prompts`, `tie2_ts`, `first_real`, the main tie=2/4 branch). That's **~12 effective re-executions** of `scoped_events` in one query run. None of `tool_render`/`reply_trunc`/`reply_render`/`failure_error`/`plan_proposals_match`/`prompt_calc` do any `GROUP BY`/aggregation of their own — they're all pure per-row projections or an ASOF join keyed off `scoped_events`'s own columns, so there's no structural reason they need to be separate CTEs each re-scanning the base query from scratch, rather than one wider CTE (or extra columns computed inline on `scoped_events` itself).
- **Measured, not just counted**: isolated `scoped_events`'s own definition (dedup_messages join + 3 window functions) and ran it standalone via `mcp__dev__profile_query` against a real 1,584-event session (`d82819ad-6d93-4d2c-a405-de5459035ba8`, ~4hr span): **136.5ms, 20,680 rows read, 678KB memory** for one execution. At ~12 effective executions that's roughly **1.6s+ of pure redundant base-CTE re-scanning alone**, before any of the per-branch regex/markdown-conversion/JSON-extraction work layered on top of each one — and it scales up further for busier/longer sessions than this test one.
- **Separate, unrelated correctness bug found while reading the live SQL — flagging now since the file is uncommitted:** every `has([...], ...)` filter in the current panel-76 `rawSql` has a **literal, hardcoded session_id** (`'d82819ad-6d93-4d2c-a405-de5459035ba8'`) baked in, instead of the `${session_id:singlequote}` Grafana template macro used in 82 other places across this same dashboard file. Confirmed that session is real (1,584 real events) — this is almost certainly a live-editing artifact (tested against that one session in Grafana's query inspector, then saved without swapping back to the macro). **As currently saved, this panel will always show that one specific session's trace regardless of what's picked in the dashboard's Session dropdown.** Needs fixing before this is usable for any other session, independent of the performance question.

## Recommended approach

### 1. Fix the hardcoded session_id first (correctness, blocks normal use of the panel)

In `services/grafana/dashboards/agents_overview.json`, panel-76's `rawSql`: replace every literal `'d82819ad-6d93-4d2c-a405-de5459035ba8'` array back with the `${session_id:singlequote}` macro form used everywhere else in this file (check a sibling panel's `has(${session_id:singlequote}, ...)` pattern for the exact syntax this dashboard uses).
Use the `dashboards-expert` agent for this edit (owns all panel JSON edits) and follow the "Safe JSON-editing procedure" / brace-matching splice convention from `.agents/skills/dynamictext-panel-queries/SKILL.md` — do not run a blanket string replace across the whole file, since other panels' own literals/macros must stay untouched.

### 2. Collapse the remaining `scoped_events` re-execution (the actual speed-up)

Merge `tool_render`, `reply_trunc`, `reply_render`, `failure_error`, and `plan_proposals_match` into `scoped_events` itself — either as additional computed columns in `scoped_events`'s own `SELECT` (simplest: these are all scalar expressions on `calculated_payload`/`response_text`/`tool_name`/`status`, no join needed except `plan_proposals_match`'s ASOF against `plan_proposals`, which can stay a small join directly inside `scoped_events`'s own definition) or one single downstream CTE computed once and referenced everywhere those five currently are.
This mirrors exactly the fix already validated for `prompt_final`'s 3-branch merge — same technique, one layer down.

Do the same for `prompt_calc`/`prompt_final`'s remaining 4 references (`stats_prompts`, `tie2_ts`, `first_real`, the main tie=2/4 branch) if it falls out naturally from step 2's restructuring.
`prompt_calc` already sits downstream of `scoped_events`, so once `scoped_events` is widened, re-check whether `prompt_final` still needs to be computed 4 separate times or whether the same array/`ARRAY JOIN` collapse technique already used for the tie=2/4 lines can extend to cover `stats_prompts`/`tie2_ts`/`first_real` too.
Don't force this if it doesn't fall out cleanly — the `scoped_events` collapse alone already removes the bulk of the measured redundant cost.

Verify before/after using the `sql-expert`/`query-perf-runner` workflow (`query_perf.py resolve` + `mcp__dev__profile_query`, before/after run comparison) against the same real session (`d82819ad-6d93-4d2c-a405-de5459035ba8`) used for this investigation's baseline measurement, so the improvement is a real number, not a guess.
Remember `query-performance-sync`: keep `dashboards-health/query_performance.json` in sync with the panel-76 query edit.

## Background: broader hierarchy-model research (secondary, not the current focus)

Kept for context, lower priority than the above.
Full investigation found:

- No explicit per-call parent link exists in the schema (`agent_events`/`agent_usage`/`agent_messages` have no `parent_span`-equivalent column). The only real (non-heuristic) parent link is `agent_invocations.parent_agent_id` (migration `013_agent_invocations_parent_id.sql`), and it operates at subagent-invocation granularity, not per-event.
- Panel-99 ("Fork tree") already uses `parent_agent_id` correctly via a 7-level unrolled self-join (ClickHouse has no recursive CTE support) — no urgent issue there.
- Panel-76 already uses `parent_agent_id`-scoped ASOF joins too (verified live — a stale doc in `.agents/skills/dynamictext-panel-queries/SKILL.md` still describes the old, unscoped nearest-timestamp heuristic; worth refreshing that doc once the above lands, low priority).
- **Recommendation if this gets picked up later**: add one new column, `agent_invocations.parent_call_id Nullable(String)` (migration `014_...sql`, mirroring 013's pattern), capturing the exact spawning call's own `litellm_call_id` — already in scope in `services/_common/src/ingest_parsing.py`'s `_agent_invocation_rows` call sites, just never persisted. Backfillable via `reparse.py` for every row with an `ingest_raw` entry (stronger backfill story than 013 had). Do **not** build a general per-event parent-chain (true OTel span-per-call) — no consumer needs call-to-call ordering within one invocation beyond the `timestamp` column already available for free; that would be completeness for its own sake, not a fix for anything observed. Do not touch `trace_id` (already an alias of `session_id`, fine to leave) or `turn_id` (dead, hardcoded 0 — separate, unrelated cleanup).

## Critical files

- `services/grafana/dashboards/agents_overview.json` — panel-76 ("Trace"): fix hardcoded session_id, then collapse `scoped_events`/`prompt_final` re-execution
- `.agents/skills/dynamictext-panel-queries/SKILL.md` — refresh stale "Concurrent subagent ordering"/"Data-model facts" sections once the rewrite lands
- `services/grafana/dashboards-health/query_performance.json` — keep in sync per `query-performance-sync` skill
- (background/secondary) `services/clickhouse/migrations/013_agent_invocations_parent_id.sql`, `services/_common/src/ingest_parsing.py`, `services/_common/src/ingest_db.py`, `services/reparse/src/reparse.py`

## Verification

- After step 1: confirm panel-76 responds to changing the Session dropdown (shows a different trace for a different selected session), not just the one hardcoded session.
- After step 2: `query_perf.py` before/after comparison (delegate to `sql-expert`/`query-perf-runner`) against the same real session used in this investigation (`d82819ad-6d93-4d2c-a405-de5459035ba8`, 1,584 events) — expect a measurable drop in `query_duration_ms`/`read_rows`/`memory_usage_bytes` versus the current baseline (full-query baseline not yet measured directly; the isolated `scoped_events` baseline alone was 136.5ms/20,680 rows/678KB per execution, ~12 executions in the current query).
- Visual check in Grafana: the rendered trace tree looks identical before/after the SQL restructuring (this is a pure performance refactor, not a behavior change) — spot-check the same session's output.
