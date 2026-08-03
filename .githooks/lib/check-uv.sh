#!/bin/sh
# Pure POSIX check - must not itself depend on uv/python3, since its job is
# to detect whether uv is present. Future hooks needing real logic (JSON
# parsing, etc.) can shell out to `uv run <script>.py` once they've confirmed
# uv is present via this same check (or fall back to plain python3/skip).

check_uv() {
    if ! command -v uv >/dev/null 2>&1; then
        echo "hint: 'uv' is not installed - install it with:"
        echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
}
