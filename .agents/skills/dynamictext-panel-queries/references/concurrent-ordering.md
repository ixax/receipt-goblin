`sort_ts` vs `ts` mechanics for interleaving a background subagent's rows correctly - open when touching ordering/CTE logic, not for routine reading.

## The problem

A background subagent (an `Agent` tool_use the model didn't wait on) keeps running while the orchestrator continues its own work.
Sorting every row by its own real timestamp (`ts`) interleaves the subagent's steps with whatever the orchestrator did while it ran, in whichever order the clock happened to land - the tree looks jumbled.

## Fix: shared `sort_ts` per subagent block

Every subagent's rows (both prompt markers and event lines) share one `sort_ts`: the orchestrator's nearest-preceding `Agent` tool_use row.
The whole block then sorts as one contiguous unit right after its spawn point, ordered internally by real `ts`.
Orchestrator rows (`agent_invocation_id = ''`) are unaffected - their `sort_ts` always equals their own `ts`.

Two extra CTEs sit before `session_header`: `agent_spawn_events` (every orchestrator-level `Agent` tool_use row, `agent_invocation_id = ''`) and `child_anchor` (each subagent's `agent_id` ASOF-joined backward to the nearest-preceding row in `agent_spawn_events`, deduped to one row per `(session_id, agent_id)`).
Read panel-76's own `rawSql` for the exact CTE syntax - both already exist there.
Same nearest-before heuristic as `spawn_info` (no real parent link exists), run in the opposite ASOF direction: `spawn_info` goes from an orchestrator row forward to the next spawn, `child_anchor` goes from a spawn backward to the orchestrator row that triggered it.
Wherever a prompt/event row is built: join `child_anchor` on `(session_id, agent_invocation_id)`, then `sort_ts` is the joined anchor timestamp when `agent_invocation_id != ''`, else the row's own `ts`.

Do not skip the dedup in `child_anchor`.
`agent_invocations` can hold more than one row per `agent_id` (nothing runs `FINAL` against its `ReplacingMergeTree` here) - joining the raw ASOF result directly into the final SELECT would silently multiply every one of that subagent's event rows by however many duplicate `agent_invocations` rows exist.

This only groups one level deep, matching this panel's single-nesting-level limitation.
A grandchild agent (spawned by another sub-agent) anchors to the same top-level spawn point as its parent, since `agent_spawn_events` only looks at orchestrator-level (`agent_invocation_id = ''`) `Agent` rows.
