# Log & metric retention/rotation for the observability stack

## Context

Metrics and logs are already persisted in named Docker volumes (`prometheus-data`, `loki-data`, plus ClickHouse's own `clickhouse-data` for its `system.*` log tables), but **nothing actually enforces a retention limit today**:

- **Prometheus** (`services/prometheus/Dockerfile`) has no `CMD` override, no retention flag set anywhere in this repo.
- **Loki** (`services/loki/config.yaml`) sets `limits_config.retention_period: 168h`, but has **no `compactor` block**.
  Without `compactor.retention_enabled: true` (+ `delete_request_store`), Loki only compacts data - it never deletes it.
  The number is currently a no-op.
- **ClickHouse system log tables** (`query_log`, `crash_log`, `asynchronous_metric_log`, `metric_log`, `services/clickhouse/config.d/logging.xml`) have no cap and share the `clickhouse-data` volume with business data.
- **Raw per-container stdout/stderr** (Docker's `json-file` driver, the source Alloy tails into Loki) has no `max-size`/`max-file` on any service, so it grows unbounded on the host, independent of Loki.

The goal is real rotation, not just deletion: move old data out into compressed archive files under `$BACKUP_DIR/<service>` (the existing `.backups/` convention, already used by `services/backup/scripts/`), and separately prune archives past a second, longer retention window - implemented as scripts (matching the existing `backup_*.sh` pattern), not new long-running docker services.

**Per-service mechanism differs, deliberately, based on what's safe for each storage format** (confirmed with the user):

| Service                     | Mechanism                                                                                                                                                                                                | Why                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
|-----------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Prometheus                  | **Script**: tar+gzip old TSDB blocks into `$BACKUP_DIR/prometheus`, remove from live dir                                                                                                                 | TSDB blocks (`<ULID>/{meta.json,chunks/,index}`) are self-contained, immutable, physically independent directories - safe to move/delete individually while Prometheus runs (this is exactly what its own retention manager does internally).                                                                                                                                                                                                                                                                                                                              |
| Loki                        | **Native only** (`compactor.retention_enabled`), no manual archive                                                                                                                                       | Loki's chunk files are referenced by name from a separate TSDB index. Hand-moving old chunk files out from under the index desyncs it - queries for that time range then **error**, they don't just return empty. Loki's own compactor deletes chunk *and* index entries atomically; a script can't safely replicate that. If a cold archive of old Loki data is ever wanted, the safe unit is a full periodic snapshot of the whole `loki-data` volume before the compactor deletes anything - not selective per-chunk extraction. This tradeoff is documented in README. |
| ClickHouse system logs      | **Script**: `BACKUP TABLE system.<t> PARTITION '<p>' TO Disk('backups', ...)` (reuses the existing `backups` disk from `services/clickhouse/config.d/backups.xml`) then `ALTER TABLE ... DROP PARTITION` | Native ClickHouse mechanism, already used for whole-database backups (`backup_clickhouse.sh`) - produces a real compressed archive file, removes the partition from live storage. Scoped strictly to `system.*` tables.                                                                                                                                                                                                                                                                                                                                                    |
| Raw docker `json-file` logs | **Native** `logging: {driver: json-file, options: {max-size, max-file}}`                                                                                                                                 | Rotation only (no useful "archive" unit - these are just Loki's raw source, already durably captured in Loki once ingested), applied compose-wide.                                                                                                                                                                                                                                                                                                                                                                                                                         |

**Explicitly out of scope, by design:**
- ClickHouse's *business* tables (`agent_events`, `agent_usage`, `agent_messages`, `ingest_raw`, `ingest_dlq`, `litellm_alerts`) are governed by the repo's hard rule "No TTL-based auto-delete on any table" (`agent_docs/rules/coding.md:18`).
  New scripts here never touch them, and are named/scoped explicitly to avoid ever being pointed at them.
- `.backups/` archive pruning for the *existing* clickhouse/litellm/grafana whole-DB backups - not selected by the user this round.

## Changes

### 1. Prometheus - archive old TSDB blocks to `$BACKUP_DIR/prometheus`

- `docker-compose.yml`: add a new bind mount to the `prometheus` service: `${BACKUP_DIR:-.backups}/prometheus:/backups/prometheus` (data only, same pattern as clickhouse's own backups mount).
- `services/prometheus/scripts/archive_old_blocks.sh` (new, `COPY`'d into the image alongside `prometheus.yml`), run via `docker compose exec prometheus /scripts/archive_old_blocks.sh` (same exec-into-running-container pattern as `services/clickhouse/scripts/create_user.sh`):
  - For each entry in `/prometheus` that contains a `meta.json` (this is exactly how a real persistent block is distinguished from `wal/`/`chunks_head/`/lockfiles - those never have one): extract `maxTime` (millis) via `grep -o`/`sed` (no `jq` in the base image), convert to seconds, compare against a cutoff computed as `$(( $(date +%s) - ${PROMETHEUS_ARCHIVE_AFTER_DAYS:-14} * 86400 ))` (plain arithmetic, portable - avoids relying on GNU `date -d` which the base busybox image may not support).
  - Blocks older than the cutoff: `tar czf /backups/prometheus/<ULID>_<maxTime-date>.tar.gz <ULID>/` then `rm -rf <ULID>/`.
  - Second pass: delete archive files under `/backups/prometheus` older than `${PROMETHEUS_ARCHIVE_RETENTION_DAYS:-90}` days (`find ... -mtime +N -delete`).
  - Follows `common.sh`'s logging style (`log()` with UTC timestamp) even though it can't literally source `services/backup/scripts/common.sh` (different image/context) - just replicate the same one-line log format inline.
- `Makefile` target `archive-prometheus`: `docker compose $(COMPOSE_FILES) exec prometheus /scripts/archive_old_blocks.sh`.

### 2. Loki - enable native retention only (no archive script)

Add the missing `compactor` block to `services/loki/config.yaml` so `retention_period: 168h` is actually enforced:

```yaml
compactor:
  working_directory: /loki/compactor
  compaction_interval: 10m
  retention_enabled: true
  retention_delete_delay: 2h
  delete_request_store: filesystem
```

`/loki/compactor` lands inside the existing `loki-data:/loki` volume (`common.path_prefix: /loki`) - no volume change needed.
Replace the stale "No retention automation" comment.
README must state explicitly why Loki gets no archive script (index/chunk desync risk, see table above), so this isn't mistaken for an oversight later.

### 3. ClickHouse system logs - partition consistently, archive+prune via script

- `services/clickhouse/config.d/logging.xml`: add `<partition_by>toYYYYMM(event_date)</partition_by>` to `crash_log` and `metric_log` (only `query_log`/`asynchronous_metric_log` have it today), so all four partition monthly and can be targeted the same way.
- New `services/backup/scripts/archive_clickhouse_system_logs.sh` (co-located with `backup_*.sh`/`common.sh` - needs `CLICKHOUSE_BOOTSTRAP_USER`/`_PASSWORD`, already wired into the `backup` service's environment, since `system.*` DDL needs bootstrap rights no app role has):
  - For each of `query_log`, `crash_log`, `asynchronous_metric_log`, `metric_log`: find partitions via `SELECT DISTINCT partition FROM system.parts WHERE database='system' AND table='<t>'` older than `${CLICKHOUSE_LOG_RETENTION_MONTHS:-3}` months, run `BACKUP TABLE system.<t> PARTITION '<p>' TO Disk('backups', 'system_<t>_<p>.zip')` then `ALTER TABLE system.<t> DROP PARTITION '<p>'` (archive file lands under the already-mounted `/backups/clickhouse` disk → host `$BACKUP_DIR/clickhouse/`, no new volume needed).
  - Second pass: delete `system_*.zip` archive files under `/backups/clickhouse` older than `${CLICKHOUSE_LOG_ARCHIVE_RETENTION_DAYS:-180}` days.
  - Header comment states explicitly: scoped only to these four `system.*` tables, never the business tables governed by the no-TTL rule (`agent_docs/rules/coding.md:18`).
  - Same `set -euo pipefail` / `log()`/`timestamp()` style as `backup_clickhouse.sh`.
- `Makefile` target `archive-clickhouse-logs`: `docker compose $(COMPOSE_FILES) run --rm backup ./scripts/archive_clickhouse_system_logs.sh`.

### 4. Raw docker container logs - cap `json-file` driver (`docker-compose.yml`)

Add a reusable anchor near the other `x-*` anchors:

```yaml
x-default-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "5"
```

Add `logging: *default-logging` on every service (~25) lacking one.
Confirmed compatible with Alloy's `loki.source.docker` (reads via Docker's API/log files directly - `max-size`/`max-file` don't interfere).
`json-file` rotates/truncates but doesn't compress rotated files - accepted as a bounded local buffer only.
The compressed, queryable long-term copy is Loki (step 2).

### 5. Documentation & agent awareness

- `README.md`: new subsection near "Observability" covering all four mechanisms:
  - the Prometheus archive script and its two env vars
  - Loki's compactor/retention_period, and the explicit reason it gets no archive script
  - the ClickHouse system-log archive script and its two env vars
  - the docker `json-file` cap

  Include a cron suggestion mirroring the existing `backup-all` block (README.md:247) for `archive-prometheus`/`archive-clickhouse-logs`.
- `.claude/agents/dev-ops.md`: add `archive-prometheus`/`archive-clickhouse-logs` to its owned Makefile targets (alongside `backup-*`/`restore-*`), and note the `x-default-logging` anchor applies compose-wide.
  Route this edit through the `harness-expert` agent at execution time (repo convention: agent/skill file edits go through it).
- Delegate all `docker-compose.yml`/`Makefile` edits (steps 1, 3, 4's compose/Makefile parts) to the `dev-ops` agent at execution time - it's the sole owner of those files per its own definition.

## Verification

1. `make up SERVICE=prometheus` (picks up the new mount + baked script), let it run long enough to accumulate a few blocks, then `PROMETHEUS_ARCHIVE_AFTER_DAYS=0 make archive-prometheus` against a dev stack to force-archive everything; confirm `.backups/prometheus/*.tar.gz` appears and `docker exec receipt-goblin-prometheus ls /prometheus` shows the archived block directories gone, `wal/`/`chunks_head/` untouched, and Prometheus's own `/targets`/`/graph` UI still responds (container didn't crash from files disappearing under it).
2. Rebuild Loki (`docker compose build loki`), bring it up, check its logs for successful compactor startup (no config-parse error - Loki fails fast on a bad retention config) and eventually a `"msg"="applying delete request"`/marker-deletion style log line.
3. Run `make archive-clickhouse-logs` against a dev stack with some `system.query_log` history; verify old partitions are gone (`SELECT partition, count() FROM system.parts WHERE database='system' AND table='query_log' GROUP BY partition` before/after) and a `.backups/clickhouse/system_query_log_*.zip` exists; confirm the business tables are untouched (row counts on `agent_events` unchanged).
4. `docker inspect receipt-goblin-<any-service> --format '{{json .HostConfig.LogConfig}}'` after `make up` to confirm the new `json-file` options landed.
5. Confirm README's new section and `dev-ops.md`'s target list read correctly, and that the Loki "no archive script, here's why" explanation is present and accurate.
