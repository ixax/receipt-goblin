-- Schema for the local AI agent cost/efficiency tracking stack.
-- Applies automatically on first container start via
-- docker-entrypoint-initdb.d (see docker-compose.yml). To reapply manually
-- (e.g. after the volume already exists): docker exec -i agent-tracking-clickhouse
-- clickhouse-client --multiquery < clickhouse/schema.sql

CREATE TABLE IF NOT EXISTS agent_registry
(
    agent_name    String,
    version       String,
    description   String,
    source_file   String,
    registered_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(registered_at)
ORDER BY (agent_name, version);

CREATE TABLE IF NOT EXISTS skill_registry
(
    skill_name    String,
    version       String,
    description   String,
    source_file   String,
    registered_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(registered_at)
ORDER BY (skill_name, version);

-- agent_id -> subagent_type lookup, recovered from the orchestrator's own
-- LiteLLM call: an Agent tool_use block paired with the tool_result that
-- follows it (containing "agentId: <hex>") tells us what a given agent_id
-- actually is. Populated by webhook/src/clickhouse_ingest.py before it
-- writes any row whose agent_invocation_id needs resolving - see AGENTS.md.
-- One row per subagent spawn, so this table stays tiny - agent_id is left
-- as a plain String (not worth a tighter type for a table this small).
CREATE TABLE IF NOT EXISTS agent_invocations
(
    agent_id      String,
    session_id    String,
    subagent_type LowCardinality(String),
    description   String,
    spawned_at    DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(spawned_at)
ORDER BY (agent_id);

-- One row per lifecycle event (hook invocation). raw_payload keeps the full
-- untouched JSON Claude Code sent, so any field missed by the extracted
-- columns can still be recovered later.
--
-- PARTITION BY/ORDER BY: nearly every dashboard query filters by a time
-- range first (across many sessions), and only two panels (Full trace, Call
-- stack) filter by a specific session_id - so timestamp leads the sort key
-- for partition/granule pruning on the common case, with session_id second
-- for the two session-scoped panels (cheap once already time/partition-
-- pruned, since one session's rows cluster in a narrow time window anyway).
--
-- Half-year partitions ("2026-H1"/"2026-H2") are sized for archival, not
-- query pruning: once a half stops being actively queried, detach and ship
-- it off (ALTER TABLE agent_events DETACH PARTITION '2026-H1' moves it to
-- `detached/` untouched by future queries/merges - copy it out for backup,
-- ATTACH it back if you ever need to query that period again). No TTL/
-- DELETE - data is kept forever unless you detach it yourself.
--
-- session_id/trace_id stay String rather than UUID: they come from three
-- different sources depending on what's available at ingest time
-- (x-claude-code-session-id header, trace_id, litellm_call_id, or "" as a
-- last resort - see _session_and_trace_id in clickhouse_ingest.py), so
-- guaranteeing well-formed UUIDs on every path isn't free, and these are a
-- small fraction of a row's bytes next to raw_payload anyway.
--
-- agent_name/skill_name/command_name/tool_name/etc. are LowCardinality:
-- the actual set of distinct values is bounded (the agents/skills/tools
-- registered in this repo), so dictionary encoding shrinks storage and
-- speeds up every filter/GROUP BY/JOIN on them - the set(...) skip indexes
-- below replace the old bloom_filter ones for the same reason (bloom_filter
-- is for genuinely high-cardinality columns; set is cheaper and exact for a
-- small bounded set of values).
CREATE TABLE IF NOT EXISTS agent_events
(
    timestamp         DateTime64(3),
    user_id           LowCardinality(String),
    session_id        String,
    trace_id          String,
    parent_session_id String,
    turn_id           UInt32,
    sequence_id        UInt32,
    event_type        LowCardinality(String),
    tool_name         LowCardinality(String),
    agent_name        LowCardinality(String),
    agent_version     LowCardinality(String),
    skill_name        LowCardinality(String),
    skill_version     LowCardinality(String),
    -- Slash command that kicked off the current chain of calls (e.g.
    -- "whatsup"), recovered from the "<command-name>" tag Claude Code
    -- injects into the triggering user message - see
    -- webhook/src/clickhouse_ingest.py:_active_command_name. Deliberately
    -- has no version column: commands are meant to stay a stable,
    -- version-independent entry point even when the skill/logic behind
    -- them is renamed on every version bump.
    command_name      LowCardinality(String) DEFAULT '',
    -- x-claude-code-agent-id when this row is a subagent's own call, blank
    -- for the orchestrator's own turns. See agent_invocations above.
    agent_invocation_id String DEFAULT '',
    status            LowCardinality(String),
    latency_ms        Nullable(UInt32),
    -- Set when this call's incoming "messages" ends with a tool_result
    -- marked is_error - i.e. this call is reacting to a tool that just
    -- failed. Recovered at ingest time only (from "messages", which is
    -- dropped from raw_payload to keep rows small) - not backfillable from
    -- already-ingested rows the way tool_name/cost were, since the source
    -- data is gone once ingested. Distinct from this row's own tool_name,
    -- which is whatever tool (if any) THIS call's own response goes on to
    -- invoke next.
    failed_tool_name  LowCardinality(String) DEFAULT '',
    failed_tool_args  String DEFAULT '',
    failed_tool_error String DEFAULT '',
    raw_payload       String CODEC(ZSTD(3)),
    INDEX idx_tool_name tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_agent_name agent_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_command_name command_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_user_id user_id TYPE set(1000) GRANULARITY 4,
    INDEX idx_failed_tool_name failed_tool_name TYPE set(1000) GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (timestamp, session_id);

-- One row per model call (usage report). cost/input_cost/output_cost come
-- straight from LiteLLM's own response_cost/cost_breakdown - no local price
-- table needed or wanted: a manually-maintained model_pricing table used to
-- exist for this and was removed after it was found to overcount cost by
-- several times whenever prompt caching was in play, since it priced every
-- input token at full rate with no cache-read/cache-write discount. LiteLLM
-- already prices those tiers correctly internally.
--
-- Same PARTITION BY/ORDER BY reasoning as agent_events above - every
-- token/cost panel filters by time range first, never by session_id alone.
CREATE TABLE IF NOT EXISTS agent_usage
(
    timestamp            DateTime64(3),
    user_id              LowCardinality(String),
    session_id           String,
    trace_id             String,
    turn_id              UInt32,
    model                LowCardinality(String),
    agent_name           LowCardinality(String),
    agent_version        LowCardinality(String),
    skill_name           LowCardinality(String),
    skill_version        LowCardinality(String),
    command_name         LowCardinality(String) DEFAULT '',
    agent_invocation_id  String DEFAULT '',
    mcp_tool_name        LowCardinality(String),
    input_tokens         UInt32,
    output_tokens         UInt32,
    cache_creation_tokens UInt32,
    cache_read_tokens     UInt32,
    -- Why the turn stopped generating (tool_use/end_turn/max_tokens/
    -- refusal) - lets a truncated or refused turn be told apart from a
    -- normal completion.
    stop_reason           LowCardinality(String) DEFAULT '',
    service_tier          LowCardinality(String) DEFAULT '',
    speed                 LowCardinality(String) DEFAULT '',
    -- cache_creation_tokens above stays the sum of these two, for the
    -- existing cost/token panels; 1h vs 5m ephemeral cache writes are
    -- priced differently, hence the separate breakdown.
    cache_creation_1h_tokens UInt32 DEFAULT 0,
    cache_creation_5m_tokens UInt32 DEFAULT 0,
    web_search_requests   UInt32 DEFAULT 0,
    web_fetch_requests    UInt32 DEFAULT 0,
    -- From LiteLLM's own response_cost/cost_breakdown (total/input/output
    -- split) - see the table comment above for why these replaced a local
    -- price table instead of being derived from one.
    cost                  Float64 DEFAULT 0,
    input_cost            Float64 DEFAULT 0,
    output_cost           Float64 DEFAULT 0,
    cache_hit             UInt8 DEFAULT 0,
    -- completionStartTime - startTime, in ms: time to first token, distinct
    -- from the total call latency in agent_events.latency_ms.
    ttft_ms               UInt32 DEFAULT 0,
    INDEX idx_agent_name agent_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_command_name command_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_mcp_tool_name mcp_tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_user_id user_id TYPE set(1000) GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (timestamp, session_id);

-- One row per turn (main session turn or subagent turn), holding the
-- actual prompt sent to the model and the text it replied with. Kept
-- separate from agent_usage/agent_events - this is arbitrary-length free
-- text, not a fixed-width metric or lifecycle event, and most queries
-- (cost, latency, error rate) never need to touch it. Looked up by
-- (session_id, turn_id) from a specific agent_events row via a Grafana
-- data link - see "Full trace" panel and the message/tool-detail panels
-- in the Grafana dashboard section below.
CREATE TABLE IF NOT EXISTS agent_messages
(
    timestamp     DateTime64(3),
    user_id       LowCardinality(String),
    session_id    String,
    trace_id      String,
    turn_id       UInt32,
    agent_name    LowCardinality(String),
    agent_version LowCardinality(String),
    skill_name    LowCardinality(String),
    skill_version LowCardinality(String),
    command_name  LowCardinality(String) DEFAULT '',
    agent_invocation_id String DEFAULT '',
    prompt_text   String CODEC(ZSTD(3)),
    response_text String CODEC(ZSTD(3))
)
ENGINE = MergeTree
-- Unlike agent_events/agent_usage, this table is always looked up by a
-- specific session_id (joined from an agent_events row) rather than
-- scanned by time range, so session_id stays the lead sort key. Half-year
-- partitioning is still worth it here for the same detach-to-archive
-- reason as agent_events above, even though it doesn't change how these
-- particular queries are pruned.
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (session_id, turn_id);
