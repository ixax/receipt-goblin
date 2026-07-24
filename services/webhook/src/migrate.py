"""Idempotent ClickHouse migration runner - applies every
services/clickhouse/migrations/*.sql file at most once, then exits. Runs on
every `docker compose up` via clickhouse-migrate (webhook/webhook-worker
`depends_on` it with `condition: service_completed_successfully`).

Applied migrations are recorded in schema_migrations and never re-executed.
Destructive recreate+swap migrations (like 001_replacing_mergetree.sql) also
get a SKIP_CHECKS guard so a fresh volume - already matching the
post-migration shape via schema.sql - skips running that SQL at all.
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
    """Creates/refreshes CLICKHOUSE_USER via the bootstrap superuser. OR
    REPLACE makes this idempotent and picks up a changed CLICKHOUSE_PASSWORD
    on every start; unlike migrations/*.sql, the password never touches
    disk or schema_migrations.
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
    """True once agent_events is already ReplacingMergeTree (migrated
    already, or created that way by schema.sql on a fresh volume)."""
    rows = client.query(
        "SELECT engine FROM system.tables WHERE database = currentDatabase() AND name = 'agent_events'"
    ).result_rows
    return bool(rows) and rows[0][0] == "ReplacingMergeTree"


# Maps a migration stem to callable(client) -> bool: True means record as
# applied without running its SQL (target already has the shape it'd
# produce). Unlisted migrations are assumed safe `IF NOT EXISTS` DDL.
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
