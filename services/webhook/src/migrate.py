"""Idempotent ClickHouse migration runner - applies every
services/clickhouse/migrations/*.sql file at most once, then exits. Runs on
every `docker compose up` via clickhouse-migrate (webhook/webhook-worker
`depends_on` it with `condition: service_completed_successfully`).

Applied migrations are recorded in schema_migrations and never re-executed.
Destructive recreate+swap migrations (like 001_replacing_mergetree.sql) also
get a SKIP_CHECKS guard so a fresh volume - already matching the
post-migration shape via schema.sql - skips running that SQL at all.

_grant_ui_access_to_app_user_once() is the one exception to "every step is a
migrations/*.sql file" - it needs CLICKHOUSE_USER interpolated in, which the
plain-SQL migration files have no templating for, so it's Python instead but
still tracked in schema_migrations under its own version so it only ever
runs once, at true first initialization - not on every start like
_ensure_app_user()'s GRANT ALL, so a grant manually revoked later (e.g. via
services/clickhouse/scripts/create_user.sh) doesn't get silently reinstated
on the next `docker compose up`.
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


def _event_sources_already_renamed(client) -> bool:
    """True when event_sources doesn't exist under its old name - either a
    fresh volume (schema.sql already creates ingest_raw/ingest_dlq directly,
    the old name never existed) or a stack that already ran
    007_rename_ingest_tables. Both 006 (ALTER TABLE event_sources ...) and
    007 (RENAME TABLE event_sources TO ...) target the pre-rename name, so
    both fail outright on a fresh volume without this guard - confirmed via
    a from-scratch clickhouse-data volume: 006 raised UNKNOWN_TABLE on
    event_sources and blocked every migration after it, 007 included."""
    rows = client.query(
        "SELECT count() FROM system.tables WHERE database = currentDatabase() AND name = 'event_sources'"
    ).result_rows
    return rows[0][0] == 0


# Maps a migration stem to callable(client) -> bool: True means record as
# applied without running its SQL (target already has the shape it'd
# produce). Unlisted migrations are assumed safe `IF NOT EXISTS` DDL.
SKIP_CHECKS = {
    "001_replacing_mergetree": _already_replacing_mergetree,
    "006_event_sources_lower_compression": _event_sources_already_renamed,
    "007_rename_ingest_tables": _event_sources_already_renamed,
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


def _grant_ui_access_to_app_user_once(client) -> None:
    """One-time: grants CLICKHOUSE_USER read-only system.* access (Play's
    schema-browser sidebar, ClickHouse's built-in /dashboard page). Runs
    once, at first initialization only - see this module's docstring for
    why that's different from _ensure_app_user()'s every-start GRANT ALL.

    Needs the bootstrap superuser, not the app-user `client` passed in for
    its schema_migrations bookkeeping only: CLICKHOUSE_USER has no GRANT
    OPTION on anything (see _ensure_app_user - its own GRANT ALL is never
    WITH GRANT OPTION), so it can't grant system.* access to itself.
    """
    version = "008_admin_ui_access"
    if _is_recorded(client, version):
        logger.info("skip %s (already recorded in schema_migrations)", version)
        return
    bootstrap_client = clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_BOOTSTRAP_USER,
        password=CLICKHOUSE_BOOTSTRAP_PASSWORD,
    )
    try:
        bootstrap_client.command(f"GRANT SELECT ON system.* TO {CLICKHOUSE_USER}")
    finally:
        bootstrap_client.close()
    _mark_applied(client, version)
    logger.info("applied %s (granted system.* SELECT to %s)", version, CLICKHOUSE_USER)


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
    _grant_ui_access_to_app_user_once(client)
    logger.info("all migrations up to date")


if __name__ == "__main__":
    main()
