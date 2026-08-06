-- Migration for stacks whose ClickHouse volume already existed before
-- subscription billing was modelled.
--
-- Why: agent_usage.cost holds LiteLLM's response_cost - public per-token
-- rates - but essentially no traffic in this stack is actually billed per
-- token. claude-* models run through OAuth passthrough against the
-- caller's own `claude login` (Pro/Max/Team) subscription, and gpt-5.6-*
-- through litellm's `chatgpt` provider against a ChatGPT subscription;
-- both bill a flat monthly fee (see services/litellm/config.yaml's own
-- "cost proxy, not an actual charge" comment). So every cost panel in
-- agents_overview.json has been showing what the traffic *would* have cost
-- on the API, with no way to see the real charge or how the two compare.
--
-- These are two different facts at two different grains and are modelled
-- as such: agent_usage.cost stays exactly as it is (a per-call fact from
-- LiteLLM, untouched), and the flat fee arrives as its own per-period fact.
--
-- Three objects, and the ownership split between them is the point:
--   * person_identities  - config-owned. Maps LiteLLM key identity
--     (agent_usage.user_id) to the human who pays. These are NOT the same
--     thing: user_id is a per-key identity, so one person running Claude
--     Code on two machines has two user_ids but one subscription. Attaching
--     a subscription to user_id would either double-count the fee or leave
--     one machine looking free.
--     Deliberately a separate table rather than a person_id column on
--     ai_gateway_users: that table is ingest-owned (rewritten from LiteLLM
--     metadata on every single call - see services/_common/src/ingest_db.py's
--     _insert_ai_gateway_users), and ingest has no idea who a person is, so
--     under ReplacingMergeTree(updated_at) the next call would immediately
--     overwrite any config-written person_id back to ''.
--   * subscriptions      - config-owned. What is actually paid, with
--     validity intervals (SCD-2). valid_from/valid_to are not optional
--     bookkeeping: plans get upgraded and prices change, and without
--     intervals every historical panel would silently be recomputed at
--     today's price the moment a plan changed.
--   * subscription_cost_daily - the query surface. Expands each interval
--     into one row per day at monthly_price/days-in-that-month. Grafana
--     ranges never line up with billing months, so *something* has to
--     prorate; doing it once here means every panel is a plain
--     SUM(cost) WHERE day BETWEEN ..., additive and consistent, instead of
--     the same proration expression copy-pasted per panel (the exact
--     mistake the provider classifier was moved into ingest to undo - see
--     agent_usage.provider's comment in schema.sql).
--
-- Both tables are populated by `make subscriptions`
-- (services/init/load_subscriptions.py, reading services/init/subscriptions.yml),
-- NOT by ingest and NOT by this file: what you pay is a human-declared
-- fact with no upstream to sync from, unlike ai_gateway_users/clients which
-- are derived from LiteLLM payloads. Keeping it in a reviewed, versioned
-- YAML is what makes it reproducible on a fresh volume.
--
-- Also adds agent_usage.billing_mode, classified at ingest next to
-- provider. Every row is 'subscription' today, which is exactly why it is
-- worth adding now: the moment one model is configured with a real API key,
-- notional and actually-charged costs would otherwise sum into the same
-- column with nothing to tell them apart.
--
-- Run manually, in order:
--   1. Apply this file:
--      docker exec -i receipt-goblin-clickhouse clickhouse-client \
--        --database "$CLICKHOUSE_DATABASE" --multiquery < services/clickhouse/migrations/017_subscriptions.sql
--   2. Deploy the updated webhook/webhook-worker images (billing_mode at
--      ingest).
--   3. `make subscriptions` to load services/init/subscriptions.yml.
--   4. Optionally `make reparse-all` to backfill billing_mode on existing
--      rows; without it they keep the 'unknown' default, which the panels
--      below treat as notional. Purely cosmetic while every model is
--      subscription-billed.
--
-- Safe to re-run: every statement is IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS person_identities
(
    user_id    LowCardinality(String),
    person_id  LowCardinality(String),
    updated_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (user_id);

CREATE TABLE IF NOT EXISTS subscriptions
(
    person_id     LowCardinality(String),
    -- Same domain as agent_usage.provider ('claude'/'openai'/'other') so a
    -- panel can put spend and token usage side by side without a mapping
    -- table in between.
    provider      LowCardinality(String),
    plan          LowCardinality(String),
    -- Decimal, not Float64 like agent_usage's cost columns: this one is a
    -- declared price copied from an invoice, so it should round-trip
    -- exactly. subscription_cost_daily casts it to Float64 on the way out,
    -- where it only ever meets already-float notional costs anyway.
    monthly_price Decimal(12, 2),
    currency      LowCardinality(String) DEFAULT 'USD',
    seats         UInt16 DEFAULT 1,
    valid_from    Date,
    valid_to      Date DEFAULT toDate('2099-12-31'),
    updated_at    DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (person_id, provider, valid_from);

-- One row per (subscription, day) at that day's share of the monthly fee.
--
-- The inner GROUP BY/argMax is the standard dedupe for reading a
-- ReplacingMergeTree without FINAL (same idiom as the dictionaries in
-- services/migrate/src/migrate.py): a re-run of `make subscriptions` inserts a
-- fresh row per subscription rather than mutating one, and only the newest
-- updated_at should win.
--
-- least(valid_to, today()) clamps open-ended subscriptions - valid_to
-- defaults to 2099, and a subscription cannot have been charged for days
-- that have not happened yet. The greatest(..., -1) + 1 around the day
-- count makes a fully future-dated subscription expand to zero rows
-- instead of failing on range() of a negative number.
CREATE VIEW IF NOT EXISTS subscription_cost_daily AS
WITH latest AS
(
    SELECT
        person_id,
        provider,
        valid_from,
        argMax(plan, updated_at)          AS plan,
        argMax(monthly_price, updated_at) AS monthly_price,
        argMax(currency, updated_at)      AS currency,
        argMax(seats, updated_at)         AS seats,
        argMax(valid_to, updated_at)      AS valid_to
    FROM subscriptions
    GROUP BY person_id, provider, valid_from
)
SELECT
    person_id,
    provider,
    plan,
    currency,
    valid_from + toIntervalDay(day_offset) AS day,
    toFloat64(monthly_price) * seats
        / toDayOfMonth(toLastDayOfMonth(valid_from + toIntervalDay(day_offset))) AS cost
FROM latest
ARRAY JOIN range(toUInt32(greatest(toInt64(dateDiff('day', valid_from, least(valid_to, today()))), -1) + 1)) AS day_offset;

ALTER TABLE agent_usage
    ADD COLUMN IF NOT EXISTS billing_mode LowCardinality(String) DEFAULT 'unknown' AFTER provider;
ALTER TABLE agent_usage
    ADD INDEX IF NOT EXISTS idx_billing_mode billing_mode TYPE set(10) GRANULARITY 4;
