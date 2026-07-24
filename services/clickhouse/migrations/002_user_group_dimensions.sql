-- Migration for stacks whose ClickHouse volume already existed before
-- ai_gateway_users/ai_gateway_groups were introduced.
--
-- Why: agent_events.user_id used to hold metadata.user_api_key_alias (a
-- human-editable LiteLLM key alias, renamable at any time) instead of the
-- real, stable metadata.user_api_key_user_id LiteLLM actually sends -
-- meaning a rename silently changed the "identity" every fact table joined
-- on. group_id was already the real stable id, but its display name
-- (group_alias) was duplicated onto every agent_events row instead of
-- living in one place. This migration adds the two dimension tables (see
-- their comments in schema.sql), drops the now-redundant group_alias
-- column, and backfills both tables plus corrected user_id values from
-- event_sources.raw_payload_full via `make reparse-all`
-- (services/webhook/src/reparse.py) - NOT via SQL here, since the real ids/
-- names only exist inside each row's original LiteLLM payload, not
-- anywhere queryable in agent_events itself.
--
-- Run manually, in order:
--   1. Apply this file:
--      docker exec -i receipt-goblin-clickhouse clickhouse-client \
--        --database "$CLICKHOUSE_DATABASE" --multiquery < services/clickhouse/migrations/002_user_group_dimensions.sql
--   2. Deploy the updated webhook/webhook-worker images (real user_id +
--      dimension-row ingestion logic).
--   3. Run `make reparse-all` to rewrite every event_sources-backed row
--      with the real user_id and populate ai_gateway_users/ai_gateway_groups
--      from history. Rows ingested before event_sources existed have
--      nothing to reparse from and permanently keep whatever alias they
--      already hold - same accepted gap as calculated_type='unknown' rows
--      (see schema.sql's event_sources comment).
--   4. `OPTIMIZE TABLE agent_events FINAL`, `OPTIMIZE TABLE agent_usage
--      FINAL`, `OPTIMIZE TABLE agent_messages FINAL`,
--      `OPTIMIZE TABLE ai_gateway_users FINAL`,
--      `OPTIMIZE TABLE ai_gateway_groups FINAL` - forces the dedup merge
--      immediately so dashboard queries (which don't use FINAL) see
--      corrected data right away instead of waiting for a background merge.
--
-- Safe to re-run: CREATE ... IF NOT EXISTS and DROP COLUMN IF EXISTS are
-- both no-ops on a second run.

CREATE TABLE IF NOT EXISTS ai_gateway_groups
(
    group_id   LowCardinality(String),
    group_name LowCardinality(String),
    updated_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (group_id);

CREATE TABLE IF NOT EXISTS ai_gateway_users
(
    user_id    LowCardinality(String),
    group_id   LowCardinality(String) DEFAULT '',
    user_name  LowCardinality(String),
    updated_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (user_id);

ALTER TABLE agent_events DROP COLUMN IF EXISTS group_alias;
