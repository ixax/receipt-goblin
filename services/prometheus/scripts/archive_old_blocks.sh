#!/bin/sh
# Archives Prometheus TSDB blocks older than PROMETHEUS_ARCHIVE_AFTER_DAYS
# into /backups/prometheus (bind-mounted to $BACKUP_DIR/prometheus on the
# host), then prunes archive files older than
# PROMETHEUS_ARCHIVE_RETENTION_DAYS. Run via `make archive-prometheus`
# (docker compose exec into the running container) - not a background
# process, safe to run against a live server since each block directory is
# self-contained and immutable, exactly like Prometheus's own retention
# manager touches them.
set -eu

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

AFTER_DAYS="${PROMETHEUS_ARCHIVE_AFTER_DAYS:-14}"
RETENTION_DAYS="${PROMETHEUS_ARCHIVE_RETENTION_DAYS:-90}"
DATA_DIR="/prometheus"
BACKUP_DIR="/backups/prometheus"
CUTOFF=$(( $(date -u +%s) - AFTER_DAYS * 86400 ))

mkdir -p "$BACKUP_DIR"

log "Archiving blocks in $DATA_DIR older than ${AFTER_DAYS}d (cutoff epoch $CUTOFF)"

for block in "$DATA_DIR"/*/; do
    block="${block%/}"
    meta="$block/meta.json"
    [ -f "$meta" ] || continue

    max_time_ms=$(grep -o '"maxTime":[0-9]*' "$meta" | head -1 | sed 's/[^0-9]*//')
    [ -n "$max_time_ms" ] || continue
    max_time_s=$(( max_time_ms / 1000 ))

    if [ "$max_time_s" -lt "$CUTOFF" ]; then
        ulid=$(basename "$block")
        date_tag=$(date -u -d "@${max_time_s}" +%Y%m%d)
        archive="$BACKUP_DIR/${ulid}_${date_tag}.tar.gz"
        log "Archiving block $ulid (maxTime $date_tag) -> $archive"
        tar czf "$archive" -C "$DATA_DIR" "$ulid"
        rm -rf "$block"
    fi
done

log "Pruning archives in $BACKUP_DIR older than ${RETENTION_DAYS}d"
find "$BACKUP_DIR" -name '*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete

log "Done"
