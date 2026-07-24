#!/usr/bin/env bash
# Restores $CLICKHOUSE_DATABASE from a file previously written by
# backup_clickhouse.sh (services/clickhouse/config.d/backups.xml's `backups`
# disk, i.e. $BACKUP_DIR/clickhouse/<file> on the host).
#
# DESTRUCTIVE: drops the database before restoring. See
# services/backup/README.md before running this against anything but a
# throwaway/verification target.
#
# Usage: restore_clickhouse.sh <filename> --yes
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

file="${1:-}"
shift || true
require_confirmation "$@"

if [ -z "$file" ]; then
    echo "Usage: restore_clickhouse.sh <filename> --yes" >&2
    exit 1
fi

log "Restoring ClickHouse database '${CLICKHOUSE_DATABASE}' from ${file} (dropping existing database first)"

clickhouse-client \
    --host "$CLICKHOUSE_HOST" \
    --port "${CLICKHOUSE_NATIVE_PORT:-9000}" \
    --user "$CLICKHOUSE_USER" \
    --password "$CLICKHOUSE_PASSWORD" \
    --query "DROP DATABASE IF EXISTS \`${CLICKHOUSE_DATABASE}\`"

clickhouse-client \
    --host "$CLICKHOUSE_HOST" \
    --port "${CLICKHOUSE_NATIVE_PORT:-9000}" \
    --user "$CLICKHOUSE_USER" \
    --password "$CLICKHOUSE_PASSWORD" \
    --query "RESTORE DATABASE \`${CLICKHOUSE_DATABASE}\` FROM Disk('backups', '${file}')"

log "ClickHouse restore complete from ${file}"
