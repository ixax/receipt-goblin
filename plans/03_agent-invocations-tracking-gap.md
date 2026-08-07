# Fix missing agent_invocations rows for spawned subagents

## Context

The observability pipeline attributes LLM cost/tokens to subagents via the `x-claude-code-agent-id` header, ingested into `agent_usage.agent_invocation_id` and `agent_events.agent_invocation_id` (blank = orchestrator's own turn, non-blank = a specific subagent's own call).

Separately, `agent_invocations` records the spawn tree (`agent_id`, `parent_agent_id`, `subagent_type`), built by parsing Agent tool_use blocks out of the orchestrator's own message stream during ingest (`services/_common/src/ingest_parsing.py`, `ingest_db.py`).

Grafana panels 52/58/59 in `agents_overview.json` resolve *which* subagent spent what by joining `agent_usage.agent_invocation_id = agent_invocations.agent_id`.
When that join misses, the row shows up as cost/tokens with no resolvable `subagent_type`.
Money is counted but not attributed.

## Findings

Checked session `ae6a9ee8-8666-4efd-aa00-68be14b90a94` directly in ClickHouse:

- `agent_usage` for that session has **9 distinct non-blank `agent_invocation_id`** values with real activity (1-26 calls each, $0.02-$0.74).
- Only **1 of the 9** (`afa93f1ecb937d4bb` -> `script-ops`) has a matching row in `agent_invocations`, checked both scoped to the session and unscoped (searched `agent_invocations.agent_id` across the whole table).
- The other 8 do not exist in `agent_invocations` at all, under any session.
- Confirmed `session_id` is uniform across `agent_events` / `agent_usage` / `agent_invocations` (the persistent harness session UUID, from the `x-claude-code-session-id` header).
  This isn't a session_id mismatch - the rows are genuinely absent.
- One of the resolvable spawns (`code-locator`, agent_id `abda9a6e5765da6f8`) is a background/async Agent call (`Agent` tool, `run_in_background` default true).
  Worth checking whether background-agent spawns hit a different code path at ingest than synchronous ones, since that's the one type confirmed present.
  Need to check what tool/pattern produced the other 8 missing IDs to find the actual common factor.

Unmatched `agent_invocation_id`s from that session, for reproduction:

- `a83cf40fb1e8a0f08`
- `afcfb4b1911a5e2b9`
- `a09c4dc4442f74b3e`
- `ac44c8719fa98df04`
- `ab6783dd43b0342f8`
- `aa593d2a824ac053d`
- `a97b0d5fc65597103`
- `a4ac90bab292a3831`

## Investigation steps

1. Query across a wider sample of sessions (not just one) to size the gap: `count(distinct agent_invocation_id)` in `agent_usage` where non-blank, vs. `count(distinct agent_id)` in `agent_invocations`.
   Get a real ratio, not a single anecdote.
2. Read `_agent_invocations_from_messages()` in `services/_common/src/ingest_parsing.py` line-by-line against the actual tool_use block shapes it expects (Agent tool call + tool_result carrying the spawned agent_id).
3. Pull raw message payloads for a few of the 8 missing IDs (via whatever raw-payload retention exists - check webhook logs / raw table) and diff their tool_use/tool_result shape against what the parser matches.
   Likely suspects:
   - Different tool name for some agent-spawning paths (e.g. Workflow-spawned agents, or agents resumed via SendMessage rather than a fresh Agent tool_use block).
   - Missing tool_result before ingest runs (timing/ordering).
   - A subagent type whose result payload doesn't carry the agent_id in the expected field.
4. Once the missing shape is identified, extend the parser to cover it.
5. Decide on backfill.
   If raw payloads are retained long enough, re-run ingestion for affected historical rows.
   Otherwise treat old gaps as unrecoverable, consistent with how migration `013_agent_invocations_parent_id.sql` already accepts NULL `parent_agent_id` for pre-migration rows and falls back to an ASOF join in Grafana.
   That same fallback pattern could mask entirely-missing rows too - worth checking if panels 52/58/59 already do this or need a similar fallback join added.

## Files to touch

- `services/_common/src/ingest_parsing.py` - `_agent_invocations_from_messages()` and `_agent_invocation_id()`
- `services/_common/src/ingest_db.py` - `_agent_invocation_rows()` write path
- `services/webhook/src/server.py` - only if the gap traces to header/payload handling before parsing
- Possibly `services/grafana/dashboards/agents_overview.json` panels 52/58/59 if a fallback join is the right mitigation for historical gaps

## Verification

- Re-run the unmatched-ID query from Findings against a fresh session after the fix.
  Confirm `agent_invocations` now has a row for every non-blank `agent_invocation_id` seen in `agent_usage`/`agent_events` for that session.
- Spot-check panels 52/58/59 in Grafana show a named `subagent_type` instead of blank/unknown for the previously-missing spawns.
- If a backfill is run, verify total attributed cost by `subagent_type` before/after matches the previously-unattributed total (no double-counting, no dropped rows).
