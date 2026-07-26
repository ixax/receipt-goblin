---
name: infra-backups
description: >
  Rules for backing up/restoring the stack's non-reproducible state (`clickhouse`, `litellm-db`, `grafana`'s `grafana.db`) via the `backup` tools-profile service.
  TRIGGER - read BEFORE running any `make backup-*`/`restore-*` target, editing anything under `services/backup/`, touching a cron job that calls one of those targets, or making a major/destructive ClickHouse change (a migration with a BACKFILL, a manual data-surgery query, a `schema.sql` re-apply, truncating tables before a load test) where backing up first (and knowing how to restore after) matters.
  <version>1.0.0</version>
---

# infra-backups

Backs up/restores the three services with state not reproducible from the
repo (`clickhouse`, `litellm-db`, `grafana`'s `grafana.db`) via the `backup`
tools-profile service.

Full playbook (setup, `make backup-*`/`restore-*` usage, per-service restore
steps, cron) lives in README.md's "Backup & restore" section - read that for
the actual mechanics. This skill covers when and why, plus the rules that
must never be violated regardless of mechanics.

## Before a major ClickHouse change

Take a `make backup-clickhouse` (or whichever `backup-*` target covers the
table(s) involved) before any of these, since they're the ones that have
actually destroyed/corrupted data in this stack before:

- Applying a migration under `services/clickhouse/migrations/` that includes
  a BACKFILL or an engine/rename change (see the `clickhouse-migration`
  skill for the migration itself).
- Re-applying `services/clickhouse/schema.sql` by hand against an
  already-initialized volume.
- Truncating `agent_events`/`agent_invocations`/`agent_messages`/`agent_usage`/etc.
  before a load test run (see "Running the load test" in `AGENTS.md`).
- Any manual `ALTER`/`DELETE`/data-surgery query run directly against
  ClickHouse for a one-off fix.

If the change goes wrong, restore from that backup rather than trying to
hand-patch the damage - see README's "Backup & restore" for the actual
`restore-clickhouse` steps.

## Rules that never change

- **Backup is always safe to run against a live stack; restore is always
  destructive** - it drops/overwrites the live target, and for
  `litellm`/`grafana` specifically requires that service stopped first (the
  `backup` container never gets Docker API/socket access, so it can't
  stop/start sibling containers itself).
- **Cron only ever calls `make backup-all`.** Never point a cron job at a
  `restore-*` target - restore stays a manual, deliberate action taken by a
  human who's read README.md first.
- **No automatic pruning/retention** - `.backups/` (or `$BACKUP_DIR` if set)
  accumulates every backup file until removed by hand. Don't add a
  retention/cleanup step without being asked; it was deliberately left out.
