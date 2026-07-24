-- Migration for stacks whose ClickHouse volume already existed before
-- user_key_hash/user_agent were introduced.
--
-- Why: LiteLLM distinguishes "internal users" (metadata.user_api_key_user_id,
-- already stored as agent_events/agent_usage/agent_messages.user_id) from
-- "virtual keys" (metadata.user_api_key_hash) - one internal user can hold
-- any number of keys. user_key_hash adds that per-call key identity
-- alongside user_id (additive, not a replacement - see _user_key_hash in
-- clickhouse_ingest.py). user_agent (metadata.user_agent, e.g.
-- "claude-cli/2.1.207") records which client each internal user calls from,
-- stored as the latest-seen value per user in ai_gateway_users (see
-- _user_agent/_user_row).
--
-- Run manually, in order:
--   1. Apply this file:
--      docker exec -i receipt-goblin-clickhouse clickhouse-client \
--        --database "$CLICKHOUSE_DATABASE" --multiquery < services/clickhouse/migrations/003_user_key_hash_and_agent.sql
--   2. Deploy the updated webhook/webhook-worker images (new
--      _user_key_hash/_user_agent ingestion logic).
--   3. Run `make reparse-all` to backfill user_key_hash on every
--      event_sources-backed fact-table row and user_agent into
--      ai_gateway_users from that same history. Rows ingested before
--      event_sources existed have nothing to reparse from and permanently
--      keep the empty default - same accepted gap as the 002 migration's
--      group_alias/user_id backfill.
--   4. `OPTIMIZE TABLE agent_events FINAL`, `OPTIMIZE TABLE agent_usage
--      FINAL`, `OPTIMIZE TABLE agent_messages FINAL`,
--      `OPTIMIZE TABLE ai_gateway_users FINAL` - forces the dedup merge
--      immediately so dashboard queries (which don't use FINAL) see
--      corrected data right away instead of waiting for a background merge.
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS is a no-op on a second run.

ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS user_key_hash LowCardinality(String) DEFAULT '';
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS user_key_hash LowCardinality(String) DEFAULT '';
ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS user_key_hash LowCardinality(String) DEFAULT '';
ALTER TABLE ai_gateway_users ADD COLUMN IF NOT EXISTS user_agent LowCardinality(String) DEFAULT '';
