#!/bin/sh
# Exits non-zero if uv is missing, after printing how to install it.
# Pure POSIX - must not itself depend on uv/python3, since its job is to
# detect whether uv is present.
# Callers decide what a non-zero exit means: pre-commit aborts on it, while
# post-merge/post-checkout are advisory and still exit 0.

if command -v uv >/dev/null 2>&1; then
    exit 0
fi

echo "hint: 'uv' is not installed - install it with:"
echo "  make install-uv"
exit 1
