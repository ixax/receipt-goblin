"""Idempotent ClickHouse migration runner - applies every
services/clickhouse/migrations/*.sql file at most once, then exits. Runs
automatically on every `docker compose up` via the clickhouse-migrate
service (webhook/webhook-worker both `depends_on` it with
`condition: service_completed_successfully` - see docker-compose.yml), so a
stack's tables are always brought up to date before anything tries to write
to them - no separate manual step needed on either a brand-new volume (where
schema.sql already created the final shape, see SKIP_CHECKS below) or an
existing one (where a migration actually needs to run).

Safe to re-run any number of times: applied migrations are recorded in
schema_migrations and never re-executed. Migrations that do a destructive
recreate+swap (like 001_replacing_mergetree.sql) additionally get a
structural SKIP_CHECKS guard so a fresh volume - whose tables already match
the post-migration shape via schema.sql, with zero rows to lose - never
runs that SQL at all instead of just harmlessly re-running it.
"""
import logging
import os
from pathlib import Path

import clickhouse_connect

from .clickhouse_ingest import get_client
from .config import (
    CLICKHOUSE_BOOTSTRAP_PASSWORD,
    CLICKHOUSE_BOOTSTRAP_USER,
    CLICKHOUSE_DATABASE,
    CLICKHOUSE_HOST,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clickhouse.migrate")

MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", "/app/migrations"))


def _ensure_app_user() -> None:
    """Creates/refreshes the SQL-managed app user (CLICKHOUSE_USER, stored in
    ClickHouse's local_directory access storage - see system.users) using the
    bootstrap superuser the image provisions (see docker-compose.yml's
    x-clickhouse-bootstrap-* anchors and CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT).
    OR REPLACE makes this idempotent on every stack start - including picking
    up a changed CLICKHOUSE_PASSWORD in .env - and unlike the migrations/*.sql
    files below, the password never touches disk or schema_migrations.
    Grants are database-scoped, not instance-wide.
    """
    bootstrap_client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_BOOTSTRAP_USER,
        password=CLICKHOUSE_BOOTSTRAP_PASSWORD,
    )
    try:
        bootstrap_client.command(
            f"CREATE USER OR REPLACE {CLICKHOUSE_USER} IDENTIFIED BY {{password:String}} "
            f"DEFAULT DATABASE {CLICKHOUSE_DATABASE}",
            parameters={"password": CLICKHOUSE_PASSWORD},
        )
        bootstrap_client.command(f"GRANT ALL ON {CLICKHOUSE_DATABASE}.* TO {CLICKHOUSE_USER}")
    finally:
        bootstrap_client.close()
    logger.info("ensured app user %s exists (database %s)", CLICKHOUSE_USER, CLICKHOUSE_DATABASE)


def _already_replacing_mergetree(client) -> bool:
    """SKIP_CHECKS guard for 001_replacing_mergetree: true once agent_events
    is already ReplacingMergeTree (either migrated already, or created
    straight into that shape by schema.sql on a fresh volume)."""
    rows = client.query(
        "SELECT engine FROM system.tables WHERE database = currentDatabase() AND name = 'agent_events'"
    ).result_rows
    return bool(rows) and rows[0][0] == "ReplacingMergeTree"


# Maps a migration file's stem to a callable(client) -> bool: when it
# returns True, the migration is recorded as applied WITHOUT executing its
# SQL (the target already has the shape the migration would produce, so
# running it would only be destructive/wasteful, never a no-op). Migrations
# not listed here are assumed to be pure `IF NOT EXISTS`-style DDL, safe to
# run for real every time they're not yet recorded.
SKIP_CHECKS = {
    "001_replacing_mergetree": _already_replacing_mergetree,
}


def _statements(sql_text: str) -> list[str]:
    lines = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        lines.append(line[:idx] if idx != -1 else line)
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def _mark_applied(client, version: str) -> None:
    client.insert("schema_migrations", [[version]], column_names=["version"])


def _is_recorded(client, version: str) -> bool:
    result = client.query(
        "SELECT count() FROM schema_migrations WHERE version = {version:String}",
        parameters={"version": version},
    )
    return result.result_rows[0][0] > 0


def run_migration(client, path: Path) -> None:
    version = path.stem

    if _is_recorded(client, version):
        logger.info("skip %s (already recorded in schema_migrations)", version)
        return

    skip_check = SKIP_CHECKS.get(version)
    if skip_check and skip_check(client):
        logger.info("skip %s (target already has the post-migration shape)", version)
        _mark_applied(client, version)
        return

    logger.info("applying %s", version)
    for statement in _statements(path.read_text()):
        client.command(statement)
    _mark_applied(client, version)
    logger.info("applied %s", version)


def main() -> None:
    _ensure_app_user()
    client = get_client()
    client.command(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version String, applied_at DateTime64(3) DEFAULT now64(3)) "
        "ENGINE = MergeTree ORDER BY version"
    )
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        run_migration(client, path)
    logger.info("all migrations up to date")


if __name__ == "__main__":
    main()
