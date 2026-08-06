# `redis`

Queue between `webhook` (producer) and `webhook-worker` (consumer) for both LiteLLM callbacks and direct Claude usage envelopes - see `agent_docs/services/common.md`'s "Why a queue in front of ClickHouse".

LiteLLM entries contain the original callback body and default to the `litellm_standard` worker adapter.
Direct entries contain a compact normalized envelope plus `adapter=claude_transcript`.
The host collector sends direct history in bounded HTTP batches with a maximum event rate so a backfill does not immediately crowd live events out of the bounded stream.

Redis is the handoff point for the collector's at-least-once delivery.
The webhook returns a non-2xx response when a direct batch cannot be enqueued, so the collector keeps it in SQLite and retries.
A partially accepted retry can create duplicate Redis entries.
ClickHouse dedupes their downstream rows by transcript `requestId`/`litellm_call_id` after merge.

## `redis.conf`

Baked into `services/redis/Dockerfile` at build time and pointed at directly by its `CMD`, not passed via `docker-compose.yml`.
`maxmemory 1024mb` bounds the worst case (a stuck worker/ClickHouse) instead of growing unboundedly - bumped from 700mb since each queued event now carries the full original payload (`source_row`, ~360KB avg/~1.5MB max) for `event_sources`, not just compact rows.
The stream's own `MAXLEN` (`services/_common/queue.yml`, shared by `webhook`/`webhook-worker`) is what actually keeps memory there in practice - see `AGENTS.md` for the sizing math.
`maxmemory-policy noeviction` - never silently drop queued events under memory pressure, fail loudly instead.
`appendonly yes` - durability across a restart.
