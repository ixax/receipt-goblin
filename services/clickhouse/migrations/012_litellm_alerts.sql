-- LiteLLM's native alerting webhook (general_settings.alerting: ["webhook"]
-- in services/litellm/config.yaml) reports signals our generic_api
-- StandardLoggingPayload callback can't reconstruct: budget/spend threshold
-- crossings, deployment outages, DB exceptions, hung/too-slow requests
-- flagged by LiteLLM's own internal logic. This table stores those events,
-- fed by services/webhook/src/clickhouse_ingest.py's ingest_litellm_alert(),
-- called from server.py's new POST /api/v1/litellm-alert route.
--
-- Modeled on ingest_dlq's shape: raw-payload-preserving (only the
-- budget-event shape is fully documented by LiteLLM's own docs -
-- llm_exceptions/outage_alerts/db_exceptions payloads likely carry
-- different fields, so raw_payload keeps the full body until real payloads
-- are captured and first-class columns can be added for what's actually
-- observed), no TTL (see AGENTS.md's "no TTL-based deletion, ever" rule),
-- half-year PARTITION BY convention.
CREATE TABLE IF NOT EXISTS litellm_alerts
(
    received_at   DateTime64(3) DEFAULT now64(3),
    event         LowCardinality(String) DEFAULT '',       -- e.g. budget_crossed, threshold_crossed, llm_exceptions
    event_group   LowCardinality(String) DEFAULT '',       -- customer/key/team/proxy/internal_user, when present
    key_alias     String DEFAULT '',
    team_id       String DEFAULT '',
    user_id       String DEFAULT '',
    spend         Nullable(Float64),
    max_budget    Nullable(Float64),
    event_message String DEFAULT '',
    raw_payload   String DEFAULT '' CODEC(ZSTD(3))         -- full JSON, since alert shape varies by type
)
ENGINE = MergeTree
PARTITION BY concat(toString(toYear(received_at)), '-H', toString(intDiv(toMonth(received_at) - 1, 6) + 1))
ORDER BY (received_at);
