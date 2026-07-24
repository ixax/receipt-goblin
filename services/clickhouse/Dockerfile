# Config/schema baked in at build time (this repo's convention) - no config
# bind-mounts. A config change means rebuilding this image.
ARG CLICKHOUSE_VERSION
FROM clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}

# docker-entrypoint-initdb.d convention: runs automatically on first start
# against an empty data dir.
COPY schema.sql /docker-entrypoint-initdb.d/schema.sql

COPY config.d/ /etc/clickhouse-server/config.d/
COPY users.d/ /etc/clickhouse-server/users.d/
