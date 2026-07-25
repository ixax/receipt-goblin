-- Lowers event_sources.raw_payload_full's compression from ZSTD(19) to
-- ZSTD(3) - see schema.sql's comment on the column for the measured
-- numbers (~17s avg / ~44s max per 500-row insert batch at level 19,
-- vs single-digit ms for every other ingested table). This one column
-- was the entire ingest bottleneck under concurrent load: the Redis
-- queue backed up past its maxlen waiting on it and silently dropped
-- ~85% of events - not a webhook/worker bug, a codec choice that didn't
-- scale to real write throughput.
--
-- MODIFY COLUMN CODEC only governs parts written after this runs - it
-- does not recompress existing parts (ClickHouse has no in-place codec
-- rewrite; that would need an OPTIMIZE ... FINAL or a full
-- recreate+swap, neither necessary just to fix the write path going
-- forward). Auto-applied by clickhouse-migrate on every
-- `docker compose up` (see migrate.py) - no manual steps needed.

ALTER TABLE event_sources MODIFY COLUMN raw_payload_full String CODEC(ZSTD(3));
