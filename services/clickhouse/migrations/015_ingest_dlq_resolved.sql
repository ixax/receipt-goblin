-- ingest_dlq rows are never deleted (append-only forensic log, no TTL
-- anywhere in this codebase). reparse_dlq.py needs a way to page through
-- only the rows it hasn't already successfully replayed - deterministic
-- keyset pagination over occurred_at breaks when many rows share the same
-- millisecond timestamp (true for any batch-inserted outage backlog),
-- causing ClickHouse to re-select the same rows across pages under
-- parallel execution and eventually OOM re-reading raw_row (2026-08-04
-- incident, reparse-dlq smoke test). Insert-only marker table + LEFT ANTI
-- JOIN view instead: once a row's marker lands, it permanently drops out
-- of ingest_dlq_unresolved, so the unresolved set shrinks every page and
-- pagination is guaranteed to terminate. Also serves the "mark resolved,
-- don't delete" requirement, and gives the LiteLLM Alerting dashboard/
-- alert one shared definition of "still needs attention" instead of each
-- panel/alert reimplementing its own time-window heuristic against the
-- raw table.
CREATE TABLE IF NOT EXISTS ingest_dlq_resolved
(
    occurred_at     DateTime64(3),
    stage           LowCardinality(String),
    litellm_call_id String,
    resolved_at     DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY concat(toString(toYear(occurred_at)), '-H', toString(intDiv(toMonth(occurred_at) - 1, 6) + 1))
ORDER BY (occurred_at, stage, litellm_call_id);

CREATE VIEW IF NOT EXISTS ingest_dlq_unresolved AS
SELECT d.*
FROM ingest_dlq AS d
LEFT ANTI JOIN ingest_dlq_resolved AS r
    ON d.occurred_at = r.occurred_at AND d.stage = r.stage AND d.litellm_call_id = r.litellm_call_id;
