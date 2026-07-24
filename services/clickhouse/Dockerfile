# Bakes schema.sql + config.d/*.xml into the image at build time - no
# config bind-mounts in docker-compose.yml. A config change means rebuilding
# this image, not editing a mounted file.
ARG CLICKHOUSE_VERSION
FROM clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}

# Runs automatically on first container start against an empty data dir
# (docker-entrypoint-initdb.d convention) - this is how schema.sql gets
# applied without a manual step.
COPY schema.sql /docker-entrypoint-initdb.d/schema.sql

COPY config.d/ /etc/clickhouse-server/config.d/
COPY users.d/ /etc/clickhouse-server/users.d/
