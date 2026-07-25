-- Adds ingest_failures for stacks whose ClickHouse volume already existed
-- before it was introduced - see schema.sql's comment on the table itself
-- for what it's for. Auto-applied by clickhouse-migrate on every
-- `docker compose up` (see migrate.py) - no manual steps needed.
--
-- IF NOT EXISTS makes this safe to re-run (also covers a volume that's
-- brand new and already got the table from schema.sql directly).

CREATE TABLE IF NOT EXISTS ingest_failures
(
    occurred_at     DateTime64(3) DEFAULT now64(3),
    stage           LowCardinality(String),
    error           String,
    litellm_call_id String DEFAULT '',
    session_id      String DEFAULT '',
    raw_row         String DEFAULT '' CODEC(ZSTD(3))
)
ENGINE = MergeTree
ORDER BY (occurred_at)
TTL toDateTime(occurred_at) + INTERVAL 30 DAY;
