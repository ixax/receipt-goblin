---
name: clickhouse-migration
description: >
  Checklist for writing a ClickHouse schema migration.
  TRIGGER - read BEFORE creating or editing any file under `services/clickhouse/migrations/`, or changing `agent_events`/`agent_usage`/`agent_messages`/`ingest_raw` schema in any other way.
  <version>1.0.2</version>
---

# clickhouse-migration

`mcp-dev`'s `query` tool is read-only by validation (SELECT/WITH only, DDL rejected server-side) - no agent can run a schema change through it.
Any change to database tables (new column, engine change, new table) happens in the main conversation with Bash, the same way `services/clickhouse/migrations/001_replacing_mergetree.sql` was applied:

- One `.sql` file per migration in `services/clickhouse/migrations/`,
  numbered in order (`002_...`, `003_...`, ...) with a short, descriptive
  name.
- Start the file with a comment block explaining *why* the migration is
  needed (what's broken/missing without it), not just what it does.
- Write every migration so a second run is a no-op: `CREATE TABLE IF NOT
  EXISTS`, guard `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, check current
  state before a rename/swap rather than assuming a clean starting point.
- If the migration includes a BACKFILL, make the backfill itself
  idempotent/run-once too - gate it on the column still being at its
  default, or use `INSERT ... SELECT` into a dedup-by-engine target rather
  than an unconditional `UPDATE`-style rewrite; re-running the file must not
  double-apply it.
- Test the migration's queries (the `SELECT` parts, and any query that will
  read the changed schema afterward) via the ClickHouse MCP tool before
  considering it done, same as any other query - see `clickhouse-analyst`.
- `docker-entrypoint-initdb.d` only applies `services/clickhouse/schema.sql`
  to a brand-new empty volume, so also update `schema.sql` to match the new
  end state - migrations are for existing stacks, `schema.sql` is what a
  fresh stack gets.
