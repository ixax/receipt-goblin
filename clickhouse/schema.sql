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
CREATE TABLE IF NOT EXISTS agent_invocations
(
    agent_id      String,
    session_id    String,
    subagent_type String,
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
-- Monthly partitions let ClickHouse skip whole months outside the queried
-- range instead of scanning the entire table. Skip indexes below accelerate
-- the has([...], col)/!= ''/startsWith(...) filters used almost everywhere.
CREATE TABLE IF NOT EXISTS agent_events
(
    timestamp         DateTime64(3),
    user_id           String,
    session_id        String,
    trace_id          String,
    parent_session_id String,
    turn_id           UInt32,
    sequence_id        UInt32,
    event_type        LowCardinality(String),
    tool_name         String,
    agent_name        String,
    agent_version     String,
    skill_name        String,
    skill_version     String,
    -- Slash command that kicked off the current chain of calls (e.g.
    -- "whatsup"), recovered from the "<command-name>" tag Claude Code
    -- injects into the triggering user message - see
    -- webhook/src/clickhouse_ingest.py:_active_command_name. Deliberately
    -- has no version column: commands are meant to stay a stable,
    -- version-independent entry point even when the skill/logic behind
    -- them is renamed on every version bump.
    command_name      String DEFAULT '',
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
    failed_tool_name  String DEFAULT '',
    failed_tool_args  String DEFAULT '',
    failed_tool_error String DEFAULT '',
    raw_payload       String,
    INDEX idx_tool_name tool_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_agent_name agent_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_command_name command_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_user_id user_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_failed_tool_name failed_tool_name TYPE bloom_filter GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
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
    user_id              String,
    session_id           String,
    trace_id             String,
    turn_id              UInt32,
    model                LowCardinality(String),
    agent_name           String,
    agent_version        String,
    skill_name           String,
    skill_version        String,
    command_name         String DEFAULT '',
    agent_invocation_id  String DEFAULT '',
    mcp_tool_name        String,
    input_tokens         UInt32,
    output_tokens        UInt32,
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
    INDEX idx_agent_name agent_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_command_name command_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_mcp_tool_name mcp_tool_name TYPE bloom_filter GRANULARITY 4,
    INDEX idx_user_id user_id TYPE bloom_filter GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(timestamp)
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
    user_id       String,
    session_id    String,
    trace_id      String,
    turn_id       UInt32,
    agent_name    String,
    agent_version String,
    skill_name    String,
    skill_version String,
    command_name  String DEFAULT '',
    agent_invocation_id String DEFAULT '',
    prompt_text   String,
    response_text String
)
ENGINE = MergeTree
-- Unlike agent_events/agent_usage, this table is always looked up by a
-- specific session_id (joined from an agent_events row) rather than
-- scanned by time range, so session_id stays the lead sort key. Monthly
-- partitioning is still worth it for data lifecycle (TTL/drop old months)
-- even though it doesn't change how these particular queries are pruned.
PARTITION BY toYYYYMM(timestamp)
ORDER BY (session_id, turn_id);
