#!/usr/bin/env bash
# Restores $CLICKHOUSE_DATABASE from a file previously written by
# backup_clickhouse.sh (services/clickhouse/config.d/backups.xml's `backups`
# disk, i.e. $BACKUP_DIR/clickhouse/<file> on the host).
#
# DESTRUCTIVE: drops the database before restoring. See
# README.md's "Backup & restore" section before running this against anything but a
# throwaway/verification target.
#
# Uses CLICKHOUSE_BOOTSTRAP_USER, not the backup role's CLICKHOUSE_USER -
# DROP/RESTORE DATABASE are database-level DDL, which no least-privilege
# role's grant covers (the backup role only holds BACKUP, see
# services/init/config.yml). Confirmed the hard way, back when every
# service shared one app user with `GRANT ALL ON <database>.*`: running this
# as that app user let DROP DATABASE through (dropped it anyway) but RESTORE
# DATABASE then failed with "Database default does not exist", leaving the
# database gone with nothing put back - the bootstrap superuser is what
# migrate.py itself uses for equivalent database-level operations.
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
    --user "$CLICKHOUSE_BOOTSTRAP_USER" \
    --password "$CLICKHOUSE_BOOTSTRAP_PASSWORD" \
    --query "DROP DATABASE IF EXISTS \`${CLICKHOUSE_DATABASE}\`"

clickhouse-client \
    --host "$CLICKHOUSE_HOST" \
    --port "${CLICKHOUSE_NATIVE_PORT:-9000}" \
    --user "$CLICKHOUSE_BOOTSTRAP_USER" \
    --password "$CLICKHOUSE_BOOTSTRAP_PASSWORD" \
    --query "RESTORE DATABASE \`${CLICKHOUSE_DATABASE}\` FROM Disk('backups', '${file}')"

log "ClickHouse restore complete from ${file}"
