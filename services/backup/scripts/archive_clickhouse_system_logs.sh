#!/usr/bin/env bash
# Archives + drops old partitions from ClickHouse's own system.* log tables
# (query_log, crash_log, asynchronous_metric_log, metric_log -
# services/clickhouse/config.d/logging.xml), never the app's business
# tables - those are governed by the repo's no-TTL-auto-delete rule
# (agent_docs/rules/coding.md) and this script is scoped away from them on
# purpose.
# Uses ClickHouse's native BACKUP/DROP PARTITION, same `backups` disk as
# backup_clickhouse.sh, writing into $BACKUP_DIR/clickhouse on the host.
# Needs bootstrap rights (system.* DDL isn't covered by the backup role's
# grants) - see CLICKHOUSE_BOOTSTRAP_USER/_PASSWORD below.
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

RETENTION_MONTHS="${CLICKHOUSE_LOG_RETENTION_MONTHS:-3}"
ARCHIVE_RETENTION_DAYS="${CLICKHOUSE_LOG_ARCHIVE_RETENTION_DAYS:-180}"
TABLES="query_log crash_log asynchronous_metric_log metric_log"

client() {
    clickhouse-client \
        --host "$CLICKHOUSE_HOST" \
        --port "${CLICKHOUSE_NATIVE_PORT:-9000}" \
        --user "$CLICKHOUSE_BOOTSTRAP_USER" \
        --password "$CLICKHOUSE_BOOTSTRAP_PASSWORD" \
        "$@"
}

cutoff=$(date -u -d "${RETENTION_MONTHS} months ago" +%Y%m)

for table in $TABLES; do
    partitions=$(client --query "SELECT DISTINCT partition FROM system.parts WHERE database = 'system' AND table = '${table}' AND active" | sort -u)

    for partition in $partitions; do
        if [ "$partition" -lt "$cutoff" ]; then
            file="system_${table}_${partition}.zip"
            log "Archiving system.${table} partition ${partition} to disk file ${file}"
            client --query "BACKUP TABLE system.\`${table}\` PARTITION '${partition}' TO Disk('backups', '${file}')"

            log "Dropping system.${table} partition ${partition}"
            client --query "ALTER TABLE system.\`${table}\` DROP PARTITION '${partition}'"
        fi
    done
done

log "Pruning system_*.zip archives in /backups/clickhouse older than ${ARCHIVE_RETENTION_DAYS}d"
find /backups/clickhouse -maxdepth 1 -name 'system_*.zip' -mtime "+${ARCHIVE_RETENTION_DAYS}" -delete

log "ClickHouse system log archive complete"
