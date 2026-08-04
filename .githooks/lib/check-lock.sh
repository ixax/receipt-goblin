#!/bin/sh
# Exits non-zero if a staged services/<svc>/requirements.txt has no matching
# staged requirements.lock.
# The image installs from the lock, never the .txt, so committing the .txt
# alone changes nothing anywhere - the kind of no-op that only surfaces much
# later, as "my dependency didn't land".
# Pure POSIX, and deliberately git-only - this must stay runnable on a
# machine that has neither uv nor python3.
#
# Scope is co-staging, not freshness: a stale lock staged alongside its .txt
# passes. Verifying freshness would mean re-running `make lock` (uv, network)
# on every commit, which is far too slow for a pre-commit hook.

staged=$(git diff --cached --name-only)

missing=""
for path in $staged; do
    case "$path" in
        services/*/requirements.txt) ;;
        *) continue ;;
    esac

    lock="${path%.txt}.lock"
    if ! printf '%s\n' "$staged" | grep -qx "$lock"; then
        missing="$missing $lock"
    fi
done

if [ -z "$missing" ]; then
    exit 0
fi

echo "error: requirements.txt staged without its lock:"
for lock in $missing; do
    echo "  $lock"
done
echo "the image installs from the lock - regenerate and stage it:"
echo "  make lock && git add$missing"
exit 1
