"""Idempotent ClickHouse migration runner - applies every
services/clickhouse/migrations/*.sql file at most once, then exits. Runs on
every `docker compose up` via clickhouse-migrate (webhook/webhook-worker
`depends_on` it with `condition: service_completed_successfully`).

Applied migrations are recorded in schema_migrations and never re-executed.
Destructive recreate+swap migrations (like 001_replacing_mergetree.sql) also
get a SKIP_CHECKS guard so a fresh volume - already matching the
post-migration shape via schema.sql - skips running that SQL at all.

run_migration() passes CLICKHOUSE_USER/CLICKHOUSE_PASSWORD/CLICKHOUSE_DATABASE
as query parameters to every statement in every migrations/*.sql file, so a
migration can reference {clickhouse_user:String}/{clickhouse_password:String}/
{clickhouse_database:String} directly in an ordinary expression position
(e.g. a WHERE clause). A statement that doesn't reference any of the three
is unaffected - unused named parameters are just ignored.

That substitution does NOT work everywhere, though - confirmed against a
live server: `{name:Type}` is rejected with a SYNTAX_ERROR
("Expected substitution type (identifier)") anywhere inside a Dictionary's
SOURCE(CLICKHOUSE(...)) clause, even for something as simple as its DB
parameter - that clause's USER/PASSWORD/DB/QUERY sub-grammar only accepts
literals, not the general expression position {name:Type} hooks into
(unlike e.g. CREATE USER ... IDENTIFIED BY {password:String} below, which
does work). So _create_dictionaries_once() below builds those CREATE
DICTIONARY statements as an f-string with CLICKHOUSE_USER/PASSWORD/DATABASE
embedded directly (via _sql_string_literal()'s escaping, not client.query's
parameters=) rather than going through a migrations/*.sql file at all.

_grant_ui_access_to_app_user_once() and _create_dictionaries_once() are
therefore both Python, alongside _ensure_app_user(): a GRANT's target user
is an identifier, not a value, so it can't go through {name:Type}
substitution either. Both are still tracked in schema_migrations under
their own version so they only run once, at true first initialization -
not on every start like _ensure_app_user()'s GRANT ALL, so e.g. a grant
manually revoked later (e.g. via services/clickhouse/scripts/create_user.sh)
doesn't get silently reinstated on the next `docker compose up`.
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


def _ingest_dlq_already_partitioned(client) -> bool:
    """True once ingest_dlq already has a partition key (migrated already,
    or created that way by schema.sql on a fresh volume) - guards against
    011_ingest_dlq_partition_by.sql's recreate+rename running again on a
    stack that already has the target shape."""
    rows = client.query(
        "SELECT partition_key FROM system.tables WHERE database = currentDatabase() AND name = 'ingest_dlq'"
    ).result_rows
    return bool(rows) and rows[0][0] != ""


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
    "011_ingest_dlq_partition_by": _ingest_dlq_already_partitioned,
}


# Available to every statement in every migrations/*.sql file - see this
# module's docstring. A statement that doesn't reference one of these names
# is unaffected.
_MIGRATION_PARAMETERS = {
    "clickhouse_user": CLICKHOUSE_USER,
    "clickhouse_password": CLICKHOUSE_PASSWORD,
    "clickhouse_database": CLICKHOUSE_DATABASE,
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
        client.command(statement, parameters=_MIGRATION_PARAMETERS)
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


def _grant_query_log_access_once(client) -> None:
    """One-time: grants CLICKHOUSE_USER SELECT on system.query_log plus
    SYSTEM FLUSH LOGS - mcp-server's new `profile_query` tool needs both to
    read memory_usage/read_rows/read_bytes for a query it just ran (SYSTEM
    FLUSH LOGS forces query_log's async insert to land immediately, instead
    of the tool racing an unflushed buffer). Same one-time-at-first-init
    shape as _grant_ui_access_to_app_user_once above (see its docstring),
    and needs the same bootstrap superuser for the same reason (no GRANT
    OPTION on the app user).

    Known fragility, not fixed here (pre-existing, applies equally to
    _grant_ui_access_to_app_user_once): _ensure_app_user()'s `CREATE USER OR
    REPLACE` on every startup silently wipes every grant made outside that
    function, including this one and the system.* SELECT grant above - so
    despite being recorded in schema_migrations as applied, both may need
    re-granting by hand after a `CREATE USER OR REPLACE` cycle. Confirmed
    against the live stack while building this: system.query_log SELECT
    and system.* SELECT were both gone despite their versions showing
    applied. Flagged for a separate fix, not silently patched here.
    """
    version = "010_query_log_access"
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
        bootstrap_client.command(f"GRANT SELECT ON system.query_log TO {CLICKHOUSE_USER}")
        bootstrap_client.command(f"GRANT SYSTEM FLUSH LOGS ON *.* TO {CLICKHOUSE_USER}")
    finally:
        bootstrap_client.close()
    _mark_applied(client, version)
    logger.info("applied %s (granted system.query_log SELECT + SYSTEM FLUSH LOGS to %s)", version, CLICKHOUSE_USER)


def _sql_string_literal(value: str) -> str:
    """Escapes value for embedding directly in SQL text as '...' - only
    needed where {name:Type} substitution doesn't parse (SOURCE(CLICKHOUSE(
    ...)) - see this module's docstring); everywhere else, prefer
    parameters= instead of this."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# name -> (source query, column defs, PRIMARY KEY column, LAYOUT). Each
# source query is exactly the same LEFT JOIN subquery every dashboard panel
# in agents_overview.json currently repeats inline (session_git_branch,
# ai_gateway_users, ai_gateway_groups) - a Dictionary runs that same GROUP
# BY/argMax scan once per LIFETIME refresh (60-120s) instead of once per
# dashboard query. session_git_branch_dict keys on session_id_hash (added
# by migrations/008_session_id_hash.sql) rather than session_id itself:
# HASHED() on a UInt64 key avoids the packing overhead COMPLEX_KEY_HASHED
# needs for a String key, and this schema already established
# id = cityHash64(value) as its convention for a high-cardinality string
# join key (see `clients`). ai_gateway_users/groups stay on their plain
# String primary key - already LowCardinality, low cardinality, no
# measurable benefit from a surrogate key.
_DICTIONARIES = {
    "session_git_branch_dict": (
        "SELECT session_id_hash, "
        "argMax(git_branch, captured_at) AS git_branch, "
        "argMax(git_repo, captured_at) AS git_repo, "
        "argMax(issue_id, captured_at) AS issue_id "
        "FROM session_git_branch GROUP BY session_id_hash",
        "(session_id_hash UInt64, git_branch String, git_repo String, issue_id String)",
        "session_id_hash",
    ),
    "ai_gateway_users_dict": (
        "SELECT user_id, "
        "argMax(user_name, updated_at) AS user_name, "
        "argMax(group_id, updated_at) AS group_id "
        "FROM ai_gateway_users GROUP BY user_id",
        "(user_id String, user_name String, group_id String)",
        "user_id",
    ),
    "ai_gateway_groups_dict": (
        "SELECT group_id, argMax(group_name, updated_at) AS group_name "
        "FROM ai_gateway_groups GROUP BY group_id",
        "(group_id String, group_name String)",
        "group_id",
    ),
}


def _create_dictionaries_once(client) -> None:
    """One-time: creates the Dictionaries listed in _DICTIONARIES above.
    HOST/PORT are deliberately omitted from SOURCE(CLICKHOUSE(...)) - with
    them unset, ClickHouse reads its own local table directly instead of
    opening a native-protocol TCP connection to itself. USER/PASSWORD are
    still required even for that local path (confirmed: a plain
    SOURCE(CLICKHOUSE(QUERY '...')) with neither tries to connect as
    `default` with an empty password and fails AUTHENTICATION_FAILED
    against this stack's password-protected `default` user) - and, per this
    module's docstring, {name:Type} substitution isn't usable inside
    SOURCE(CLICKHOUSE(...)) at all, so they're embedded as escaped string
    literals instead.

    IF NOT EXISTS (not CREATE OR REPLACE): a dictionary already serving
    dashboard queries shouldn't be silently swapped by a migration re-run.
    """
    version = "009_dashboard_dictionaries"
    if _is_recorded(client, version):
        logger.info("skip %s (already recorded in schema_migrations)", version)
        return
    user_lit = _sql_string_literal(CLICKHOUSE_USER)
    password_lit = _sql_string_literal(CLICKHOUSE_PASSWORD)
    db_lit = _sql_string_literal(CLICKHOUSE_DATABASE)
    for name, (query, columns, primary_key) in _DICTIONARIES.items():
        client.command(
            f"CREATE DICTIONARY IF NOT EXISTS {name} {columns} "
            f"PRIMARY KEY {primary_key} "
            f"SOURCE(CLICKHOUSE(USER {user_lit} PASSWORD {password_lit} DB {db_lit} "
            f"QUERY {_sql_string_literal(query)})) "
            "LAYOUT(HASHED()) "
            "LIFETIME(MIN 60 MAX 120)"
        )
        logger.info("created dictionary %s", name)
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
    _grant_ui_access_to_app_user_once(client)
    _create_dictionaries_once(client)
    _grant_query_log_access_once(client)
    logger.info("all migrations up to date")


if __name__ == "__main__":
    main()
