# Tools-profile image - only run via `docker compose run --rm backup ...`
# (Makefile backup-*/restore-* targets), never `up`. Talks to
# clickhouse/litellm-db over the network and reads the grafana-data volume
# directly - never docker exec, never the Docker socket.
#
# debian:bookworm-slim, not clickhouse/clickhouse-server (focal) - focal's
# apt/PGDG no longer carry postgresql-client 16, and pg_dump/pg_restore need
# client >= server (litellm-db runs Postgres 16). Both clients are pulled
# from their upstream apt repos here, version-pinned to what they talk to.
FROM debian:bookworm-slim

# Same CLICKHOUSE_VERSION used to build the clickhouse service's own image,
# so this client never drifts from the server it talks to.
ARG CLICKHOUSE_VERSION

RUN apt-get update && \
    apt-get install --no-install-recommends -y ca-certificates curl gnupg sqlite3 && \
    # ClickHouse apt repo
    curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=$(dpkg --print-architecture)] https://packages.clickhouse.com/deb stable main" \
        > /etc/apt/sources.list.d/clickhouse.list && \
    # PGDG apt repo; postgresql-client-16 matches litellm-db's postgres:16-alpine tag.
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/keyrings/postgresql-keyring.asc && \
    echo "deb [signed-by=/usr/share/keyrings/postgresql-keyring.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    apt-get install --no-install-recommends -y clickhouse-client=${CLICKHOUSE_VERSION} clickhouse-common-static=${CLICKHOUSE_VERSION} postgresql-client-16 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scripts/ ./scripts/
RUN chmod +x ./scripts/*.sh
