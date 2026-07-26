-- Moves ingest_dlq off its old 30-day TTL onto the half-year PARTITION BY
-- convention used by agent_events/agent_usage/agent_messages/ingest_raw -
-- see AGENTS.md's "no TTL-based deletion, ever" rule. ClickHouse has no
-- ALTER for a table's partition key (same restriction as ORDER BY/ENGINE -
-- see 001_replacing_mergetree.sql's comment), so this is a recreate +
-- atomic rename, same pattern as that migration.
--
-- ingest_dlq is a low-volume triage feed (only written to when a row fails
-- its own table's insert), so unlike 001 this doesn't need webhook-worker
-- paused first - the brief window between the INSERT...SELECT snapshot and
-- the RENAME could in principle drop a row written in between, which is an
-- acceptable risk for this table (its whole point is best-effort triage,
-- not being a source of truth).
--
-- Auto-applied by clickhouse-migrate on every `docker compose up` (see
-- migrate.py) - the _ingest_dlq_already_partitioned SKIP_CHECKS guard means
-- a fresh volume (already matching this shape via schema.sql) just records
-- this as applied without running the SQL below.

CREATE TABLE ingest_dlq_new
(
    occurred_at     DateTime64(3) DEFAULT now64(3),
    stage           LowCardinality(String),
    error           String,
    litellm_call_id String DEFAULT '',
    session_id      String DEFAULT '',
    raw_row         String DEFAULT '' CODEC(ZSTD(3))
)
ENGINE = MergeTree
PARTITION BY concat(toString(toYear(occurred_at)), '-H', toString(intDiv(toMonth(occurred_at) - 1, 6) + 1))
ORDER BY (occurred_at);

INSERT INTO ingest_dlq_new
SELECT occurred_at, stage, error, litellm_call_id, session_id, raw_row
FROM ingest_dlq;

RENAME TABLE ingest_dlq TO ingest_dlq_old, ingest_dlq_new TO ingest_dlq;

-- Once confirmed correct (row counts match, dashboard/triage queries still
-- work), drop the old table by hand:
--   DROP TABLE ingest_dlq_old;
