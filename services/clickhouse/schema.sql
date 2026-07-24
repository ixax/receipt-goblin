-- Schema for the local AI agent cost/efficiency tracking stack.
-- Applies automatically on first container start via
-- docker-entrypoint-initdb.d (see docker-compose.yml). To reapply manually
-- (e.g. after the volume already exists): docker exec -i receipt-goblin-clickhouse
-- clickhouse-client --multiquery < clickhouse/schema.sql

-- agent_id -> subagent_type lookup, recovered from the orchestrator's own
-- LiteLLM call: an Agent tool_use block paired with the tool_result that
-- follows it (containing "agentId: <hex>") tells us what a given agent_id
-- actually is. Populated by webhook/src/clickhouse_ingest.py before it
-- writes any row whose agent_invocation_id needs resolving - see AGENTS.md.
-- One row per subagent spawn, so this table stays tiny - agent_id is left
-- as a plain String (not worth a tighter type for a table this small).
CREATE TABLE IF NOT EXISTS agent_invocations
(
    agent_id      String,
    session_id    String,
    subagent_type LowCardinality(String),
    -- From a "<agent_version>...</agent_version>" marker as the first thing
    -- in the agent's own description: frontmatter, which Claude Code
    -- re-injects into every call's messages via the "Available agent types"
    -- listing - see clickhouse_ingest.py:_agent_invocations_from_messages.
    -- Blank for a self-named/ad-hoc agent (no backing .md file, no marker)
    -- or an agent never edited since creation.
    agent_version LowCardinality(String) DEFAULT '',
    description   String,
    spawned_at    DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(spawned_at)
ORDER BY (agent_id);

-- Table predates agent_version: ALTER for stacks whose ClickHouse volume
-- already existed before this column was added.
ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS agent_version LowCardinality(String) DEFAULT '';

-- session_id -> git branch/repo, captured once at SessionStart by
-- hooks/report_git_branch.py (Claude Code and Codex CLI both run it - see
-- .claude/settings.json / .codex/hooks.json). This is the one lifecycle
-- hook this stack still has: neither LiteLLM's StandardLoggingPayload nor
-- ANTHROPIC_CUSTOM_HEADERS (a static env var) can carry the client's cwd/
-- git state, which is otherwise invisible to webhook/src/clickhouse_ingest.py
-- - see AGENTS.md for why every other hook was removed in favor of that
-- payload. Branch/repo are a snapshot from session start, not live - a
-- mid-session `git checkout` or directory change won't update the row.
-- git_repo is the repo's directory basename (falls back to the remote
-- "origin" URL's basename when set - see _current_repo in
-- hooks/report_git_branch.py), so it stays stable across clones under
-- different local folder names.
CREATE TABLE IF NOT EXISTS session_git_branch
(
    session_id  String,
    git_branch  String,
    git_repo    String DEFAULT '',
    captured_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(captured_at)
ORDER BY (session_id);

-- Table predates git_repo: ALTER for stacks whose ClickHouse volume already
-- existed before this column was added (docker-entrypoint-initdb.d only
-- runs CREATE TABLE on a fresh volume - see the reapply note up top).
ALTER TABLE session_git_branch ADD COLUMN IF NOT EXISTS git_repo String DEFAULT '';

-- One row per ExitPlanMode tool call, captured by hooks/report_plan_proposal.py
-- at PreToolUse (Claude Code only - Codex CLI has no plan-mode equivalent).
-- This exists because the plan text isn't recoverable from LiteLLM's
-- StandardLoggingPayload: agent_events.raw_payload's
-- response.choices[0].message.tool_calls[0].function.arguments comes back
-- as an empty "{}" for every observed ExitPlanMode call (confirmed against
-- live data - unlike every other tool, whose arguments are captured in
-- full), so the plan has to be read straight from the hook's own tool_input
-- instead. A session can propose more than one plan (revise-and-resubmit),
-- so this is plain insert-only, not a ReplacingMergeTree keyed on
-- session_id. Paired back to its triggering agent_events row (tool_name =
-- 'ExitPlanMode') via an ASOF JOIN on session_id + nearest captured_at at
-- or after that row's timestamp - see the "ExitPlanMode: user prompt ->
-- proposed plan" panel in agents_overview.json.
CREATE TABLE IF NOT EXISTS plan_proposals
(
    session_id  String,
    plan_text   String CODEC(ZSTD(3)),
    captured_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (session_id, captured_at);

-- group_id -> group_name lookup (LiteLLM Team id -> its current display
-- name). Exists so agent_events/agent_usage/agent_messages only ever need
-- to carry group_id (the stable, non-renamable key) - any panel that wants
-- a readable name joins here instead of the old group_alias column that
-- used to be duplicated onto every agent_events row. One row per team, kept
-- tiny with LowCardinality(String) columns, same reasoning as
-- agent_invocations. ReplacingMergeTree(updated_at): latest name wins if a
-- team gets renamed in the LiteLLM UI, without rewriting historical fact
-- rows. Populated by webhook/src/clickhouse_ingest.py
-- (_group_row/_insert_ai_gateway_dims) on every ingest, and backfilled from
-- event_sources by webhook/src/reparse.py - see AGENTS.md.
CREATE TABLE IF NOT EXISTS ai_gateway_groups
(
    group_id   LowCardinality(String),
    group_name LowCardinality(String),
    updated_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (group_id);

-- user_id -> (group_id, user_name) lookup. user_id here is the real,
-- stable id LiteLLM sends as metadata.user_api_key_user_id - not the
-- renamable metadata.user_api_key_alias that agent_events/agent_usage/
-- agent_messages.user_id used to hold before this table existed (see
-- clickhouse_ingest.py:_user_id). group_id is carried here too so a panel
-- can resolve "which group does this user belong to" without a second
-- join back through agent_events. Same ReplacingMergeTree(updated_at)
-- latest-wins semantics as ai_gateway_groups above.
CREATE TABLE IF NOT EXISTS ai_gateway_users
(
    user_id    LowCardinality(String),
    group_id   LowCardinality(String) DEFAULT '',
    user_name  LowCardinality(String),
    -- Latest-seen calling client (metadata.user_agent, e.g.
    -- "claude-cli/2.1.207 (external, cli)") for this user - same latest-wins
    -- semantics as user_name, populated by
    -- clickhouse_ingest.py:_user_agent/_user_row.
    user_agent LowCardinality(String) DEFAULT '',
    updated_at DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (user_id);

-- Table predates user_agent: ALTER for stacks whose ClickHouse volume
-- already existed before this column was added.
ALTER TABLE ai_gateway_users ADD COLUMN IF NOT EXISTS user_agent LowCardinality(String) DEFAULT '';

-- One row per lifecycle event (hook invocation). raw_payload keeps the full
-- untouched JSON Claude Code sent, so any field missed by the extracted
-- columns can still be recovered later.
--
-- PARTITION BY/ORDER BY: nearly every dashboard query filters by a time
-- range first (across many sessions), and only two panels (Full trace, Call
-- stack) filter by a specific session_id - so timestamp leads the sort key
-- for partition/granule pruning on the common case, with session_id second
-- for the two session-scoped panels (cheap once already time/partition-
-- pruned, since one session's rows cluster in a narrow time window anyway).
--
-- Half-year partitions ("2026-H1"/"2026-H2") are sized for archival, not
-- query pruning: once a half stops being actively queried, detach and ship
-- it off (ALTER TABLE agent_events DETACH PARTITION '2026-H1' moves it to
-- `detached/` untouched by future queries/merges - copy it out for backup,
-- ATTACH it back if you ever need to query that period again). No TTL/
-- DELETE - data is kept forever unless you detach it yourself.
--
-- session_id/trace_id stay String rather than UUID: they come from three
-- different sources depending on what's available at ingest time
-- (x-claude-code-session-id header, trace_id, litellm_call_id, or "" as a
-- last resort - see _session_and_trace_id in clickhouse_ingest.py), so
-- guaranteeing well-formed UUIDs on every path isn't free, and these are a
-- small fraction of a row's bytes next to calculated_payload anyway.
--
-- agent_name/skill_name/command_name/tool_name/etc. are LowCardinality:
-- the actual set of distinct values is bounded (the agents/skills/tools
-- registered in this repo), so dictionary encoding shrinks storage and
-- speeds up every filter/GROUP BY/JOIN on them - the set(...) skip indexes
-- below replace the old bloom_filter ones for the same reason (bloom_filter
-- is for genuinely high-cardinality columns; set is cheaper and exact for a
-- small bounded set of values).
--
-- ReplacingMergeTree(ingested_at): lets a later reparse
-- (webhook/src/reparse.py, run via `make reparse`/`make reparse-all`)
-- rewrite calculated_type/calculated_payload for an already-ingested row by
-- inserting a fresh copy with a newer ingested_at - the newest one wins on
-- merge/OPTIMIZE FINAL. This is why litellm_call_id (confirmed unique per
-- call) had to join the ORDER BY: the old (timestamp, session_id) key isn't
-- actually unique (millisecond-truncated timestamps can collide across
-- concurrent calls), so ReplacingMergeTree on that key alone would silently
-- drop real rows, not just reparse duplicates.
CREATE TABLE IF NOT EXISTS agent_events
(
    timestamp         DateTime64(3),
    user_id           LowCardinality(String),
    -- The LiteLLM Team a virtual key belongs to, captured independently of
    -- user_id - see _group_id in webhook/src/clickhouse_ingest.py
    -- for why this is its own column rather than reusing whatever user_id
    -- collapsed into. Empty until LiteLLM Teams are actually configured (see
    -- README "LiteLLM" - "Once it's needed: Teams..."), which is an
    -- operator/admin action, not something this ingestion code can do on
    -- its own.
    --
    -- group_id (metadata.user_api_key_team_id) is the stable filter/join
    -- key - a UUID that survives a team rename in the LiteLLM UI. The
    -- renamable display name used to be duplicated here as group_alias;
    -- that's gone now - join ai_gateway_groups on group_id for a name
    -- instead (see that table's comment above).
    group_id          LowCardinality(String) DEFAULT '',
    -- Which LiteLLM virtual key made this call (metadata.user_api_key_hash) -
    -- distinct from user_id above: LiteLLM's "internal users" and "virtual
    -- keys" are separate concepts, and one internal user can hold any number
    -- of keys - see _user_key_hash in webhook/src/clickhouse_ingest.py.
    user_key_hash     LowCardinality(String) DEFAULT '',
    session_id        String,
    trace_id          String,
    turn_id           UInt32,
    -- Unique per LiteLLM call (confirmed) - the real identity of a row, and
    -- the join key event_sources.litellm_call_id uses to pair a row back to
    -- its full original payload.
    litellm_call_id   String DEFAULT '',
    event_type        LowCardinality(String),
    tool_name         LowCardinality(String),
    agent_name        LowCardinality(String),
    agent_version     LowCardinality(String),
    skill_name        LowCardinality(String),
    skill_version     LowCardinality(String),
    -- Slash command that kicked off the current chain of calls (e.g.
    -- "whatsup"), recovered from the "<command-name>" tag Claude Code
    -- injects into the triggering user message - see
    -- webhook/src/clickhouse_ingest.py:_active_command_name_and_version.
    -- The command's filename itself never changes (no "_v<version>"
    -- suffix, no rename) - command_version below instead comes from a
    -- "<command_version>...</command_version>" marker placed in the
    -- command file's own body, which gets expanded into that same
    -- triggering message.
    command_name      LowCardinality(String) DEFAULT '',
    -- Blank for a command never edited since creation - same graceful
    -- fallback as agent_version/skill_version.
    command_version   LowCardinality(String) DEFAULT '',
    -- x-claude-code-agent-id when this row is a subagent's own call, blank
    -- for the orchestrator's own turns. See agent_invocations above.
    agent_invocation_id String DEFAULT '',
    status            LowCardinality(String),
    latency_ms        Nullable(UInt32),
    -- Set when this call's incoming "messages" ends with a tool_result
    -- marked is_error - i.e. this call is reacting to a tool that just
    -- failed. Recovered at ingest time only (from "messages", which never
    -- lands in this table - see event_sources for the full original
    -- payload) - not backfillable from already-ingested rows the way
    -- tool_name/cost were, since the source data is gone once ingested.
    -- Distinct from this row's own tool_name, which is whatever tool (if
    -- any) THIS call's own response goes on to invoke next.
    failed_tool_name  LowCardinality(String) DEFAULT '',
    failed_tool_args  String DEFAULT '',
    failed_tool_error String DEFAULT '',
    -- What kind of call this actually was (agent_spawn/skill_call/
    -- ask_user_question/tool_call/judge_call/system_notification/
    -- suggestion_mode/transcript_handoff/title_gen/interrupted/
    -- webpage_content/llm_answer/unknown), computed once at ingest by
    -- clickhouse_ingest.py:_classify_event and re-computable later by
    -- webhook/src/reparse.py against event_sources.raw_payload_full -
    -- see AGENTS.md/the schema-sql-capture plan for the full category
    -- list. 'unknown' is a real, expected bucket meant to be searched
    -- (`WHERE calculated_type = 'unknown'`) and iterated on, not an error.
    -- Rows ingested before this column existed keep the 'unknown' default
    -- permanently - they predate event_sources, so there's nothing left to
    -- reparse them from (see event_sources below for why .capture/*.json
    -- is never a substitute).
    calculated_type    LowCardinality(String) DEFAULT 'unknown',
    -- Structured, classifier-specific detail (e.g. {"subagent_type":...}
    -- for agent_spawn, {"tools":[...]} for tool_call - always a list even
    -- for one parallel call, so this single column also replaces the old
    -- multi-tool-call arrayJoin gymnastics the "Full trace" companion table
    -- used to need). '{}' when the type carries no extra detail of its own
    -- (e.g. llm_answer, unknown).
    calculated_payload String DEFAULT '{}' CODEC(ZSTD(3)),
    -- Set once per row at ingest/reparse time - the ReplacingMergeTree
    -- version column. A reparse always writes now() here while keeping the
    -- row's own historical `timestamp`, so the newest reparse wins on
    -- merge without corrupting time-range queries.
    ingested_at       DateTime64(3) DEFAULT now64(3),
    INDEX idx_tool_name tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_agent_name agent_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_command_name command_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_user_id user_id TYPE set(1000) GRANULARITY 4,
    INDEX idx_group_id group_id TYPE set(100) GRANULARITY 4,
    INDEX idx_failed_tool_name failed_tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_calculated_type calculated_type TYPE set(100) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (timestamp, session_id, litellm_call_id);

-- Tables predate command_version/litellm_call_id/calculated_type/
-- calculated_payload/ingested_at/group_id: ALTER for stacks whose
-- ClickHouse volume already existed before these columns were added. Note
-- this does NOT change the engine/ORDER BY of an already-existing table
-- (ClickHouse has no ALTER for that) - a stack that needs the
-- ReplacingMergeTree dedup semantics on old data must run the
-- recreate+swap runbook in
-- services/clickhouse/migrations/001_replacing_mergetree.sql once instead.
-- group_alias itself is gone (see 002_user_group_dimensions.sql) - not
-- re-added here even for a stack that predates it, since the migration
-- file already drops it unconditionally.
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS command_version LowCardinality(String) DEFAULT '';
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS litellm_call_id String DEFAULT '';
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS calculated_type LowCardinality(String) DEFAULT 'unknown';
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS calculated_payload String DEFAULT '{}' CODEC(ZSTD(3));
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS ingested_at DateTime64(3) DEFAULT now64(3);
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS group_id LowCardinality(String) DEFAULT '';
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS user_key_hash LowCardinality(String) DEFAULT '';

-- One row per model call (usage report). cost/input_cost/output_cost come
-- straight from LiteLLM's own response_cost/cost_breakdown - no local price
-- table needed or wanted: a manually-maintained model_pricing table used to
-- exist for this and was removed after it was found to overcount cost by
-- several times whenever prompt caching was in play, since it priced every
-- input token at full rate with no cache-read/cache-write discount. LiteLLM
-- already prices those tiers correctly internally.
--
-- Same PARTITION BY/ORDER BY reasoning as agent_events above - every
-- token/cost panel filters by time range first, never by session_id alone.
-- Same ReplacingMergeTree(ingested_at)/litellm_call_id reasoning as
-- agent_events above too - a reparse can rewrite `provider` for an
-- already-ingested row the same way it rewrites calculated_type there.
CREATE TABLE IF NOT EXISTS agent_usage
(
    timestamp            DateTime64(3),
    user_id              LowCardinality(String),
    -- Stable team id (metadata.user_api_key_team_id) - see agent_events'
    -- group_id comment above for why this, not the alias, is the filter key.
    group_id             LowCardinality(String) DEFAULT '',
    -- See agent_events.user_key_hash above.
    user_key_hash        LowCardinality(String) DEFAULT '',
    session_id           String,
    trace_id             String,
    turn_id              UInt32,
    litellm_call_id      String DEFAULT '',
    model                LowCardinality(String),
    -- claude/openai/other, classified once from `model` at ingest time (the
    -- same 3-way regex that used to be duplicated across ~30 dashboard
    -- panels) - see clickhouse_ingest.py's provider classifier.
    provider             LowCardinality(String) DEFAULT '',
    agent_name           LowCardinality(String),
    agent_version        LowCardinality(String),
    skill_name           LowCardinality(String),
    skill_version        LowCardinality(String),
    command_name         LowCardinality(String) DEFAULT '',
    command_version      LowCardinality(String) DEFAULT '',
    agent_invocation_id  String DEFAULT '',
    mcp_tool_name        LowCardinality(String),
    input_tokens         UInt32,
    output_tokens         UInt32,
    cache_creation_tokens UInt32,
    cache_read_tokens     UInt32,
    -- Why the turn stopped generating (tool_use/end_turn/max_tokens/
    -- refusal) - lets a truncated or refused turn be told apart from a
    -- normal completion.
    stop_reason           LowCardinality(String) DEFAULT '',
    -- cache_creation_tokens above stays the sum of these two, for the
    -- existing cost/token panels; 1h vs 5m ephemeral cache writes are
    -- priced differently, hence the separate breakdown.
    cache_creation_1h_tokens UInt32 DEFAULT 0,
    cache_creation_5m_tokens UInt32 DEFAULT 0,
    -- From LiteLLM's own response_cost/cost_breakdown (total/input/output
    -- split) - see the table comment above for why these replaced a local
    -- price table instead of being derived from one.
    cost                  Float64 DEFAULT 0,
    input_cost            Float64 DEFAULT 0,
    output_cost           Float64 DEFAULT 0,
    cache_hit             UInt8 DEFAULT 0,
    -- completionStartTime - startTime, in ms: time to first token, distinct
    -- from the total call latency in agent_events.latency_ms.
    ttft_ms               UInt32 DEFAULT 0,
    ingested_at           DateTime64(3) DEFAULT now64(3),
    INDEX idx_agent_name agent_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_skill_name skill_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_command_name command_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_mcp_tool_name mcp_tool_name TYPE set(1000) GRANULARITY 4,
    INDEX idx_user_id user_id TYPE set(1000) GRANULARITY 4,
    INDEX idx_group_id group_id TYPE set(100) GRANULARITY 4,
    INDEX idx_provider provider TYPE set(10) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (timestamp, session_id, litellm_call_id);

-- Tables predate command_version/litellm_call_id/provider/ingested_at/
-- group_id: see the agent_events ALTER comment above - same caveat applies
-- here (no ENGINE/ORDER BY migration via ALTER; use the migrations/ runbook).
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS command_version LowCardinality(String) DEFAULT '';
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS litellm_call_id String DEFAULT '';
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS provider LowCardinality(String) DEFAULT '';
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS ingested_at DateTime64(3) DEFAULT now64(3);
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS group_id LowCardinality(String) DEFAULT '';
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS user_key_hash LowCardinality(String) DEFAULT '';

-- One row per turn (main session turn or subagent turn), holding the
-- actual prompt sent to the model and the text it replied with. Kept
-- separate from agent_usage/agent_events - this is arbitrary-length free
-- text, not a fixed-width metric or lifecycle event, and most queries
-- (cost, latency, error rate) never need to touch it. Looked up by
-- (session_id, turn_id) from a specific agent_events row via a Grafana
-- data link - see "Full trace" panel and the message/tool-detail panels
-- in the Grafana dashboard section below.
-- ReplacingMergeTree(ingested_at)/litellm_call_id: same reparse-rewrite
-- reasoning as agent_events above. turn_id is dropped from the sort key
-- entirely here (kept as a column - it's just hardcoded to 0 by
-- clickhouse_ingest.py today, not a real per-session sequence number, so it
-- was never a safe uniqueness key to begin with; every row sharing
-- (session_id, 0) would have collapsed to one under ReplacingMergeTree).
CREATE TABLE IF NOT EXISTS agent_messages
(
    timestamp     DateTime64(3),
    user_id       LowCardinality(String),
    -- Stable team id (metadata.user_api_key_team_id) - see agent_events'
    -- group_id comment above for why this, not the alias, is the filter key.
    group_id      LowCardinality(String) DEFAULT '',
    -- See agent_events.user_key_hash above.
    user_key_hash LowCardinality(String) DEFAULT '',
    session_id    String,
    trace_id      String,
    turn_id       UInt32,
    litellm_call_id String DEFAULT '',
    agent_name    LowCardinality(String),
    agent_version LowCardinality(String),
    skill_name    LowCardinality(String),
    skill_version LowCardinality(String),
    command_name  LowCardinality(String) DEFAULT '',
    command_version LowCardinality(String) DEFAULT '',
    agent_invocation_id String DEFAULT '',
    prompt_text   String CODEC(ZSTD(3)),
    response_text String CODEC(ZSTD(3)),
    ingested_at   DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
-- Unlike agent_events/agent_usage, this table is always looked up by a
-- specific session_id (joined from an agent_events row) rather than
-- scanned by time range, so session_id stays the lead sort key. Half-year
-- partitioning is still worth it here for the same detach-to-archive
-- reason as agent_events above, even though it doesn't change how these
-- particular queries are pruned.
PARTITION BY concat(toString(toYear(timestamp)), '-H', toString(intDiv(toMonth(timestamp) - 1, 6) + 1))
ORDER BY (session_id, litellm_call_id);

-- Tables predate command_version/litellm_call_id/ingested_at/group_id: see
-- the agent_events ALTER comment above - same ENGINE/ORDER BY caveat applies.
ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS command_version LowCardinality(String) DEFAULT '';
ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS litellm_call_id String DEFAULT '';
ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS ingested_at DateTime64(3) DEFAULT now64(3);
ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS group_id LowCardinality(String) DEFAULT '';
ALTER TABLE agent_messages ADD COLUMN IF NOT EXISTS user_key_hash LowCardinality(String) DEFAULT '';

-- Full, untouched original StandardLoggingPayload per call (messages
-- included - the one place in this schema that keeps that field), written
-- exactly once at ingest time by clickhouse_ingest.py's _source_row,
-- compressed hard (ZSTD(19), near-max - this column is write-once and read
-- only by webhook/src/reparse.py, never by a live dashboard query, so we
-- optimize purely for size over CPU). This is what makes reparsing
-- possible: calculated_type/calculated_payload/provider classifiers can be
-- rewritten and rerun later against real historical payloads without
-- needing the original webhook call again.
--
-- Deliberately separate from .capture/*.json: that's a CAPTURE_ENABLED-
-- gated, off-by-default debug aid with no retention policy and no place in
-- the actual data model - no ingest or reparse code path may ever read from
-- it, now or in the future, including as a one-time historical backfill
-- source. This table is the only source of truth for "the full payload
-- behind row X."
--
-- PARTITION BY the same half-year convention as the other tables above -
-- not for query pruning (this table is read rarely, by litellm_call_id or
-- session_id, never by time range scan) but so that whenever old data
-- starts moving to MinIO, whole half-year partitions are the natural,
-- already-proven unit to detach and ship off (see the half-year comment on
-- agent_events above for the existing DETACH PARTITION pattern) - not
-- building that MinIO move now, just not painting this table's layout into
-- a corner that would need reshaping later to support it.
CREATE TABLE IF NOT EXISTS event_sources
(
    litellm_call_id  String,
    session_id       String,
    ingested_at      DateTime64(3) DEFAULT now64(3),
    raw_payload_full String CODEC(ZSTD(19)),
    INDEX idx_session_id session_id TYPE set(1000) GRANULARITY 4
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY concat(toString(toYear(ingested_at)), '-H', toString(intDiv(toMonth(ingested_at) - 1, 6) + 1))
ORDER BY (litellm_call_id);
