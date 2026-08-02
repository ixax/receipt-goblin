# Full-text search over prompt/response bodies

## Context

`agent_messages` already stores the full `prompt_text`/`response_text` per call (`ZSTD(3)`-compressed), `ORDER BY (session_id, litellm_call_id)`.
There is no indexed way to search it by content - only a full-column scan via `LIKE`/`match`, which is exactly the gap the earlier Langfuse comparison flagged (browsable/searchable call bodies), except the data already exists here.
This closes that gap without standing up a second system.

## Design

### Index choice

Add a ClickHouse data-skipping index on `prompt_text` and `response_text` - either `tokenbf_v1` (good for whitespace/punctuation-tokenized natural language) or `ngrambf_v1` (good for substrings without token boundaries, e.g. code fragments, identifiers).
Both columns mix natural language and code, so the right choice isn't obvious from the schema alone - benchmark both against real data before deciding, using `sql-expert`'s existing before/after query-perf discipline (`query_perf.py`), not a guess.

### Migration

Follow the `clickhouse-migration` skill: a new numbered file in `services/clickhouse/migrations/`, `ALTER TABLE agent_messages ADD INDEX IF NOT EXISTS ...`, and the matching end-state added to `schema.sql`.
Data-skipping indexes only apply to parts written after the `ALTER` - existing historical parts need an explicit `ALTER TABLE agent_messages MATERIALIZE INDEX <name>` to backfill.
That materialize pass rewrites existing parts and can be slow at current data volume - run it off-hours and watch CPU, same caution `sql-expert`/`dev-ops` already apply to other heavy ClickHouse operations.

### Query surface

A free-text search box doesn't fit Grafana's table-panel model well.
Two realistic options, not mutually exclusive:

- A dashboard text-input template variable plus a `WHERE hasToken(...)` / `match(...)` clause on a new or existing panel, scoped to a session/agent/time-range the user has already narrowed down.
- A documented query pattern for `clickhouse-analyst`/`sql-expert` to use directly when asked "find sessions where the agent said X" - lower effort, no dashboard change needed, and the subagent fleet already exists to serve this conversationally.

Start with the second (zero new panels, just a documented pattern), add the dashboard variable only if that turns out to be too slow a workflow in practice.

### Redaction

`prompt_text`/`response_text` can contain secrets/PII pulled from real sessions.
Making them searchable doesn't change who can reach them - the same privileged surface (`mcp-dev`, `clickhouse-analyst`, `sql-expert`) already has raw read access today - so this isn't new exposure, just faster access to what's already reachable.
Worth a one-line confirmation with the user before shipping, not a blocker.

## Rollout

1. Add the index in a migration (pick `tokenbf_v1` vs `ngrambf_v1` after benchmarking both).
2. `MATERIALIZE INDEX` against historical partitions, off-hours.
3. Document the search query pattern for `clickhouse-analyst`.
4. Add a dashboard text-search panel only if the documented-pattern workflow proves insufficient.
