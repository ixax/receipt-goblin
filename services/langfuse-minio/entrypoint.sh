#!/bin/sh
# Pre-creates the "langfuse" bucket dir under the data root before starting
# the server - minio has no docker-entrypoint-initdb.d equivalent, so this
# replaces what used to be an inline compose `command:` shell one-liner.
set -eu

mkdir -p /data/langfuse
exec minio server --address ":9000" --console-address ":9001" /data
