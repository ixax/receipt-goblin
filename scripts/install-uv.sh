#!/bin/sh
# Single source of truth for installing uv - called both from the Makefile's
# top-of-file parse-time guard (uv is needed to parse the file at all, see
# that comment) and from the standalone `install-uv` target (manual
# reinstall/upgrade). Can't route the parse-time guard through
# `$(MAKE) install-uv` instead of this script - that would spawn a new make
# process, which re-parses the Makefile from scratch and hits the same guard
# again before ever reaching the install-uv recipe, recursing forever.
set -e

curl -LsSf https://astral.sh/uv/install.sh | sh
