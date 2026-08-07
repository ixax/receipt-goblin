#!/bin/sh
# Exits non-zero if a staged services/<name>/ change has no matching *_TAG
# bump staged in VERSIONS.yml - see VERSIONS.yml's own header comment and
# agent_docs/rules/coding.md for the policy this enforces.
# Pure POSIX, git-only, no uv/python3 - same constraint as check-lock.sh.
#
# Scope is dir-level, not file-level: any staged path under services/<name>/
# requires that name's *_TAG line to appear in `git diff --cached VERSIONS.yml`
# (added or removed, i.e. the line was touched) - not a verified semver
# increase, matching check-lock.sh's "co-staging, not freshness" scope.
# services/init/ is a provisioning script, not an image - excluded.
# services/_common/ is shared by all eight "." build-context services below
# (webhook/worker/reparse/migrate/loadtest/loadtest-fixtures/mcp-dev/mcp-stats)
# - touching it requires bumping ANY ONE of their tags, not all eight.

staged=$(git diff --cached --name-only)

key_for_dir() {
    case "$1" in
        clickhouse) echo CLICKHOUSE_TAG ;;
        grafana) echo GRAFANA_TAG ;;
        redis) echo REDIS_TAG ;;
        webhook) echo WEBHOOK_TAG ;;
        worker) echo WORKER_TAG ;;
        reparse) echo REPARSE_TAG ;;
        migrate) echo MIGRATE_TAG ;;
        loadtest) echo LOADTEST_TAG ;;
        loadtest-fixtures) echo LOADTEST_FIXTURES_TAG ;;
        mcp-dev) echo MCP_DEV_TAG ;;
        mcp-stats) echo MCP_STATS_TAG ;;
        litellm) echo LITELLM_TAG ;;
        load-balancer) echo LOAD_BALANCER_TAG ;;
        backup) echo BACKUP_TAG ;;
        prometheus) echo PROMETHEUS_TAG ;;
        blackbox) echo BLACKBOX_TAG ;;
        loki) echo LOKI_TAG ;;
        redis-exporter) echo REDIS_EXPORTER_TAG ;;
        nginx-exporter) echo NGINX_EXPORTER_TAG ;;
        node-exporter) echo NODE_EXPORTER_TAG ;;
        alloy) echo ALLOY_TAG ;;
        langfuse-minio) echo LANGFUSE_MINIO_TAG ;;
        langfuse-redis) echo LANGFUSE_REDIS_TAG ;;
    esac
}

common_group="WEBHOOK_TAG WORKER_TAG REPARSE_TAG MIGRATE_TAG LOADTEST_TAG LOADTEST_FIXTURES_TAG MCP_DEV_TAG MCP_STATS_TAG"

touched_keys=$(git diff --cached -U0 -- VERSIONS.yml | grep -E '^[+-][A-Z_]+_TAG:' | sed -E 's/^[+-]//; s/:.*//' | sort -u)

key_touched() {
    printf '%s\n' "$touched_keys" | grep -qx "$1"
}

required_keys=""
common_seen=0
for path in $staged; do
    case "$path" in
        services/_common/*)
            common_seen=1
            continue
            ;;
        services/init/*)
            continue
            ;;
        services/*/*)
            dir=${path#services/}
            dir=${dir%%/*}
            key=$(key_for_dir "$dir")
            [ -z "$key" ] && continue
            case " $required_keys " in
                *" $key "*) ;;
                *) required_keys="$required_keys $key" ;;
            esac
            ;;
    esac
done

missing=""
add_missing() {
    if [ -z "$missing" ]; then
        missing="$1"
    else
        missing="$missing
$1"
    fi
}

for key in $required_keys; do
    key_touched "$key" || add_missing "  $key"
done

if [ "$common_seen" = 1 ]; then
    common_ok=0
    for key in $common_group; do
        if key_touched "$key"; then
            common_ok=1
            break
        fi
    done
    [ "$common_ok" = 0 ] && add_missing "  services/_common (needs any one of: $common_group)"
fi

if [ -z "$missing" ]; then
    exit 0
fi

echo "error: service code staged without matching VERSIONS.yml tag bump:"
printf '%s\n' "$missing"
echo "bump the SEMVER by hand in VERSIONS.yml and stage it - see agent_docs/rules/coding.md"
exit 1
