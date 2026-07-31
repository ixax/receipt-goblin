# Fix fork/parent misattribution via explicit `parent_agent_id` column

## Context

Panel-99 ("Fork tree") and panel-76 ("Trace") in `agents_overview.json` both reconstruct fork hierarchy/ordering heuristically.
`agent_invocations` has no real parent link today - only an ASOF JOIN against the nearest-preceding `agent_spawn` event in `agent_events`, matched purely by timestamp within the session.
Under dense/concurrent `Agent` tool dispatch (several forks spawned close together from the same session), spawn events from different forks land close enough in time that the ASOF match sometimes picks the wrong "nearest" spawner.
Panel-99's tree visually collapses/flattens incorrectly as a result.

The fix: at ingest time, the webhook/webhook-worker already knows exactly who is making each `Agent` tool call.
`_agent_invocation_id(payload)` reads the caller's own `x-claude-code-agent-id` header (blank for main).
Record this as `parent_agent_id` directly on the `agent_invocations` row for the spawned child, instead of reconstructing it later by timestamp proximity in Grafana.
Keep the ASOF heuristic only as a fallback for historical rows inserted before this column existed.

## 1. Schema migration

New file `services/clickhouse/migrations/013_agent_invocations_parent_id.sql` (read the `clickhouse-migration` skill first).
Use `Nullable(String)` with no explicit default so pre-migration rows read back as `NULL` (ClickHouse serves the column's default for existing parts until merged), while all newly-inserted rows always carry a real string (`''` for main-spawned, or the parent's own agent_id).
This makes `IS NULL` a clean, permanent discriminant between "we don't know" (fall back to ASOF) and "we know it's root" (`''`).

```sql
ALTER TABLE agent_invocations ADD COLUMN parent_agent_id Nullable(String);
```

Add a header comment matching the convention in `012_litellm_alerts.sql` explaining the "why" above, and update the table comment block in `services/clickhouse/schema.sql` (lines 14-30) to add the column there too, matching post-migration state per repo convention.

No secondary index on `parent_agent_id`.
The table's own comment says it stays tiny (one row per subagent spawn), and both panels already filter to a single `session_id` before touching this column, so a skip index would add write overhead without a matching read benefit.

## 2. `services/_common/src/ingest_parsing.py` / `services/_common/src/ingest_db.py`

Note: this section's file/line references predate the webhook-worker-split refactor (`plans/webhook-worker-split.md`).
That refactor moved this code out of the old monolithic `services/webhook/src/clickhouse_ingest.py` into `services/_common/src/ingest_parsing.py` (pure parsing) and `services/_common/src/ingest_db.py` (ClickHouse I/O).
Re-verify every function's current file and line number before executing - not attempted here.

- `_agent_invocation_rows(session_id, messages, now=None)` (now in `ingest_parsing.py`) gains a required `parent_agent_id: str` parameter, inserted into the row tuple:
  `[agent_id, session_id, subagent_type, agent_version, description, parent_agent_id, now]`.
- `_INVOCATION_COLUMNS` gains `"parent_agent_id"` (position must match the row shape above).
  `_INVOCATION_SPAWNED_AT_IDX` is derived via `.index("spawned_at")` so it auto-adjusts.
- Three call sites, all passing the caller's own agent id (the fork making the `Agent` call - blank if main):
  - `ingest_standard_logging_payload` (now in `ingest_db.py`): call site is deliberately before `_derive_context` runs (comment explains why - race-window minimization).
    Don't reorder; instead call the already-existing pure helper `_agent_invocation_id(payload)` directly (it only reads `payload["metadata"]["requester_custom_headers"]`, no DB, no ctx needed) and pass that as `parent_agent_id`.
  - `build_event` (now in `ingest_parsing.py`): `ctx = _derive_context(...)` already runs first here, so just pass `ctx.agent_invocation_id`.
  - `services/reparse/src/reparse.py:_reparse_one`: same pattern as `ingest_standard_logging_payload` - call `_agent_invocation_id(payload)` directly before `_derive_context`, since it mirrors that function's structure.
    This is also what makes reparse the natural backfill path (see §4).

## 3. Panel updates (delegate - do not hand-edit dashboard JSON)

Both panels are in `services/grafana/dashboards/agents_overview.json`.
Read the `dashboard-panels` skill conventions and delegate to the owning agents.

**Panel 99 ("Fork tree")** goes to `dashboard-panels-builder`.
Rewrite the `fork_dedup` -> `fork_start` -> `fork_parent_raw` -> `fork_parent` CTE chain:

- `fork_dedup`: add `argMax(ai.parent_agent_id, ai.spawned_at) AS parent_agent_id` alongside the existing `subagent_type` dedup.
- `fork_start`: carry `parent_agent_id` through, add it to `GROUP BY`.
- `fork_parent_raw`: keep the existing ASOF LEFT JOIN against `spawn_events` unchanged (still needed as fallback), but rename its output to `parent_agent_id_asof` and additionally select `fs.parent_agent_id AS parent_agent_id_direct`.
- Split resolution into two steps (mirrors the existing raw->final pattern):
  - `fork_parent_resolved`: `if(parent_agent_id_direct IS NOT NULL, parent_agent_id_direct, coalesce(parent_agent_id_asof, '')) AS parent_id_pre`.
  - `fork_parent`: keep the existing self-guard, now against `parent_id_pre`: `if(parent_id_pre = agent_id, '', parent_id_pre) AS parent_agent_id`.
- Everything downstream (`fork_sib`, `fork_full`, `fork_chain`, `fork_render`, `fork_final`) is unchanged - they only consume the `parent_agent_id` output column, not how it was derived.

**Panel 76 ("Trace")** goes to `dynamictext-panel-builder`.
Update `child_anchor` so its ASOF match is constrained by real parent identity when known, instead of matching any nearby spawn event session-wide:

- `agent_spawn_events`: drop the `ev.agent_invocation_id = ''` filter, add `ev.agent_invocation_id AS spawner_agent_id` to the select list (mirrors panel-99's `spawn_events`).
- `child_anchor_raw`: change the ASOF join condition from `se.session_id = ai.session_id AND ai.spawned_at >= se.timestamp` to also require `se.spawner_agent_id = coalesce(ai.parent_agent_id, '')`.
  Because `parent_agent_id` is `NULL` for legacy rows, `coalesce(..., '')` reproduces the exact old top-level-only filter for those rows - no separate direct/fallback branch needed here, unlike panel-99's tree logic.
  New rows get anchored to their actual parent's spawn event instead of merely the nearest one.
- Update the panel's SQL comment that says "no parent link exists" since that's no longer fully true (still true only for pre-migration rows).
- After the edit, `dynamictext-panel-builder` must delegate the tag+`query_performance.json` mirror sync to `dashboard-panels-builder` per its own stated workflow.

## 4. Backfill

No dedicated backfill script needed.
`services/reparse/src/reparse.py` already recomputes `agent_invocations` rows from `ingest_raw` (which has no TTL) by calling `_agent_invocation_rows` directly, so once §2's change lands there, `make reparse-all` naturally backfills `parent_agent_id` for every historical row (ReplacingMergeTree on `agent_id` means the new row wins).
Run it after the migration and code deploy, then `OPTIMIZE TABLE agent_invocations FINAL` (reparse.py already logs this reminder for the other tables).
Historical rows are handled at query time regardless via the `IS NULL` fallback, so backfill is a nice-to-have for full accuracy, not a hard requirement before the panels are safe to use.

## 5. Tests

Update `services/_common/tests/test_ingest_parsing.py`:

- `test_agent_invocation_rows_success_builds_one_row_per_spawn`, `test_agent_invocation_rows_unsuccess_no_spawns_returns_empty_list` (line numbers stale post-webhook-worker-split, re-verify): add the new `parent_agent_id` arg to `_agent_invocation_rows(...)` calls and assert it lands in the right row position.
- Add a case verifying `ingest_standard_logging_payload` and `build_event` populate `parent_agent_id` from the request's own `x-claude-code-agent-id` header (blank for main-session payloads, the header's value for subagent payloads).
  Reuse the existing `success_subagent_call` capture fixture.
- Delegate the actual `make test` run to `webhook-test-runner` per its standing instruction - never run pytest inline.

## Verification

1. `webhook-test-runner` for `make test` after the ingest changes.
2. Apply the migration (confirm with the user before running DDL - migrations are otherwise design-only for `sql-expert`, which is read-only against ClickHouse).
3. Spot-check via `mcp__dev__query`: pick a session with several nested forks, confirm `SELECT agent_id, parent_agent_id FROM agent_invocations WHERE session_id = '<x>'` shows non-null values for forks spawned after the deploy.
4. Run `make reparse-all` (or `reparse SESSION=<x>` for a fast spot-check) then re-run the query above to confirm historical rows backfill.
5. Load panel-99 and panel-76 for a session known to have had the collapsed-tree symptom; confirm the tree now nests correctly.
   Use `loadtest-sql`/`sql-expert` before/after perf-check on both rewritten queries per `dashboard-panels` convention, since both are non-trivial rawSql rewrites.
