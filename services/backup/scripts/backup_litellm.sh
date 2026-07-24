#!/usr/bin/env bash
# Backs up litellm-db (Postgres - virtual keys, budgets, spend logs) via
# pg_dump, custom format (restorable with pg_restore, see restore_litellm.sh).
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

mkdir -p "${BACKUPS_ROOT}/litellm"
file="litellm_$(timestamp).dump"

log "Backing up litellm-db to ${file}"
PGPASSWORD="$LITELLM_DB_PASSWORD" pg_dump \
    -h litellm-db -p 5432 -U litellm -d litellm \
    -F custom \
    -f "${BACKUPS_ROOT}/litellm/${file}"

log "litellm-db backup complete: ${file}"
