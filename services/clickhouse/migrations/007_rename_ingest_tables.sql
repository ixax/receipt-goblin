-- Renames event_sources -> ingest_raw and ingest_failures -> ingest_dlq
-- for stacks whose ClickHouse volume already has the old names - see
-- schema.sql, which already defines the tables under their new names for
-- fresh installs. Pure rename, no data movement - a RENAME TABLE is a
-- metadata-only operation in ClickHouse, safe to run against a live table.
-- Auto-applied by clickhouse-migrate on every `docker compose up` (see
-- migrate.py) - no manual steps needed.

RENAME TABLE event_sources TO ingest_raw, ingest_failures TO ingest_dlq;
