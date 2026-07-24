#!/usr/bin/env bash
# Restores litellm-db from a file previously written by backup_litellm.sh
# ($BACKUP_DIR/litellm/<file> on the host).
#
# DESTRUCTIVE, and litellm writes to this DB continuously - stop the
# `litellm` container first (litellm-db itself must stay up, this script
# connects to it). See services/backup/README.md.
#
# Usage: restore_litellm.sh <filename> --yes
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

file="${1:-}"
shift || true
require_confirmation "$@"

if [ -z "$file" ]; then
    echo "Usage: restore_litellm.sh <filename> --yes" >&2
    exit 1
fi

log "Restoring litellm-db from ${file} (--clean: drops existing objects first)"
PGPASSWORD="$LITELLM_DB_PASSWORD" pg_restore \
    -h litellm-db -p 5432 -U litellm -d litellm \
    --clean --if-exists \
    "${BACKUPS_ROOT}/litellm/${file}"

log "litellm-db restore complete from ${file}"
