#!/usr/bin/env bash
# Runs every backup_*.sh in turn - the target for cron and `make backup-all`.
# Non-interactive, fails loudly (non-zero exit) if any one of them fails, so
# a cron wrapper's mailed/logged output actually reflects a real problem.
set -euo pipefail
cd "$(dirname "$0")"
. ./common.sh

log "Starting full backup run"

status=0
for svc in clickhouse litellm grafana; do
    if ! "./backup_${svc}.sh"; then
        log "backup_${svc}.sh FAILED"
        status=1
    fi
done

if [ "$status" -eq 0 ]; then
    log "Full backup run complete"
else
    log "Full backup run finished with failures"
fi

exit "$status"
