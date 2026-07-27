# Config/schema baked in at build time (this repo's convention) - no config
# bind-mounts. A config change means rebuilding this image.
#
# Default is a deliberately bogus tag, not empty/unset - docker-compose.yml
# always passes the real CLICKHOUSE_VERSION as a build arg, so this only ever
# matters if someone builds this image directly (skipping compose) without
# --build-arg CLICKHOUSE_VERSION=... A blank ARG default makes FROM resolve to
# "clickhouse/clickhouse-server:" (buildkit just warns, doesn't fail - that's
# the InvalidDefaultArgInFrom lint) and other unrelated tags could resolve to
# a real-but-wrong version silently; this fails the pull outright instead,
# loudly, with the missing var's name right there in the error.
ARG CLICKHOUSE_VERSION=set-CLICKHOUSE_VERSION-build-arg
FROM clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}

# docker-entrypoint-initdb.d convention: runs automatically on first start
# against an empty data dir.
COPY schema.sql /docker-entrypoint-initdb.d/schema.sql

COPY config.d/ /etc/clickhouse-server/config.d/
COPY users.d/ /etc/clickhouse-server/users.d/

# Admin CLI tooling - not WORKDIR (the base image's own entrypoint has
# opinions about that) - run via
# `docker compose exec clickhouse /scripts/<name>.sh`.
COPY scripts/ /scripts/
RUN chmod +x /scripts/*.sh
