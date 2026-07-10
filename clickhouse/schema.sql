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

-- One row per lifecycle event (hook invocation). raw_payload keeps the full
-- untouched JSON Claude Code sent, so any field missed by the extracted
-- columns can still be recovered later.
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
    status            LowCardinality(String),
    latency_ms        Nullable(UInt32),
    raw_payload       String
)
ENGINE = MergeTree
ORDER BY (session_id, timestamp);

-- One row per model call (usage report). Cost is intentionally not stored
-- here - it is derived at query time via ASOF JOIN against model_pricing,
-- so historical price changes never distort past cost figures.
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
    web_fetch_requests    UInt32 DEFAULT 0
)
ENGINE = MergeTree
ORDER BY (session_id, timestamp);

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
    prompt_text   String,
    response_text String
)
ENGINE = MergeTree
ORDER BY (session_id, turn_id);

-- Filled manually, never hardcoded in application code. A new price change
-- is a new row with a new effective_from - old rows are kept so historical
-- usage still costs out correctly via ASOF JOIN.
CREATE TABLE IF NOT EXISTS model_pricing
(
    model               LowCardinality(String),
    effective_from      DateTime,
    price_in_per_mtok   Float64,
    price_out_per_mtok  Float64
)
ENGINE = MergeTree
ORDER BY (model, effective_from);

-- Default pricing snapshot (per Claude API list pricing, cached 2026-06-24).
-- Adding a price change later means INSERTing a new row with a new
-- effective_from, never editing these - see README section "Updating model
-- pricing without losing history".
--
-- One INSERT per row: ClickHouse's Values-format parser (used for fast
-- bulk INSERT) does not support comments between tuples inside a single
-- VALUES list, only between statements.
INSERT INTO model_pricing (model, effective_from, price_in_per_mtok, price_out_per_mtok) VALUES
    ('claude-fable-5', '2026-01-01 00:00:00', 10.00, 50.00);
INSERT INTO model_pricing (model, effective_from, price_in_per_mtok, price_out_per_mtok) VALUES
    ('claude-opus-4-8', '2026-01-01 00:00:00', 5.00, 25.00);
INSERT INTO model_pricing (model, effective_from, price_in_per_mtok, price_out_per_mtok) VALUES
    ('claude-haiku-4-5', '2026-01-01 00:00:00', 1.00, 5.00);
-- Sonnet 5 introductory pricing, in effect through 2026-08-31.
INSERT INTO model_pricing (model, effective_from, price_in_per_mtok, price_out_per_mtok) VALUES
    ('claude-sonnet-5', '2026-01-01 00:00:00', 2.00, 10.00);
-- Sonnet 5 standard pricing, effective 2026-09-01 once the intro period ends.
INSERT INTO model_pricing (model, effective_from, price_in_per_mtok, price_out_per_mtok) VALUES
    ('claude-sonnet-5', '2026-09-01 00:00:00', 3.00, 15.00);
