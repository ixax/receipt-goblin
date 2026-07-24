#!/usr/bin/env bash
# Shared helpers for backup_*.sh/restore_*.sh - sourced, not run directly.
set -euo pipefail

BACKUPS_ROOT="/backups"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

timestamp() {
    date -u +%Y%m%d-%H%M%S
}

# Restore scripts call this before touching anything destructive - requires
# the literal --yes flag among "$@" so a bare/copy-pasted invocation without
# it fails loudly instead of restoring silently.
require_confirmation() {
    for arg in "$@"; do
        if [ "$arg" = "--yes" ]; then
            return 0
        fi
    done
    echo "Refusing to restore without --yes (this overwrites live data)." >&2
    exit 1
}
