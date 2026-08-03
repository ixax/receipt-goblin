#!/bin/sh
# Idempotent, safe to re-run. Points git at the tracked .githooks/ directory
# and re-asserts the executable bit (belt-and-suspenders for non-POSIX
# filesystems or core.fileMode=false dropping the tracked exec bit).
set -e

git config core.hooksPath .githooks
chmod +x .githooks/* .githooks/lib/* 2>/dev/null || true

echo "git hooks installed: core.hooksPath=.githooks"
