# `redis`

Queue between `webhook` (producer) and `webhook-worker` (consumer) - see `agent_docs/services/common.md`'s "Why a queue in front of ClickHouse".

## `redis.conf`

Baked into `services/redis/Dockerfile` at build time and pointed at directly by its `CMD`, not passed via `docker-compose.yml`.
`maxmemory 1024mb` bounds the worst case (a stuck worker/ClickHouse) instead of growing unboundedly - bumped from 700mb since each queued event now carries the full original payload (`source_row`, ~360KB avg/~1.5MB max) for `event_sources`, not just compact rows.
The stream's own `MAXLEN` (`services/_common/queue.yml`, shared by `webhook`/`webhook-worker`) is what actually keeps memory there in practice - see `AGENTS.md` for the sizing math.
`maxmemory-policy noeviction` - never silently drop queued events under memory pressure, fail loudly instead.
`appendonly yes` - durability across a restart.
