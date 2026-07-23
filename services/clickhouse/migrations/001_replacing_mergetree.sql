-- One-time migration for stacks whose ClickHouse volume already existed
-- before agent_events/agent_usage/agent_messages moved to
-- ReplacingMergeTree(ingested_at) + litellm_call_id in ORDER BY, and before
-- agent_events grew calculated_type/calculated_payload (raw_payload
-- dropped, superseded by event_sources.raw_payload_full). NOT run
-- automatically - docker-entrypoint-initdb.d only applies schema.sql to a
-- brand-new (empty) data volume, and ClickHouse has no ALTER for ENGINE or
-- for retroactively re-sorting existing parts under a new ORDER BY (MODIFY
-- ORDER BY only governs parts written after the change - unsafe for
-- ReplacingMergeTree dedup correctness against old parts). The only real
-- path is recreate + atomic rename.
--
-- Run manually, in order, against a stack whose webhook-worker has been
-- paused first (see below) - e.g.:
--   docker exec -i receipt-goblin-clickhouse clickhouse-client \
--     --database "$CLICKHOUSE_DATABASE" --multiquery < services/clickhouse/migrations/001_replacing_mergetree.sql
--
-- 1. Pause webhook-worker's ClickHouse writes before running this file -
--    e.g. `docker compose stop webhook-worker`. The Redis queue buffers
--    incoming events during the window (see services/webhook/src/queue_client.py) -
--    resume webhook-worker once step 7 (RENAME) below has completed for all
--    three tables.
--
-- 2. Rows written before this migration have no litellm_call_id and get a
--    permanent calculated_type = 'unknown' placeholder - there is no source
--    to reparse them from (event_sources only starts accumulating once the
--    ingest code that populates it is deployed, and .capture/*.json is
--    explicitly out of scope as a parsing/backfill input, now and always -
--    see event_sources's comment in schema.sql). This is an accepted,
--    permanent gap for pre-migration data, not a defect to fix later.

-- ---------------------------------------------------------------------
-- event_sources (brand new table, no prior counterpart - unlike the three
-- tables below, this needs no recreate+swap, just a plain CREATE - but
-- docker-entrypoint-initdb.d only applies schema.sql to a brand-new volume,
-- so an existing stack never gets this table unless it's created here too.
-- IF NOT EXISTS makes this safe to run even if it somehow already exists.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_sources
(
    litellm_call_id  String,
    session_id       String,
    ingested_at      DateTime64(3) DEFAULT now64(3),
    raw_payload_full String CODEC(ZSTD(19)),
    INDEX idx_session_id session_id TYPE set(1000) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY concat(toString(toYear(ingested_at)), '-H', toString(intDiv(toMonth(ingested_at) - 1, 6) + 1))
ORDER BY (litellm_call_id);

-- ---------------------------------------------------------------------
-- agent_events
-- ---------------------------------------------------------------------
CREATE TABLE agent_events_new
(
    timestamp            DateTime64(3),
    user_id              LowCardinality(String),
    session_id           String,
    trace_id             String,
    turn_id              UInt32,
    litellm_call_id      String DEFAULT '',
    event_type           LowCardinality(String),
    tool_name            LowCardinality(String),
    agent_name           LowCardinality(String),
    agent_version        LowCardinality(String),
    skill_name           LowCardinality(String),
    skill_version        LowCardinality(String),
    command_name         LowCardinality(String) DEFAULT '',
    command_version      LowCardinality(String) DEFAULT '',
    agent_invocation_id  String DEFAULT '',
    status               LowCardinality(String),
    latency_ms           Nullable(UInt32),
    failed_tool_name     LowCardinality(String) DEFAULT '',
    failed_tool_args     String DEFAULT '',
    failed_tool_error    String DEFAULT '',
    calculated_type      LowCardinality(String) DEFAULT 'unknown',
    calculated_payload   String DEFAULT '{}' CODEC(ZSTD(3)),
    ingested_at          DateTime64(3) DEFAULT now64(3),
    INDEX idx_tool_name tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_agent_name agent_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_command_name command_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_user_id user_id TYPE set(1000) GRANULARITY 4,
    INDEX idx_failed_tool_name failed_tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_calculated_type calculated_type TYPE set(100) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (timestamp, session_id, litellm_call_id);

INSERT INTO agent_events_new
SELECT
    timestamp, user_id, session_id, trace_id, turn_id,
    '' AS litellm_call_id,
    event_type, tool_name, agent_name, agent_version, skill_name, skill_version,
    command_name, command_version, agent_invocation_id, status, latency_ms,
    failed_tool_name, failed_tool_args, failed_tool_error,
    'unknown' AS calculated_type, '{}' AS calculated_payload,
    now64(3) AS ingested_at
FROM agent_events;

RENAME TABLE agent_events TO agent_events_old, agent_events_new TO agent_events;

-- ---------------------------------------------------------------------
-- agent_usage
-- ---------------------------------------------------------------------
CREATE TABLE agent_usage_new
(
    timestamp             DateTime64(3),
    user_id               LowCardinality(String),
    session_id            String,
    trace_id              String,
    turn_id               UInt32,
    litellm_call_id       String DEFAULT '',
    model                 LowCardinality(String),
    provider              LowCardinality(String) DEFAULT '',
    agent_name            LowCardinality(String),
    agent_version         LowCardinality(String),
    skill_name            LowCardinality(String),
    skill_version         LowCardinality(String),
    command_name          LowCardinality(String) DEFAULT '',
    command_version       LowCardinality(String) DEFAULT '',
    agent_invocation_id   String DEFAULT '',
    mcp_tool_name         LowCardinality(String),
    input_tokens          UInt32,
    output_tokens         UInt32,
    cache_creation_tokens UInt32,
    cache_read_tokens     UInt32,
    stop_reason           LowCardinality(String) DEFAULT '',
    cache_creation_1h_tokens UInt32 DEFAULT 0,
    cache_creation_5m_tokens UInt32 DEFAULT 0,
    cost                  Float64 DEFAULT 0,
    input_cost            Float64 DEFAULT 0,
    output_cost           Float64 DEFAULT 0,
    cache_hit             UInt8 DEFAULT 0,
    ttft_ms               UInt32 DEFAULT 0,
    ingested_at           DateTime64(3) DEFAULT now64(3),
    INDEX idx_agent_name agent_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_command_name command_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_mcp_tool_name mcp_tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_user_id user_id TYPE set(1000) GRANULARITY 4,
    INDEX idx_provider provider TYPE set(10) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (timestamp, session_id, litellm_call_id);

INSERT INTO agent_usage_new
SELECT
    timestamp, user_id, session_id, trace_id, turn_id,
    '' AS litellm_call_id,
    model,
    multiIf(
        model LIKE 'claude-%', 'claude',
        match(model, '^(gpt-|chatgpt-|o[0-9]|text-embedding-|dall-e-|whisper|tts-)'), 'openai',
        'other'
    ) AS provider,
    agent_name, agent_version, skill_name, skill_version,
    command_name, command_version, agent_invocation_id, mcp_tool_name,
    input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
    stop_reason, cache_creation_1h_tokens, cache_creation_5m_tokens,
    cost, input_cost, output_cost, cache_hit, ttft_ms,
    now64(3) AS ingested_at
FROM agent_usage;

RENAME TABLE agent_usage TO agent_usage_old, agent_usage_new TO agent_usage;

-- ---------------------------------------------------------------------
-- agent_messages
-- ---------------------------------------------------------------------
CREATE TABLE agent_messages_new
(
    timestamp       DateTime64(3),
    user_id         LowCardinality(String),
    session_id      String,
    trace_id        String,
    turn_id         UInt32,
    litellm_call_id String DEFAULT '',
    agent_name      LowCardinality(String),
    agent_version   LowCardinality(String),
    skill_name      LowCardinality(String),
    skill_version   LowCardinality(String),
    command_name    LowCardinality(String) DEFAULT '',
    command_version LowCardinality(String) DEFAULT '',
    agent_invocation_id String DEFAULT '',
    prompt_text     String CODEC(ZSTD(3)),
    response_text   String CODEC(ZSTD(3)),
    ingested_at     DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (session_id, litellm_call_id);

INSERT INTO agent_messages_new
SELECT
    timestamp, user_id, session_id, trace_id, turn_id,
    '' AS litellm_call_id,
    agent_name, agent_version, skill_name, skill_version,
    command_name, command_version, agent_invocation_id,
    prompt_text, response_text,
    now64(3) AS ingested_at
FROM agent_messages;

RENAME TABLE agent_messages TO agent_messages_old, agent_messages_new TO agent_messages;

-- ---------------------------------------------------------------------
-- Resume webhook-worker now (e.g. `docker compose start webhook-worker`),
-- then once event_sources has accumulated enough history, run
-- `make reparse-all` (or `make reparse SESSION=<id>` per session) to fill
-- in real calculated_type/calculated_payload/provider for rows that have an
-- event_sources counterpart - see webhook/src/reparse.py.
--
-- Force the dedup merge immediately, since most dashboard queries don't use
-- FINAL (for performance) and would otherwise see duplicate old+new rows
-- until a background merge happens naturally:
--   OPTIMIZE TABLE agent_events FINAL;
--   OPTIMIZE TABLE agent_usage FINAL;
--   OPTIMIZE TABLE agent_messages FINAL;
--
-- Once confirmed correct, drop the _old tables:
--   DROP TABLE agent_events_old;
--   DROP TABLE agent_usage_old;
--   DROP TABLE agent_messages_old;
