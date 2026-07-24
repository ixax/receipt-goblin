#!/usr/bin/env bash
# Backs up $CLICKHOUSE_DATABASE via ClickHouse's own native BACKUP statement,
# writing into the `backups` disk configured on the clickhouse server itself
# (services/clickhouse/config.d/backups.xml) - this script only triggers the
# query over the network, the server process does the actual file I/O onto
# its bind-mounted /backups/clickhouse.
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

file="clickhouse_${CLICKHOUSE_DATABASE}_$(timestamp).zip"

log "Backing up ClickHouse database '${CLICKHOUSE_DATABASE}' to disk file ${file}"
clickhouse-client \
    --host "$CLICKHOUSE_HOST" \
    --port "${CLICKHOUSE_NATIVE_PORT:-9000}" \
    --user "$CLICKHOUSE_USER" \
    --password "$CLICKHOUSE_PASSWORD" \
    --query "BACKUP DATABASE \`${CLICKHOUSE_DATABASE}\` TO Disk('backups', '${file}')"

log "ClickHouse backup complete: ${file}"
