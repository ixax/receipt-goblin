# Tools-profile image (see the `backup` service in docker-compose.yml) - not
# started by `docker compose up`, only via `docker compose run --rm backup
# ...` (wrapped by the Makefile's backup-*/restore-* targets). Talks to
# clickhouse/litellm-db over the network and reads the grafana-data named
# volume directly - never docker exec, never the Docker socket.
#
# debian:bookworm-slim rather than clickhouse/clickhouse-server (Ubuntu
# 20.04/focal) - focal's own apt repos only carry postgresql-client 12,
# and PGDG has since dropped focal support entirely (404 on focal-pgdg).
# litellm-db runs Postgres 16 (docker-compose.yml), and pg_dump/pg_restore
# aren't reliably forward-compatible with a newer server than the client,
# so both clickhouse-client and postgresql-client are pulled from their
# upstream apt repos here, pinned to versions matching the services they
# talk to.
FROM debian:bookworm-slim

# Single source of truth is CLICKHOUSE_VERSION in .env, passed as a build
# arg from docker-compose.yml's `backup` service - same value used to build
# the `clickhouse` service's own image (services/clickhouse/Dockerfile), so
# this apt-pinned client version never drifts from the server it talks to.
ARG CLICKHOUSE_VERSION

RUN apt-get update && \
    apt-get install --no-install-recommends -y ca-certificates curl gnupg sqlite3 && \
    # ClickHouse apt repo (see https://clickhouse.com/docs/install#available-installation-options)
    curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=$(dpkg --print-architecture)] https://packages.clickhouse.com/deb stable main" \
        > /etc/apt/sources.list.d/clickhouse.list && \
    # PGDG apt repo (see https://www.postgresql.org/download/linux/debian/) -
    # postgresql-client-16 matches litellm-db's postgres:16-alpine image tag.
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/keyrings/postgresql-keyring.asc && \
    echo "deb [signed-by=/usr/share/keyrings/postgresql-keyring.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    apt-get install --no-install-recommends -y clickhouse-client=${CLICKHOUSE_VERSION} clickhouse-common-static=${CLICKHOUSE_VERSION} postgresql-client-16 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scripts/ ./scripts/
RUN chmod +x ./scripts/*.sh
