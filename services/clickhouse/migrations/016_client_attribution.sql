-- Token and cost facts currently know the model provider but not which
-- product surface initiated the call. Grafana therefore cannot distinguish
-- CLI, Desktop, and Remote Control without joining events or parsing a raw
-- user-agent in every panel.

ALTER TABLE agent_events
    ADD COLUMN IF NOT EXISTS client_product LowCardinality(String) DEFAULT 'unknown' AFTER event_client_id;
ALTER TABLE agent_events
    ADD COLUMN IF NOT EXISTS client_surface LowCardinality(String) DEFAULT 'unknown' AFTER client_product;
ALTER TABLE agent_events
    ADD COLUMN IF NOT EXISTS ingest_path LowCardinality(String) DEFAULT 'unknown' AFTER client_surface;
ALTER TABLE agent_events
    ADD INDEX IF NOT EXISTS idx_client_product client_product TYPE set(10) GRANULARITY 4;
ALTER TABLE agent_events
    ADD INDEX IF NOT EXISTS idx_client_surface client_surface TYPE set(10) GRANULARITY 4;
ALTER TABLE agent_events
    ADD INDEX IF NOT EXISTS idx_ingest_path ingest_path TYPE set(10) GRANULARITY 4;

ALTER TABLE agent_usage
    ADD COLUMN IF NOT EXISTS client_id UInt64 DEFAULT 0 AFTER litellm_call_id;
ALTER TABLE agent_usage
    ADD COLUMN IF NOT EXISTS client_product LowCardinality(String) DEFAULT 'unknown' AFTER client_id;
ALTER TABLE agent_usage
    ADD COLUMN IF NOT EXISTS client_surface LowCardinality(String) DEFAULT 'unknown' AFTER client_product;
ALTER TABLE agent_usage
    ADD COLUMN IF NOT EXISTS ingest_path LowCardinality(String) DEFAULT 'unknown' AFTER client_surface;
ALTER TABLE agent_usage
    ADD INDEX IF NOT EXISTS idx_client_id client_id TYPE set(50) GRANULARITY 4;
ALTER TABLE agent_usage
    ADD INDEX IF NOT EXISTS idx_client_product client_product TYPE set(10) GRANULARITY 4;
ALTER TABLE agent_usage
    ADD INDEX IF NOT EXISTS idx_client_surface client_surface TYPE set(10) GRANULARITY 4;
ALTER TABLE agent_usage
    ADD INDEX IF NOT EXISTS idx_ingest_path ingest_path TYPE set(10) GRANULARITY 4;
