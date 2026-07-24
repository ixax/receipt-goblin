#!/usr/bin/env bash
# Restores grafana.db from a file previously written by backup_grafana.sh
# ($BACKUP_DIR/grafana/<file> on the host).
#
# DESTRUCTIVE, and unsafe to run against a live server - stop the `grafana`
# container first (`docker compose stop grafana`), run this, then start it
# back up. See services/backup/README.md.
#
# Usage: restore_grafana.sh <filename> --yes
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

file="${1:-}"
shift || true
require_confirmation "$@"

if [ -z "$file" ]; then
    echo "Usage: restore_grafana.sh <filename> --yes" >&2
    exit 1
fi

src="${BACKUPS_ROOT}/grafana/${file}"
if [ ! -f "$src" ]; then
    echo "No such backup file: ${src}" >&2
    exit 1
fi

log "Restoring grafana.db from ${file}"
cp "$src" /var/lib/grafana/grafana.db

log "Grafana restore complete from ${file}. Start the grafana container back up now."
