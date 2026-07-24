# Backup & restore

Backs up and restores the three services in this stack that hold state not
reproducible from the repo: `clickhouse` (all tracking data), `litellm-db`
(LiteLLM's virtual keys/budgets/spend logs), and `grafana`'s `grafana.db`
(users/orgs, API keys, alert rules - dashboards themselves are already
source-controlled JSON, see `services/grafana/dashboards/`).

Everything runs through the `backup` tools-profile service
(`docker-compose.yml`) - it never uses `docker exec` or the Docker socket:
`clickhouse`/`litellm-db` are reached over the `receipt-goblin` network,
`grafana-data` is mounted directly as the same named volume the `grafana`
service itself uses. Files land under `$BACKUP_DIR` (`.env`, default
`.backups/` at the repo root) as `.backups/clickhouse/`,
`.backups/litellm/`, `.backups/grafana/`. **No automatic pruning** - backups
accumulate until you remove them by hand.

## One-time setup

`clickhouse`'s BACKUP/RESTORE disk (`services/clickhouse/config.d/backups.xml`)
and its new `$BACKUP_DIR/clickhouse` bind mount only take effect once that
container is recreated:

```
docker compose up -d --build clickhouse
```

This briefly restarts `clickhouse` only - unlike `litellm`, nothing else
depends on avoiding a restart here, but it's still worth doing at a quiet
moment since Grafana panels will show gaps for the few seconds it's down.

## Manual backup

```
make backup-clickhouse   # BACKUP DATABASE via clickhouse-client, safe on a live server
make backup-litellm      # pg_dump against litellm-db, safe on a live server
make backup-grafana      # sqlite3 .backup against grafana.db, safe on a live server
make backup-all          # all three - this is what cron should call
```

None of the three needs any container stopped - each uses a mechanism
that's safe to run against a live, in-use service (ClickHouse's own
BACKUP statement, a consistent `pg_dump` snapshot, SQLite's backup API).

## Restore

**Destructive.** Each restore drops/overwrites the live target - don't run
these against anything but a throwaway/verification target unless you
actually mean to roll back to that snapshot.

List available files first: `ls .backups/clickhouse/`, `ls .backups/litellm/`,
`ls .backups/grafana/` (or under `$BACKUP_DIR` if you set one).

### ClickHouse

Safe to run with `clickhouse` still up (drops and recreates the database as
part of the restore, so any query mid-flight during the restore will simply
fail, not corrupt anything):

```
make restore-clickhouse FILE=clickhouse_default_20260724-030000.zip
```

### LiteLLM

`litellm` writes to `litellm-db` continuously - stop it first so the restore
isn't racing live writes (`litellm-db` itself must stay up, the restore
connects to it):

```
docker compose stop litellm
make restore-litellm FILE=litellm_20260724-030000.dump
docker compose start litellm
```

### Grafana

Swapping `grafana.db` under a live server isn't safe - stop `grafana` first:

```
docker compose stop grafana
make restore-grafana FILE=grafana_20260724-030000.db
docker compose start grafana
```

## Cron

Point cron at `make backup-all` from the repo root (needs `docker`/`make` on
`PATH` for cron's environment, which is usually sparser than an interactive
shell - use absolute paths or source your shell profile if `make`/`docker`
aren't found):

```
0 3 * * * cd /path/to/receipt-goblin && make backup-all >> .backups/cron.log 2>&1
```

Never point cron at a `restore-*` target - restore is a manual, deliberate
operation only (see "Destructive" above).
