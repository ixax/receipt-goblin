#!/usr/bin/env bash
# Backs up grafana.db (users/orgs, API keys, alert rules - not dashboards,
# those are provisioned from services/grafana/dashboards/*.json already in
# the repo) via SQLite's own backup API (sqlite3 .backup), safe to run
# against a live DB with the `grafana` container still up - no downtime.
# Reads the grafana-data named volume directly (mounted into this
# container), not over the network.
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

mkdir -p "${BACKUPS_ROOT}/grafana"
file="grafana_$(timestamp).db"

log "Backing up grafana.db to ${file}"
sqlite3 /var/lib/grafana/grafana.db ".backup '${BACKUPS_ROOT}/grafana/${file}'"

log "Grafana backup complete: ${file}"
