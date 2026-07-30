# mcp-dev multiquery support

## Context

`mcp-dev` (`services/mcp-dev/src/server.py`) exposes two MCP tools, `query(sql, max_rows)` and `profile_query(sql)`, each running exactly one SELECT/WITH statement against the local ClickHouse instance.
Several agents already need to run *several* independent queries per task and currently do it by looping single calls:

- `query-perf-runner` loops `mcp__dev__profile_query` once per resolved panel query, under a hand-written, easy-to-forget rule ("never more than 2 of these in flight at once") that lives only in that agent's prose, not enforced anywhere.
- `loadtest-sql` calls `query` 5 times per widget (min/avg/max timing), sequentially, one HTTP round-trip per call.

The user wants a real multiquery entry point: send an array of SQL statements, get back an array of per-query results (status, error if any, response), with the actual concurrency against ClickHouse throttled server-side (max 2 in flight) so batch callers can't overload the local instance - replacing ad-hoc, unenforced "keep it to 2" conventions with something the server itself guarantees.
The detailed format only needs to be documented where it's actually read (agent_docs/services/mcp-dev.md, loaded on demand per AGENTS.md's "per-service detail" pointer), not duplicated into every agent's always-loaded frontmatter description.

## Design

### 1. New tools, existing ones untouched

Add two new MCP tools to `services/mcp-dev/src/server.py`, alongside the existing `query`/`profile_query` (which keep their current signature, return shape, and validation-error-raises-immediately behavior - no consumer that only ever runs one-off queries needs to change):

```python
def query_batch(queries: list[str], max_rows: int = 200) -> dict
def profile_query_batch(queries: list[str]) -> dict
```

Return shape (same for both): `{"results": [...]}`, one entry per input query, **in input order**:

- Success: `{"status": "ok", "result": <same dict query()/profile_query() already returns>}`
- Failure (validation error or query error): `{"status": "error", "error": "<message>", "execution_time_ms": ...}`

A batch-level problem (empty list, more than `max_batch_queries` items) returns a top-level `{"error": "..."}` instead of `{"results": [...]}`, without touching ClickHouse.

### 2. Refactor to share logic + add throttling

Extract the current bodies of `query`/`profile_query` (past validation) into private helpers `_do_query(sql, max_rows) -> dict` and `_do_profile(sql) -> dict` - identical logic to today, just callable from both the single tool and the batch executor.
The `@mcp.tool()` `query`/`profile_query` functions become thin wrappers: validate, call the helper, return its dict (validation errors still raise/propagate exactly as today).

Add a module-level throttle shared by **all four** tools (single and batch alike - the goal is bounding real concurrent load on ClickHouse, not just within one batch):

```python
_MAX_CONCURRENT_QUERIES = _config.get("max_concurrent_queries", 2)
_ch_semaphore = threading.Semaphore(_MAX_CONCURRENT_QUERIES)
```

Acquire it (`with _ch_semaphore:`) around the actual blocking ClickHouse calls inside `_do_query` (`client.query(...)`) and `_do_profile` (`client.command(...)`, plus the `SYSTEM FLUSH LOGS` + `system.query_log` lookup).
This works because FastMCP already runs sync tool callables off the event loop thread (confirmed via `ClickHouseClientFactory`'s existing threading.Lock comment, which explicitly anticipates concurrent tool dispatch) - a `threading.Semaphore` correctly bounds real concurrency whether it comes from one batch call's internal fan-out or from two unrelated agents calling the server at the same moment.

For batch execution itself, use a small `concurrent.futures.ThreadPoolExecutor(max_workers=min(len(queries), _MAX_CONCURRENT_QUERIES))` to fan the list out; the shared semaphore is still the actual enforcement point (the pool just avoids spawning more idle threads than can ever run at once).
Each item is executed through a small wrapper that also catches the validation `ValueError` (which `_do_query`/`_do_profile` don't catch today, by design, for the single-tool path) so one bad query in a batch doesn't abort the rest:

```python
def _do_query_safe(sql, max_rows):
    try:
        return _do_query(sql, max_rows)
    except Exception as exc:
        return {"error": str(exc)}
```
(same pattern for `_do_profile_safe`).
The batch tool then maps `"error" in result` -> `{"status": "error", ...}` else `{"status": "ok", "result": result}`.

### 3. Config

Add to `services/mcp-dev/config.yml` (next to `max_rows_hard_cap`):
```yaml
max_concurrent_queries: 2   # server-side throttle shared by query/profile_query/query_batch/profile_query_batch
max_batch_queries: 10       # hard cap on queries per query_batch/profile_query_batch call
```

### 4. Docstrings carry the format, not every agent file

`query_batch`/`profile_query_batch`'s own `@mcp.tool()` docstrings state the array-in/array-out contract plainly (models read tool docstrings directly, no separate doc needed just to call the tool).
The **detailed** write-up - result shape, throttling rationale, batch cap, when to prefer batch over looping - goes in `agent_docs/services/mcp-dev.md` (extend the existing `## src/server.py` section), since that file is explicitly "per-service detail, read on demand" per `AGENTS.md`, not injected into every session.
Add one line each to:
- `README.md`'s existing tool bullet list (same one-line style as the current `profile_query` entry).
- `.claude/skills/clickhouse-sql/SKILL.md`'s "Sanctioned tools" section - one line each pointing at the new tools, no duplication of the full contract.

No new standalone doc file - `agent_docs/services/mcp-dev.md` already is that "not mandatory for every agent" location.

### 5. Update the two agents that actually loop queries today

Only agents with a real sequential-query-loop pattern change; agents doing single ad-hoc queries (`clickhouse-analyst`, `dashboard-panels-builder`, `dynamictext-panel-builder`, `sql-expert`'s one-off workflow C) are left as-is.

- **`.claude/agents/query-perf-runner.md`** (primary beneficiary): replace the per-query `mcp__dev__profile_query` loop (step 3) with a single `mcp__dev__profile_query_batch(queries=[...])` call over all resolved queries (chunking into groups of `max_batch_queries` if a resolve produces more than that), unwrapping each `{"status", "result"|"error"}` entry back into the flat per-query dict `query_perf.py save-run` already expects for `stats.json` - so `query_perf.py` itself needs no changes. Remove the now-redundant "never more than 2 of these in flight" prose (the server enforces it). Update `tools:` frontmatter: swap `mcp__dev__profile_query` for `mcp__dev__profile_query_batch`.
- **`.claude/agents/loadtest-sql.md`**: change step 3 to fire a widget's N timing iterations as one `mcp__dev__query_batch` call instead of N sequential `query` calls, reading `execution_time_ms` from each `result` entry (skip/note entries with `status: "error"`). Update `tools:` frontmatter to add `mcp__dev__query_batch`.

Both `.claude/agents/*.md` edits (frontmatter tool list + version bump) should go through the harness-expert agent per `AGENTS.md`'s "MUST BE USED PROACTIVELY" rule for this file type, not be hand-edited directly.

## Files touched

- `services/mcp-dev/src/server.py` - refactor + two new tools + semaphore.
- `services/mcp-dev/config.yml` - two new keys.
- `agent_docs/services/mcp-dev.md` - detailed format writeup.
- `README.md`, `.claude/skills/clickhouse-sql/SKILL.md` - one-line pointers.
- `.claude/agents/query-perf-runner.md`, `.claude/agents/loadtest-sql.md` - switch to batch tool, drop manual throttling prose, update `tools:`.

## Verification

- `docker compose up -d --build mcp-dev` (or equivalent rebuild) and confirm `/health` still returns ok.
- Call `query_batch` with a mix of 1 valid + 1 invalid (bad table) + 1 slow query via the MCP tool directly; confirm `results` has 3 entries in order, correct `status`/`error`/`result` shape, and total wall time reflects the 2-concurrent throttle (not fully serial, not fully parallel) when tried with >2 queries.
- Call `profile_query_batch` similarly and confirm `memory_usage_bytes` etc. appear per successful item.
- Fire two concurrent batch calls (or a batch + a single `query`) and confirm no more than 2 queries are ever active in `system.processes` at once during the overlap.
- Run `query-perf-runner`'s Job 1 end-to-end on a couple of panels and confirm the saved run file still has the same shape `query_perf.py report`/`diff` expect.
- Run `loadtest-sql` against one dashboard tab and confirm its timing table still populates correctly from batch results.
